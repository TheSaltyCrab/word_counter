# Word Numberer

Photograph (or upload) pages of a story and get a PDF with a **red number under
every word**. Numbering restarts at each story title and carries across the pages
of a story. Titles, sight-word header bars, footers, shadows and blank back pages
are skipped automatically.

## Files

| File | Purpose |
|---|---|
| `app.py` | The Streamlit web app (phone-friendly, password protected) |
| `number_words.py` | The engine (OCR, layout analysis, numbering). Also a command-line tool |
| `requirements.txt` | Python packages |
| `packages.txt` | System packages (Tesseract OCR, Poppler) - read by Streamlit Community Cloud |
| `Dockerfile` | Optional, for Render / Cloud Run / Fly.io |
| `.streamlit/` | Settings and an example secrets file |

## Deploy on Streamlit Community Cloud (free)

1. Create a GitHub repository (it can be private) and upload all of these files.
   **Do not upload a real `secrets.toml`** (it is git-ignored).
2. Go to <https://share.streamlit.io>, sign in with GitHub, and choose **Create app**.
3. Pick your repository, branch `main`, and main file `app.py`.
4. Open **Advanced settings -> Secrets** and paste:
   ```toml
   APP_PASSWORD = "choose-a-long-passphrase"
   ```
5. Click **Deploy**. The first build installs Tesseract and takes a few minutes.
6. On your iPhone, open the app's address in Safari, then **Share -> Add to Home Screen**
   so it launches like an app.

The app refuses to run unless `APP_PASSWORD` is set, so it is never open to the public
by accident. Free apps go to sleep when idle; the first visit afterwards may need a
click and a short wait to wake it.

## Using it

1. **Add pages** - tap the upload box and choose *Take Photo* (one page at a time, in
   reading order) or pick several from your library. PDFs work too.
2. Check the page list. Use the rotate/move/remove buttons if needed. If a story is not
   detected correctly, set that page to *Starts a new story* or *Continues previous story*.
3. (Optional) In **Options**, type the expected totals, e.g. `145, 188, 184, 79`. The app
   flags any story whose count differs.
4. Tap **Number the words**, review the pages, then **Download numbered PDF**.

### Photo tips
- Whole page in frame, held flat, lit evenly, camera directly above.
- A plain, darker surface behind the page lets the app find the page edges and straighten it.
- Avoid your own shadow falling across the text.

## Run locally

```bash
pip install -r requirements.txt
# also install Tesseract and Poppler:  brew install tesseract poppler   (macOS)
ALLOW_NO_PASSWORD=1 streamlit run app.py
```

Command-line (PDF in, PDF out):

```bash
python number_words.py input.pdf output.pdf
python number_words.py input.pdf output.pdf --new-story-pages 1,3,5,7
python number_words.py photo_scans.pdf output.pdf --straighten --clean
```

## Container hosts (Render, Cloud Run, ...)

Build the included `Dockerfile` and set `APP_PASSWORD` as an environment variable.

## Notes and limits

- Tesseract's version on the host can differ slightly from the one this was developed
  against, which can change a word or two. Use the *expected counts* box to catch that.
- Titles are detected as a short, centred first line. A title that wraps onto two lines
  will need the *Starts a new story* setting and may leave a stray word numbered.
- Handwriting, columns and non-English text are not handled.
