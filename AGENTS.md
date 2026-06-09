# AGENTS.md — AI Agent Guide

## Project Overview

A running-race bib number recognition and photo search system.
Users upload race photos → OCR detects bib numbers → runners search by their number to find photos of themselves.

## Architecture

```
Client (browser)
    │
    ├── GET  /                              → index.html (single-page app)
    │
    ├── Auth
    │   ├── POST /api/auth/register         → create account
    │   ├── POST /api/auth/login            → set HttpOnly session cookie
    │   ├── POST /api/auth/logout           → clear session cookie
    │   ├── GET  /api/auth/me               → current user info
    │   └── PUT  /api/auth/me              → update profile / password
    │
    ├── Albums
    │   ├── GET    /api/albums              → list albums with photo counts
    │   ├── POST   /api/albums             → create album (auth required)
    │   ├── DELETE /api/albums/{id}         → delete album + photos (owner/admin)
    │   └── GET    /api/albums/{id}/progress → OCR pending/done/failed counts
    │
    ├── Photos
    │   ├── POST   /api/upload             → save files + OCR background task
    │   ├── GET    /api/photostream        → paginated photos (album_id, unrecognized filters)
    │   ├── DELETE /api/photos/{id}         → delete single photo (owner/admin)
    │   ├── GET    /api/photos/{id}/download → download original file
    │   ├── POST   /api/reprocess          → re-run OCR on existing photos
    │   └── GET    /api/search?bib=&album_id= → search by bib number
    │
    ├── Site Settings (admin only)
    │   ├── GET    /api/settings           → site title + logo_url
    │   ├── PUT    /api/settings           → update site title
    │   ├── POST   /api/settings/logo      → upload logo image
    │   └── DELETE /api/settings/logo      → remove logo
    │
    ├── Admin
    │   ├── GET    /api/admin/users        → list all users
    │   ├── PUT    /api/admin/users/{id}   → edit user (display_name, email, role, password)
    │   ├── DELETE /api/admin/users/{id}   → delete user
    │   └── POST   /api/admin/factory-reset → wipe all data (password + phrase required)
    │
    ├── GET /api/stats                     → system-wide counts
    ├── GET /api/health                    → ok + GPU info
    └── GET /uploads/*                     → serve stored images (static)

FastAPI (main.py)
    ├── ocr_engine.py  → image preprocessing + EasyOCR / Tesseract / cloud OCR
    ├── models.py      → SQLAlchemy ORM (User, Album, Photo, BibNumber)
    └── database.py    → engine, session factory, Base
```

## File Map

| File | Role |
|------|------|
| `main.py` | FastAPI app, all API routes, startup init, SQLite migration |
| `ocr_engine.py` | Public function `extract_bib_numbers(path, use_claude_api, use_vision_api)` |
| `database.py` | `engine`, `SessionLocal`, `Base`, `get_db()` |
| `models.py` | `User`, `Album`, `Photo`, `BibNumber` ORM models |
| `index.html` | Single-page app (state machine, no framework, no build step) |
| `static/` | Stores logo image served at `/static/logo.*` |
| `uploads/` | Auto-created at startup; stores uploaded images |
| `settings.json` | Persisted site settings: `site_title`, `logo_url` |
| `bib_recognition.db` | SQLite file; auto-created at startup |

## Database Schema

```
users
  id               INTEGER PK
  display_name     TEXT NOT NULL       (shown in UI)
  username         TEXT UNIQUE         (used for login)
  email            TEXT UNIQUE
  hashed_password  TEXT NOT NULL       (bcrypt)
  role             TEXT                ("user" | "admin")
  created_at       DATETIME

albums
  id               INTEGER PK
  name             TEXT NOT NULL
  event_date       TEXT                (ISO date string "2025-10-01", nullable)
  description      TEXT                (nullable)
  created_at       DATETIME
  owner_id         INTEGER FK → users.id (nullable)

photos
  id               INTEGER PK
  filename         TEXT UNIQUE         (UUID-based stored name)
  original_filename TEXT
  upload_time      DATETIME
  album_id         INTEGER FK → albums.id   (nullable — photos can exist without album)
  ocr_status       TEXT                ("pending" | "done" | "failed")
  storage_url      TEXT                (Cloudinary URL; NULL = local disk)

bib_numbers
  id               INTEGER PK
  photo_id         INTEGER FK → photos.id
  bib_number       TEXT INDEX          (e.g. "1234"; leading zeros preserved)
```

**Unrecognized photos** = photos with no associated `bib_numbers` rows.
Query pattern: `filter(~exists().where(BibNumber.photo_id == Photo.id))`

One photo → many bib_number rows (one race photo can contain multiple runners).

## OCR Pipeline (ocr_engine.py)

### Primary: EasyOCR (deep learning)

1. `_load_and_resize`: scale up if width < 800px, down if > 2000px
2. `_enhance_contrast`: CLAHE on LAB luminance channel
3. **Pass 1** — full image via `_ocr_easyocr`:
   - Uses CRAFT text detector + allowlist='0123456789'
   - Low-confidence detections with valid bib proportions → `_recheck_low_confidence` (zoomed crop)
4. **Pass 2** — colour-segmented bib crops via `_detect_bib_regions`:
   - HSV segmentation (white body + blue header), contour filtering
   - Wide contours (multiple bibs) split by `_split_bib_region`
   - Each crop OCR'd raw + contrast-enhanced
5. Post-merge filter: 5-digit strings that start with a known 4-digit bib are discarded

### Fallback: Tesseract (CPU)

1. Same resize/load step
2. `_preprocess_variants` generates **5** binary variants per image:
   - Otsu binarization
   - Inverted Otsu
   - Adaptive Gaussian threshold
   - Morphological closing
   - Red-channel-to-black mask (`_red_to_black`)
3. Run Tesseract with PSM modes 11, 6, 3 and digit whitelist
4. Collect all `\d+` matches → filter by `_is_valid_bib()`

### Optional Cloud Backends

- **Claude Vision API** (`use_claude_api=True`, needs `ANTHROPIC_API_KEY`)
- **Google Cloud Vision** (`use_vision_api=True`, needs `GOOGLE_APPLICATION_CREDENTIALS`)

### Bib Validation (`_is_valid_bib`)

- Must be all digits
- Length 2–5 (leading zeros allowed, e.g. "0425")
- Excludes calendar years 1900–2099

## Authentication

- JWT tokens, 7-day expiry, stored as HttpOnly + Secure + SameSite=Strict cookie
- Cookie name: `rb_session`; never exposed to JavaScript
- `localStorage` holds only non-sensitive display info (name, role)
- First registered user auto-promoted to `admin`
- `_require_user()` FastAPI dependency raises 401 if no valid session

## Key Conventions

- All API routes are prefixed `/api/`.
- Uploaded files are stored under `uploads/` with UUID filenames.
- DB session is injected via `Depends(get_db)` — never instantiate `SessionLocal` directly in routes.
- `Base.metadata.create_all()` + `_migrate()` run at startup — handles SQLite schema evolution.
- Security headers middleware applies CSP, X-Frame-Options, HSTS, etc. to every response.
- The frontend is vanilla HTML/JS (no framework, no build). `credentials: 'include'` on every fetch to send the HttpOnly cookie.
- Album "id=0" is a virtual album representing orphaned (uncategorized) photos.

## Running Locally

```powershell
# First time
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt

# Every time
venv\Scripts\uvicorn.exe main:app --reload --host 0.0.0.0 --port 8000
```

Tesseract must be installed separately: `winget install UB-Mannheim.TesseractOCR`

## Extending the Project

**Switch to PostgreSQL**: set `DATABASE_URL` in `.env`; the `postgres://` → `postgresql://` rewrite is already in `database.py`.

**Enable GPU (EasyOCR)**: `pip install torch --index-url https://download.pytorch.org/whl/cu121 && pip install easyocr` — auto-detected at startup.

**Enable Cloudinary**: set `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` env vars. After upload, local file is deleted once safely stored on Cloudinary.

**Add Claude Vision**: set `ANTHROPIC_API_KEY`; call `extract_bib_numbers(path, use_claude_api=True)`.

**Add Google Vision**: install `google-cloud-vision`, set `GOOGLE_APPLICATION_CREDENTIALS`, call `extract_bib_numbers(path, use_vision_api=True)`.

**Pagination on search**: add `skip`/`limit` query params to `GET /api/search` and pass to SQLAlchemy query.
