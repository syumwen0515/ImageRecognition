# AGENTS.md — AI Agent Guide

## Project Overview

A running-race bib number recognition and photo search system.
Users upload race photos → OCR detects bib numbers → runners search by their number to find photos of themselves.

## Architecture

```
Client (browser)
    │
    ├── GET  /                          → frontend/index.html (static)
    ├── GET  /api/albums                → list all albums
    ├── POST /api/albums                → create album
    ├── GET  /api/photostream           → paginated photos (album_id, unrecognized filters)
    ├── POST /api/upload                → save + OCR + write DB (accepts album_id form field)
    ├── GET  /api/search?bib=&album_id= → search by bib number
    ├── GET  /api/photos/{id}/download  → download original file
    ├── POST /api/reprocess             → re-run OCR on existing photos (album_id, unrecognized_only)
    └── GET  /uploads/*                 → serves stored images (static)

FastAPI (main.py)
    ├── ocr_engine.py  → image preprocessing + Tesseract OCR
    ├── models.py      → SQLAlchemy ORM (Album, Photo, BibNumber)
    └── database.py    → engine, session factory, Base
```

## File Map

| File | Role |
|------|------|
| `main.py` | FastAPI app, all API routes, startup init, SQLite migration |
| `ocr_engine.py` | Public function `extract_bib_numbers(path, use_vision_api)` |
| `database.py` | `engine`, `SessionLocal`, `Base`, `get_db()` |
| `models.py` | `Album`, `Photo`, `BibNumber` ORM models |
| `frontend/index.html` | Single-page app (state machine, no framework, no build step) |
| `uploads/` | Auto-created at startup; stores uploaded images |
| `bib_recognition.db` | SQLite file; auto-created at startup |

## Database Schema

```
albums
  id               INTEGER PK
  name             TEXT NOT NULL
  event_date       TEXT            (ISO date string "2025-10-01", nullable)
  description      TEXT            (nullable)
  created_at       DATETIME

photos
  id               INTEGER PK
  filename         TEXT UNIQUE     (UUID-based stored name)
  original_filename TEXT
  upload_time      DATETIME
  album_id         INTEGER FK → albums.id   (nullable — photos can exist without album)

bib_numbers
  id               INTEGER PK
  photo_id         INTEGER FK → photos.id
  bib_number       TEXT INDEX      (e.g. "1234")
```

**Unrecognized photos** = photos with no associated `bib_numbers` rows.
Query pattern: `filter(~exists().where(BibNumber.photo_id == Photo.id))`

One photo → many bib_number rows (one race photo can contain multiple runners).

## OCR Pipeline (ocr_engine.py)

1. Resize: scale up if width < 800px, scale down if > 2000px
2. Grayscale conversion
3. CLAHE adaptive contrast enhancement
4. Gaussian blur (noise reduction)
5. Generate 4 binary variants: Otsu / Inverted Otsu / Adaptive / Morphological close
6. Run Tesseract (`--psm 11`, `--psm 6`, `--psm 3`) on each variant with digit whitelist
7. Collect all regex `\d+` matches, filter by `_is_valid_bib()`:
   - Length 2–5 digits (leading zeros allowed, e.g. "0425")
   - Not a calendar year (1900–2099)
8. Return sorted unique list

Google Cloud Vision is available as an alternative; set `use_vision_api=True` and ensure `GOOGLE_APPLICATION_CREDENTIALS` is set.

## Key Conventions

- All API routes are prefixed `/api/`.
- Uploaded files are stored under `uploads/` with UUID filenames to avoid collisions.
- The DB session is injected via `Depends(get_db)` — never instantiate `SessionLocal` directly in routes.
- `Base.metadata.create_all()` runs at import time in `main.py` — no separate migration step for SQLite.
- The frontend is vanilla HTML/JS (no framework, no build). `API` constant at the top of the script block is `''` (same origin).

## Running Locally

```powershell
# First time
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt

# Every time
venv\Scripts\uvicorn.exe main:app --reload --host 0.0.0.0 --port 8000
```

Tesseract must be installed separately: `winget install UB-Mannheim.TesseractOCR`

If Tesseract is not on PATH, uncomment and set line 14 of `ocr_engine.py`:
```python
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
```

## Extending the Project

**Switch to PostgreSQL**: change `DATABASE_URL` in `database.py` (or via `.env`); remove the `check_same_thread` connect arg.

**Add Google Vision**: install `google-cloud-vision`, set `GOOGLE_APPLICATION_CREDENTIALS`, call `extract_bib_numbers(path, use_vision_api=True)`.

**Batch upload endpoint**: `/api/upload` already accepts a list of files (`List[UploadFile]`).

**Pagination on search**: add `skip` / `limit` query params to `GET /api/search` and pass them to the SQLAlchemy query.
