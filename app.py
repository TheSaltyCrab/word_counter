"""
Word Numberer - Streamlit web app.

Photograph (or upload) pages of a story, and get back a PDF with a red running
number printed under every word. Numbering restarts at each story title.

Run locally:   ALLOW_NO_PASSWORD=1 streamlit run app.py
Deploy:        see README.md  (set APP_PASSWORD in the app's Secrets)
"""
import hmac
import io
import os
import re
import time
import uuid

import pandas as pd
import streamlit as st
from PIL import Image, ImageOps

import number_words as nw

st.set_page_config(page_title="Word Numberer", page_icon="🔢", layout="centered")

MAX_PAGES = 30
STORED_MAX_SIDE = 4200          # keep (nearly) full phone resolution; the page is cropped
                                # out of the photo first and only then shrunk for OCR
AUTO = "Auto-detect"
NEW_STORY = "Starts a new story"
CONTINUES = "Continues previous story"
STORY_CHOICES = [AUTO, NEW_STORY, CONTINUES]

ss = st.session_state


# --------------------------------------------------------------------------- #
# Password gate (fails closed: no password configured => the app will not run)
# --------------------------------------------------------------------------- #
def _configured_password():
    try:
        pw = st.secrets["APP_PASSWORD"]
    except Exception:
        pw = None
    return pw or os.environ.get("APP_PASSWORD")


def require_login():
    if ss.get("authed"):
        return
    pw = _configured_password()
    if not pw:
        if os.environ.get("ALLOW_NO_PASSWORD") == "1":      # local development only
            ss["authed"] = True
            return
        st.title("🔢 Word Numberer")
        st.error(
            "No password is configured, so the app is locked. "
            "Add `APP_PASSWORD` to the app's Secrets (see README.md)."
        )
        st.stop()
    st.title("🔢 Word Numberer")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        ok = st.form_submit_button("Log in")
    if ok:
        if hmac.compare_digest(entered.encode(), str(pw).encode()):
            ss["authed"] = True
            st.rerun()
        else:
            time.sleep(1.0)                                   # slow down guessing
            st.error("Wrong password.")
    st.stop()


require_login()


# --------------------------------------------------------------------------- #
# Session state & helpers
# --------------------------------------------------------------------------- #
ss.setdefault("pages", [])       # [{id, name, data(jpeg bytes), thumb(jpeg bytes), rotation}]
ss.setdefault("up_key", 0)       # bumping this empties the file uploader
ss.setdefault("results", None)


def _jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def add_page(img: Image.Image, name: str) -> None:
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > STORED_MAX_SIDE:
        f = STORED_MAX_SIDE / max(img.size)
        img = img.resize((round(img.width * f), round(img.height * f)), Image.LANCZOS)
    thumb = img.copy()
    thumb.thumbnail((420, 420))
    ss.pages.append({
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "data": _jpeg(img, 95),
        "thumb": _jpeg(thumb, 80),
        "rotation": 0,
    })


def add_uploaded(files) -> None:
    from pdf2image import convert_from_bytes

    for f in files:
        if len(ss.pages) >= MAX_PAGES:
            st.warning(f"Page limit reached ({MAX_PAGES}). Some files were not added.")
            break
        try:
            raw = f.getvalue()
            if f.name.lower().endswith(".pdf"):
                for i, im in enumerate(convert_from_bytes(raw, dpi=250), start=1):
                    if len(ss.pages) >= MAX_PAGES:
                        break
                    add_page(im, f"{f.name} (p{i})")
            else:
                add_page(Image.open(io.BytesIO(raw)), f.name)
        except Exception as e:                                # noqa: BLE001
            st.warning(f"Couldn't read '{f.name}': {e}")


def _index_of(pid: str) -> int:
    return next(i for i, p in enumerate(ss.pages) if p["id"] == pid)


def cb_rotate(pid: str) -> None:
    p = ss.pages[_index_of(pid)]
    p["rotation"] = (p["rotation"] + 90) % 360
    ss.results = None


def cb_move(pid: str, delta: int) -> None:
    i = _index_of(pid)
    j = i + delta
    if 0 <= j < len(ss.pages):
        ss.pages[i], ss.pages[j] = ss.pages[j], ss.pages[i]
        ss.results = None


def cb_remove(pid: str) -> None:
    ss.pages.pop(_index_of(pid))
    ss.results = None


def cb_clear() -> None:
    ss.pages = []
    ss.results = None
    ss.up_key += 1


def story_flag(choice: str):
    return {AUTO: None, NEW_STORY: True, CONTINUES: False}[choice]


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
st.title("🔢 Word Numberer")
st.caption("Photograph story pages and get a PDF with a red number under every word. "
           "Numbering restarts at each story title.")

# ---- 1. add pages ---------------------------------------------------------- #
st.subheader("1 · Add pages")
st.caption("Take photos **in reading order** (one at a time is fine), or choose several "
           "from your library. PDFs work too. Flat, well-lit photos with the whole page "
           "visible give the best results.")
files = st.file_uploader(
    "Take a photo or choose files",
    type=["jpg", "jpeg", "png", "pdf"],
    accept_multiple_files=True,
    key=f"uploader_{ss.up_key}",
)
if files:
    with st.spinner("Adding pages…"):
        add_uploaded(files)
    ss.up_key += 1               # empties the uploader so the same files aren't re-added
    ss.results = None
    st.rerun()

# ---- 2. page list ---------------------------------------------------------- #
st.subheader(f"2 · Your pages ({len(ss.pages)})")
if not ss.pages:
    st.info("No pages yet. Add some photos above.")
else:
    for idx, p in enumerate(ss.pages):
        with st.container(border=True):
            left, right = st.columns([1, 2])
            with left:
                thumb = Image.open(io.BytesIO(p["thumb"]))
                if p["rotation"]:
                    thumb = thumb.rotate(-p["rotation"], expand=True)
                st.image(thumb, width=150)
            with right:
                st.markdown(f"**Page {idx + 1}**")
                st.caption(p["name"])
                st.selectbox("Story", STORY_CHOICES, key=f"story_{p['id']}",
                             label_visibility="collapsed")
                c1, c2, c3, c4 = st.columns(4)
                c1.button("⟳", key=f"rot_{p['id']}", help="Rotate 90° clockwise",
                          on_click=cb_rotate, args=(p["id"],))
                c2.button("↑", key=f"up_{p['id']}", help="Move earlier",
                          on_click=cb_move, args=(p["id"], -1), disabled=idx == 0)
                c3.button("↓", key=f"dn_{p['id']}", help="Move later",
                          on_click=cb_move, args=(p["id"], 1), disabled=idx == len(ss.pages) - 1)
                c4.button("🗑", key=f"rm_{p['id']}", help="Remove this page",
                          on_click=cb_remove, args=(p["id"],))
    st.button("Remove all pages", on_click=cb_clear)

# ---- 3. options ------------------------------------------------------------ #
with st.expander("Options"):
    color_hex = st.color_picker("Number colour", "#E00000")
    size = st.slider("Number size", 0.35, 0.70, 0.48, 0.01)
    straighten = st.checkbox("Straighten the page (for photos taken on a table or desk)", value=True)
    clean = st.checkbox("Remove shadows from the finished pages", value=True)
    expected_text = st.text_input(
        "Expected word counts (optional)", placeholder="e.g. 145, 188, 184, 79",
        help="One total per story, in order. The app will flag any story whose count differs.")

# ---- 4. run ---------------------------------------------------------------- #
st.subheader("3 · Number the words")
go = st.button("Number the words", type="primary", disabled=not ss.pages)

if go:
    opts = nw.Options(color=nw.hex_to_rgb(color_hex), font_frac=size,
                      straighten=straighten, clean_background=clean)
    flags = [story_flag(ss.get(f"story_{p['id']}", AUTO)) for p in ss.pages]
    rotations = [p["rotation"] for p in ss.pages]
    sources = [(lambda d=p["data"]: Image.open(io.BytesIO(d))) for p in ss.pages]
    bar = st.progress(0.0, text="Starting…")

    def _progress(i, n, msg):
        bar.progress(min(1.0, i / max(1, n)), text=msg)

    try:
        outs = nw.process_pages(sources, opts, story_flags=flags,
                                rotations=rotations, progress=_progress)
        ss.results = {"outs": outs, "pdf": nw.build_pdf(outs), "expected": expected_text}
    except Exception as e:                                    # noqa: BLE001
        ss.results = None
        st.error(f"Something went wrong while reading the pages: {e}")
    bar.empty()

# ---- 5. results ------------------------------------------------------------ #
res = ss.results
if res:
    outs = res["outs"]
    st.subheader("Results")
    totals = nw.story_totals(outs)
    expected = [int(x) for x in re.findall(r"\d+", res["expected"] or "")]

    rows, mismatches = [], []
    for k, s in enumerate(totals):
        exp = expected[k] if k < len(expected) else None
        if exp is None:
            check = ""
        elif exp == s["words"]:
            check = "✅ matches"
        else:
            check = f"⚠️ expected {exp}"
            mismatches.append((s, exp))
        pages = s["pages"]
        rows.append({
            "Story": s["story"],
            "Title": s["title"] or "(no title found)",
            "Pages": f"{pages[0]}–{pages[-1]}" if len(pages) > 1 else str(pages[0]),
            "Words": s["words"],
            "Check": check,
        })
    if rows:
        st.table(pd.DataFrame(rows).set_index("Story"))
    if len(expected) > len(totals):
        st.warning(f"You entered {len(expected)} expected counts but only {len(totals)} "
                   f"stories were found.")
    for s, exp in mismatches:
        st.warning(f"Story {s['story']} has {s['words']} words but you expected {exp}. "
                   f"Look through its pages below for a missed or extra word.")
    if not totals:
        st.error("No story text was found. Try flatter, brighter photos with the whole page in view.")

    blanks = [o.index + 1 for o in outs if o.is_blank]
    if blanks:
        st.info("No story text found on page(s) " + ", ".join(map(str, blanks)) +
                " - left unchanged (normal for a blank back page).")

    st.download_button("⬇️ Download numbered PDF", data=res["pdf"],
                       file_name="numbered_pages.pdf", mime="application/pdf", type="primary")

    for o in outs:
        label = f"Page {o.index + 1}"
        if o.title:
            label += f" · new story: {o.title}"
        elif o.is_blank:
            label += " · unchanged"
        else:
            label += f" · words {o.first_number}–{o.last_number}"
        st.image(o.jpeg, caption=label)
        if o.low_conf:
            with st.expander(f"Double-check {len(o.low_conf)} word(s) on page {o.index + 1}"):
                st.write(", ".join(f"#{w['n']} “{w['word']}”" for w in o.low_conf))

st.divider()
st.caption("Photos are processed in memory and are not saved by this app.")
