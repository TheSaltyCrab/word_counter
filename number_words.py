#!/usr/bin/env python3
"""
number_words.py - core engine for numbering the words of scanned / photographed
story pages.

Each word of the story body gets a small red running number printed just below
it. Numbering restarts at 1 at every story title and carries across the pages of
a story. Titles, sight-word header bars, footers, scanner shadows and blank
(bleed-through) back pages are skipped automatically.

Used two ways:
  * as a library by app.py (the Streamlit web app), and
  * as a command line tool for PDFs:

        python number_words.py input.pdf output.pdf
        python number_words.py input.pdf output.pdf --color "#c00000"
        python number_words.py input.pdf output.pdf --new-story-pages 1,3,5,7
"""
from __future__ import annotations

import argparse
import io
import math
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

import cv2
import img2pdf
import numpy as np
import pytesseract
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pytesseract import Output

# --------------------------------------------------------------------------- #
# Tunables (relative to text size / page size wherever possible)
# --------------------------------------------------------------------------- #
MIN_BODY_LINE_HEIGHT = 0.70   # a body line's median word height vs. typical
MIN_BODY_LINES = 2            # fewer body lines than this => blank / back page
NUMBER_GAP_FRAC = 0.36        # baseline -> top of number, as a fraction of line height
TITLE_MAX_WORDS = 6           # a title line has at most this many words
TITLE_CENTER_TOL = 0.06       # title centre must be within this x page width
DESKEW_THRESHOLD_DEG = 2.0    # pages tilted more than this are rotated flat first
LOW_CONF_FLAG = 80            # words below this confidence are listed for review

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]

DESCENDERS = set("gjpqy,;()[]{}/|Q")


# --------------------------------------------------------------------------- #
# Options & data structures
# --------------------------------------------------------------------------- #
@dataclass
class Options:
    color: tuple = (224, 0, 0)       # number colour (R, G, B)
    font_frac: float = 0.48          # number size as a fraction of line height
    min_conf: int = 60               # discard OCR words below this confidence
    normalize_lighting: bool = True  # even out shadows before OCR (photos)
    straighten: bool = False         # find the page edges and flatten (photos)
    clean_background: bool = False   # remove shadows from the OUTPUT page too (photos)
    max_side: int = 3300             # downscale huge photos to this long side
    jpeg_quality: int = 88


@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float
    yc: float = 0.0                  # vertical centre after tilt correction

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def bottom(self) -> int:
        return self.y + self.h


@dataclass
class Line:
    words: list = field(default_factory=list)

    @property
    def height(self) -> int:
        return max(w.h for w in self.words)

    @property
    def med_height(self) -> float:
        return statistics.median(w.h for w in self.words)

    @property
    def cy(self) -> float:
        return statistics.median(w.yc for w in self.words)

    @property
    def left(self) -> int:
        return min(w.x for w in self.words)

    @property
    def right(self) -> int:
        return max(w.x + w.w for w in self.words)


@dataclass
class PageResult:
    index: int
    lines: list
    title: Optional[Line]
    slope: float
    is_blank: bool = False


@dataclass
class PageOutput:
    """What the caller gets back for each processed page."""
    index: int
    jpeg: bytes
    story: Optional[int]
    title: Optional[str]
    starts_story: bool
    words: int
    first_number: Optional[int]
    last_number: Optional[int]
    low_conf: list
    is_blank: bool
    note: str = ""


# --------------------------------------------------------------------------- #
# Photo preparation (orientation, page edges, size)
# --------------------------------------------------------------------------- #
def _order_quad(pts: np.ndarray) -> np.ndarray:
    pts = pts.reshape(4, 2).astype("float32")
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)],
                     pts[np.argmax(s)], pts[np.argmax(d)]], dtype="float32")  # tl, tr, br, bl


def find_page_quad(bgr: np.ndarray) -> Optional[np.ndarray]:
    """Find the paper's four corners (full-res coordinates), or None if unsure."""
    h, w = bgr.shape[:2]
    scale = 1000.0 / max(h, w)
    small = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (7, 7), 0)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25)))
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    ratio = cv2.contourArea(c) / float(small.shape[0] * small.shape[1])
    if not (0.35 <= ratio <= 0.985):          # no visible background => leave as is
        return None
    hull = cv2.convexHull(c)
    approx = cv2.approxPolyDP(hull, 0.02 * cv2.arcLength(hull, True), True)
    if len(approx) == 4:
        quad = approx.reshape(4, 2).astype("float32")
    else:                                      # fall back to a rotated rectangle
        quad = cv2.boxPoints(cv2.minAreaRect(hull)).astype("float32")
    return _order_quad(quad) / scale


def straighten_page(img: Image.Image) -> Image.Image:
    """Crop to the paper and correct perspective, if the paper's edges are visible."""
    bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    quad = find_page_quad(bgr)
    if quad is None:
        return img
    tl, tr, br, bl = quad
    width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    if width < 400 or height < 400:
        return img
    dst = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype="float32")
    m = cv2.getPerspectiveTransform(quad, dst)
    warped = cv2.warpPerspective(bgr, m, (width, height), flags=cv2.INTER_CUBIC,
                                 borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(cv2.cvtColor(warped, cv2.COLOR_BGR2RGB))


def prepare_image(img: Image.Image, rotation: int = 0, opts: Optional[Options] = None) -> Image.Image:
    """Phone-photo hygiene: EXIF orientation, manual rotation, page edges, size."""
    opts = opts or Options()
    img = ImageOps.exif_transpose(img).convert("RGB")
    if rotation % 360:
        img = img.rotate(-rotation, expand=True)       # rotation is clockwise degrees
    if opts.straighten:
        img = straighten_page(img)
    long_side = max(img.size)
    if long_side > opts.max_side:
        f = opts.max_side / long_side
        img = img.resize((round(img.width * f), round(img.height * f)), Image.LANCZOS)
    return img


# --------------------------------------------------------------------------- #
# OCR + layout analysis
# --------------------------------------------------------------------------- #
def normalize_lighting(gray: np.ndarray) -> np.ndarray:
    """Divide out the paper's brightness so shadows and uneven light disappear."""
    h, w = gray.shape
    s = 4
    small = cv2.resize(gray, (max(1, w // s), max(1, h // s)), interpolation=cv2.INTER_AREA)
    k = max(5, (w // 100 // s) | 1)
    bg = cv2.dilate(small, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    bg = cv2.GaussianBlur(bg, (0, 0), sigmaX=max(3.0, w / 50.0 / s))
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR)
    return cv2.divide(gray, np.maximum(bg, 1).astype(np.uint8), scale=255)


def clean_page(img: Image.Image) -> Image.Image:
    """Shadow-free, evenly lit version of the page (greyscale), for printing."""
    gray = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    return Image.fromarray(normalize_lighting(gray)).convert("RGB")


def ocr_words(img: Image.Image, opts: Options) -> list:
    """Run Tesseract and return confident, alphanumeric word boxes."""
    gray = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    if opts.normalize_lighting:
        gray = normalize_lighting(gray)
    d = pytesseract.image_to_data(gray, output_type=Output.DICT, config="--psm 6")
    words = []
    for i, t in enumerate(d["text"]):
        t = t.strip()
        if not t or not re.search(r"[A-Za-z0-9]", t):
            continue
        try:
            conf = float(d["conf"][i])
        except ValueError:
            continue
        if conf < opts.min_conf:
            continue
        words.append(Word(t, d["left"][i], d["top"][i], d["width"][i], d["height"][i], conf))
    return words


def estimate_tilt(words: list) -> float:
    """Page tilt as dy/dx, found by maximising the sharpness of the baseline histogram."""
    if not words:
        return 0.0
    med_h = statistics.median(w.h for w in words)
    pts = [(w.cx, w.bottom) for w in words if w.h > 0.5 * med_h and not (set(w.text) & DESCENDERS)]
    if len(pts) < 15:
        return 0.0
    xs = np.array([p[0] for p in pts])
    ys = np.array([p[1] for p in pts])
    best, best_score = 0.0, -1.0
    for deg in np.arange(-8.0, 8.001, 0.1):
        t = np.tan(np.radians(deg))
        yy = ys - t * xs
        hist, _ = np.histogram(yy, bins=np.arange(yy.min(), yy.max() + 6, 5))
        score = float((hist.astype(float) ** 2).sum())
        if score > best_score:
            best, best_score = t, score
    return float(best)


def group_into_lines(words: list) -> list:
    """Cluster words into text lines by (tilt-corrected) vertical position."""
    if not words:
        return []
    hs = sorted(w.h for w in words)
    tol = 0.7 * hs[int(len(hs) * 0.75)]
    lines: list = []
    for w in sorted(words, key=lambda w: w.yc):
        if lines and abs(w.yc - lines[-1].cy) < tol:
            lines[-1].words.append(w)
        else:
            lines.append(Line([w]))
    for ln in lines:
        ln.words.sort(key=lambda w: w.x)
    lines.sort(key=lambda ln: ln.cy)
    return lines


def drop_isolated_words(lines: list) -> None:
    """Remove specks of noise sitting far from the rest of their line (in place)."""
    for ln in lines:
        while len(ln.words) >= 4:
            h = ln.med_height
            first_gap = ln.words[1].x - (ln.words[0].x + ln.words[0].w)
            last_gap = ln.words[-1].x - (ln.words[-2].x + ln.words[-2].w)
            if first_gap > 2.5 * h and ln.words[0].w < 1.5 * h:
                ln.words.pop(0)
            elif last_gap > 2.5 * h and ln.words[-1].w < 1.5 * h:
                ln.words.pop()
            else:
                break


def trim_outliers(body: list) -> list:
    """Drop lines stranded far above/below the main text block (stray footer or
    header debris). Double-spaced text has an even line pitch, so a gap of more
    than ~2.5 pitches before the first or after the last line is not body text."""
    body = list(body)
    if len(body) < 4:
        return body
    cys = [ln.cy for ln in body]
    pitch = statistics.median(b - a for a, b in zip(cys, cys[1:]))
    if pitch <= 0:
        return body
    while len(body) >= 4 and body[-1].cy - body[-2].cy > 2.0 * pitch:
        body.pop()
    while len(body) >= 4 and body[1].cy - body[0].cy > 2.0 * pitch:
        body.pop(0)
    return body


def _analyse(index: int, img: Image.Image, opts: Options):
    words = ocr_words(img, opts)
    slope = estimate_tilt(words)
    return words, slope


def analyse_page(index: int, img: Image.Image, opts: Options):
    """Return (PageResult, image). The image is rotated flat first if badly tilted."""
    words, slope = _analyse(index, img, opts)
    deg = math.degrees(math.atan(slope))
    if abs(deg) > DESKEW_THRESHOLD_DEG:
        img = img.rotate(deg, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))
        words, slope = _analyse(index, img, opts)
    for w in words:
        w.yc = w.cy - slope * w.cx
    lines = group_into_lines(words)
    drop_isolated_words(lines)
    lines = [ln for ln in lines if ln.words]
    if not lines:
        return PageResult(index, [], None, 0.0, is_blank=True), img

    # Body lines are set in large type; running heads and footers are much smaller.
    meds = sorted(ln.med_height for ln in lines)
    typical = meds[int(len(meds) * 0.75)]
    body = [ln for ln in lines if ln.med_height >= MIN_BODY_LINE_HEIGHT * typical]
    body = [ln for ln in body if len(ln.words) >= 2 or ln.height >= 0.8 * typical]

    body = trim_outliers(body)

    if len(body) < MIN_BODY_LINES or sum(len(l.words) for l in body) < 8:
        return PageResult(index, [], None, 0.0, is_blank=True), img

    # Title detection: a short, centred first line.
    title = None
    first = body[0]
    full = [ln for ln in body[1:] if len(ln.words) >= 6]
    if full and len(first.words) <= TITLE_MAX_WORDS:
        centre = statistics.median((ln.left + ln.right) / 2 for ln in full)
        first_c = (first.left + first.right) / 2
        width_ratio = (first.right - first.left) / max(
            1, statistics.median(ln.right - ln.left for ln in full))
        if abs(first_c - centre) < TITLE_CENTER_TOL * img.width and width_ratio < 0.75:
            title = first
            body = body[1:]

    return PageResult(index, body, title, slope), img


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #
def load_font(size: int):
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    try:                                   # Pillow >= 10.1 can size the default font
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def hex_to_rgb(s: str) -> tuple:
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def annotate_page(img: Image.Image, res: PageResult, start: int, opts: Options):
    """Draw numbers under each body word. Returns (image, next_number, log)."""
    out = img.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    line_h = statistics.median(ln.height for ln in res.lines)
    font = load_font(max(18, int(line_h * opts.font_frac)))
    gap = line_h * NUMBER_GAP_FRAC
    n = start
    log = []

    for ln in res.lines:
        base_pts = [(w.cx, w.bottom) for w in ln.words if not (set(w.text) & DESCENDERS)]
        if not base_pts:
            base_pts = [(w.cx, w.bottom - 0.22 * line_h) for w in ln.words]
        intercept = statistics.median(y - res.slope * x for x, y in base_pts)

        prev_right = -1e9
        for w in ln.words:
            label = str(n)
            tw = draw.textlength(label, font=font)
            x = w.cx - tw / 2
            if x < prev_right + 6:                     # nudge right to avoid overlap
                x = prev_right + 6
            y = intercept + res.slope * w.cx + gap
            draw.text((x, y), label, fill=opts.color, font=font)
            prev_right = x + tw
            log.append({"n": n, "word": w.text, "conf": round(w.conf, 1)})
            n += 1
    return out, n, log


# --------------------------------------------------------------------------- #
# Main pipeline (used by the web app and the CLI)
# --------------------------------------------------------------------------- #
Source = Union[Image.Image, Callable[[], Image.Image]]


def process_pages(
    sources: Sequence[Source],
    opts: Optional[Options] = None,
    story_flags: Optional[Sequence[Optional[bool]]] = None,
    rotations: Optional[Sequence[int]] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    on_page: Optional[Callable[[PageOutput, Image.Image], None]] = None,
) -> list:
    """Number the words on every page.

    sources      page images, or zero-argument callables that load them lazily.
    story_flags  per page: True = starts a new story, False = continues the
                 previous one, None = decide automatically from the title.
    rotations    per page: extra clockwise rotation in degrees (0/90/180/270).
    """
    opts = opts or Options()
    outputs: list = []
    counter, story_no = 1, 0
    total = len(sources)

    for i, src in enumerate(sources):
        if progress:
            progress(i, total, f"Reading page {i + 1} of {total}")
        img = src() if callable(src) else src
        img = prepare_image(img, rotations[i] if rotations else 0, opts)
        res, img = analyse_page(i, img, opts)

        if res.is_blank:
            annotated, log, starts, note = img.convert("RGB"), [], False, \
                "No story text found - page left unchanged"
        else:
            flag = story_flags[i] if story_flags else None
            starts = story_no == 0 or (flag if flag is not None else res.title is not None)
            if starts:
                story_no += 1
                counter = 1
            base = clean_page(img) if opts.clean_background else img
            annotated, counter, log = annotate_page(base, res, counter, opts)
            note = ""

        buf = io.BytesIO()
        annotated.save(buf, format="JPEG", quality=opts.jpeg_quality, dpi=(300, 300))
        out = PageOutput(
            index=i,
            jpeg=buf.getvalue(),
            story=None if res.is_blank else story_no,
            title=" ".join(w.text for w in res.title.words) if res.title else None,
            starts_story=starts,
            words=len(log),
            first_number=log[0]["n"] if log else None,
            last_number=log[-1]["n"] if log else None,
            low_conf=[l for l in log if l["conf"] < LOW_CONF_FLAG],
            is_blank=res.is_blank,
            note=note,
        )
        outputs.append(out)
        if on_page:
            on_page(out, annotated)

    if progress:
        progress(total, total, "Done")
    return outputs


def build_pdf(outputs: Sequence[PageOutput]) -> bytes:
    """Combine the annotated page images into one PDF."""
    return img2pdf.convert([o.jpeg for o in outputs])


def story_totals(outputs: Sequence[PageOutput]) -> list:
    """[{story, title, pages, words}] for each story, in order."""
    stories: dict = {}
    for o in outputs:
        if o.story is None:
            continue
        s = stories.setdefault(o.story, {"story": o.story, "title": None, "pages": [], "words": 0})
        if o.title and not s["title"]:
            s["title"] = o.title
        s["pages"].append(o.index + 1)
        s["words"] += o.words
    return list(stories.values())


# --------------------------------------------------------------------------- #
# Command line interface (PDF in, PDF out)
# --------------------------------------------------------------------------- #
def main() -> int:
    from pdf2image import convert_from_path, pdfinfo_from_path

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--color", default="#e00000", help="number colour as hex (default #e00000)")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--new-story-pages", default=None,
                    help="comma-separated 1-based pages where numbering restarts "
                         "(overrides automatic title detection)")
    ap.add_argument("--straighten", action="store_true", help="find page edges and flatten (for photos)")
    ap.add_argument("--clean", action="store_true", help="remove shadows from the output page (for photos)")
    ap.add_argument("--debug-dir", default=None, help="also save annotated pages as PNGs here")
    args = ap.parse_args()

    n_pages = pdfinfo_from_path(args.input)["Pages"]
    sources = [
        (lambda p=p: convert_from_path(args.input, dpi=args.dpi, first_page=p, last_page=p)[0])
        for p in range(1, n_pages + 1)
    ]
    flags = None
    if args.new_story_pages:
        starts = {int(p) for p in args.new_story_pages.split(",")}
        flags = [(p in starts) for p in range(1, n_pages + 1)]

    debug = Path(args.debug_dir) if args.debug_dir else None
    if debug:
        debug.mkdir(parents=True, exist_ok=True)

    opts = Options(color=hex_to_rgb(args.color), straighten=args.straighten, clean_background=args.clean)
    outs = process_pages(
        sources, opts, story_flags=flags,
        progress=lambda i, n, msg: print(f"  {msg}", file=sys.stderr),
        on_page=(lambda o, im: im.save(debug / f"page_{o.index + 1:02d}.png")) if debug else None,
    )
    Path(args.output).write_bytes(build_pdf(outs))

    print(f"\nWrote {args.output}  ({len(outs)} pages)\n")
    print(f"{'page':>4}  {'story':>5}  {'words':>5}  {'last #':>6}  notes")
    for o in outs:
        notes = o.note or (f"new story: '{o.title}'" if o.title else "")
        if o.low_conf:
            notes += f"  [{len(o.low_conf)} low-confidence word(s)]"
        print(f"{o.index + 1:>4}  {str(o.story or '-'):>5}  {o.words:>5}  "
              f"{str(o.last_number or '-'):>6}  {notes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
