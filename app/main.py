# main.py — FastAPI application entry point
from __future__ import annotations

import io
import os
import re
import uuid
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import json

from PIL import Image, ImageOps

from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
import bcrypt
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import case, exists, func, text
from sqlalchemy.orm import Session, selectinload

from database import Base, SessionLocal, engine, get_db
from models import Album, BibNumber, Photo, User
from ocr_engine import extract_bib_numbers, gpu_info

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Cloudinary (optional cloud image storage) ─────────────────────────────────

_CLOUDINARY_CONFIGURED = False

try:
    import cloudinary
    import cloudinary.uploader
    _cl_name   = os.getenv("CLOUDINARY_CLOUD_NAME")
    _cl_key    = os.getenv("CLOUDINARY_API_KEY")
    _cl_secret = os.getenv("CLOUDINARY_API_SECRET")
    if _cl_name and _cl_key and _cl_secret:
        cloudinary.config(cloud_name=_cl_name, api_key=_cl_key, api_secret=_cl_secret, secure=True)
        _CLOUDINARY_CONFIGURED = True
except ImportError:
    pass


def _cloudinary_upload(file_path: str, public_id: str) -> Optional[str]:
    """Upload file to Cloudinary; returns the secure URL or None on failure."""
    if not _CLOUDINARY_CONFIGURED:
        return None
    try:
        result = cloudinary.uploader.upload(
            file_path,
            public_id=public_id,
            resource_type="image",
            overwrite=True,
        )
        return result.get("secure_url")
    except Exception:
        return None


def _cloudinary_delete(public_id: str) -> None:
    if not _CLOUDINARY_CONFIGURED:
        return
    try:
        cloudinary.uploader.destroy(public_id, resource_type="image")
    except Exception:
        pass


def _load_secret_key() -> str:
    """Load the JWT signing key from $SECRET_KEY, or fall back to a locally
    generated one persisted in .secret_key.

    A hardcoded default would let anyone who has read the source forge valid
    auth tokens, so we never fall back to a fixed string — only to a randomly
    generated, per-install secret that stays stable across restarts.
    """
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key

    key_file = Path(".secret_key")
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()

    key = uuid.uuid4().hex + uuid.uuid4().hex
    key_file.write_text(key, encoding="utf-8")
    try:
        key_file.chmod(0o600)  # owner read/write only — prevents other OS users from reading the JWT signing key
    except Exception:
        pass
    return key


# ── Config ────────────────────────────────────────────────────────────────────
SECRET_KEY = _load_secret_key()
ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 7

bearer_scheme = HTTPBearer(auto_error=False)

# ── Initialisation ────────────────────────────────────────────────────────────
Base.metadata.create_all(bind=engine)


def _migrate():
    # PRAGMA table_info is SQLite-only; PostgreSQL gets a fresh schema via create_all
    if engine.dialect.name != "sqlite":
        return
    with engine.connect() as conn:
        # v1 → v2: add album_id to photos
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(photos)")).fetchall()]
        if "album_id" not in cols:
            conn.execute(text("ALTER TABLE photos ADD COLUMN album_id INTEGER REFERENCES albums(id)"))
            conn.commit()
        # v2 → v3: add owner_id to albums
        acols = [r[1] for r in conn.execute(text("PRAGMA table_info(albums)")).fetchall()]
        if "owner_id" not in acols:
            conn.execute(text("ALTER TABLE albums ADD COLUMN owner_id INTEGER REFERENCES users(id)"))
            conn.commit()
        # v3 → v4: add display_name to users (default to username for existing rows)
        ucols = [r[1] for r in conn.execute(text("PRAGMA table_info(users)")).fetchall()]
        if "display_name" not in ucols:
            conn.execute(text("ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"))
            conn.execute(text("UPDATE users SET display_name = username WHERE display_name = ''"))
            conn.commit()
        # v4 → v5: add ocr_status to photos (existing photos treated as already processed)
        pcols = [r[1] for r in conn.execute(text("PRAGMA table_info(photos)")).fetchall()]
        if "ocr_status" not in pcols:
            conn.execute(text("ALTER TABLE photos ADD COLUMN ocr_status TEXT NOT NULL DEFAULT 'done'"))
            conn.commit()
        # v5 → v6: add storage_url to photos (Cloudinary or other remote storage URL)
        pcols = [r[1] for r in conn.execute(text("PRAGMA table_info(photos)")).fetchall()]
        if "storage_url" not in pcols:
            conn.execute(text("ALTER TABLE photos ADD COLUMN storage_url TEXT"))
            conn.commit()


_migrate()

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)

SETTINGS_FILE = Path("settings.json")

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}

# Storage extension is derived from the *verified* image format — never from the
# client-supplied filename or Content-Type header (both are attacker-controlled
# and could otherwise be used to smuggle e.g. .svg/.html files that browsers
# would render inline from /uploads, leading to stored XSS).
# MPO ("Multi Picture Object") is the JPEG-based container some cameras (e.g.
# Canon in-camera HDR / multi-shot noise reduction modes) save photos in. Pillow
# reports it as format "MPO" rather than "JPEG", but decodes/converts it the
# same way, so it's treated as a JPEG for storage purposes.
EXT_BY_FORMAT = {"JPEG": ".jpg", "MPO": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "BMP": ".bmp"}

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_SIZE_MB", "20")) * 1024 * 1024
WEBP_QUALITY = int(os.getenv("WEBP_QUALITY", "85"))
THUMB_MAX_PX = int(os.getenv("THUMB_MAX_PX", "600"))


def _verify_image(raw: bytes) -> Optional[str]:
    """Return the verified image format (e.g. 'JPEG'), or None if not a real image."""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.verify()
            return img.format
    except Exception:
        # img.verify() can reject some valid images (e.g. JPEG variants from
        # certain cameras carrying extra data) that Pillow can still decode
        # fully, so fall back to a full decode before giving up.
        try:
            with Image.open(io.BytesIO(raw)) as img:
                fmt = img.format
                img.load()
                return fmt
        except Exception:
            return None


# Allowed MIME types and PIL format names for favicon uploads.
# SVG is intentionally excluded: SVG files can embed <script> tags and would
# constitute a stored-XSS vector if served from the same origin.
_FAVICON_ALLOWED_MIME = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/x-icon",           # de-facto ICO MIME type (sent by most browsers)
    "image/vnd.microsoft.icon", # official IANA MIME type for ICO
}
_FAVICON_ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "BMP", "ICO"}


def _verify_image_favicon(raw: bytes) -> Optional[str]:
    """Like _verify_image but safely handles ICO files.

    Pillow's img.verify() raises NotImplementedError for ICO in some versions,
    so ICO is validated by attempting a full decode instead.
    Returns the PIL format string on success, or None if the bytes are not a
    recognised, decodable image.
    """
    try:
        with Image.open(io.BytesIO(raw)) as img:
            fmt = img.format
            if fmt not in _FAVICON_ALLOWED_FORMATS:
                return None
            if fmt == "ICO":
                # verify() is unreliable for ICO; force a full decode instead
                img.load()
            else:
                img.verify()
            return fmt
    except Exception:
        return None


def _to_webp(raw: bytes, max_width: int = 0) -> bytes:
    """Convert image bytes to WebP, applying EXIF orientation and stripping metadata.
    If max_width > 0 and image is wider, downscale proportionally."""
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode == "P":
            img = img.convert("RGBA")
        elif img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        if max_width > 0 and img.width > max_width:
            new_h = int(img.height * max_width / img.width)
            img = img.resize((max_width, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=WEBP_QUALITY, method=4)
        return buf.getvalue()


def _load_settings() -> dict:
    defaults: dict = {"site_title": "路跑相簿", "logo_url": None, "favicon_url": None}
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            # Merge: saved values override defaults; keys absent in older installs
            # (e.g. favicon_url) fall back gracefully to the default value.
            return {**defaults, **saved}
        except Exception:
            pass
    return defaults


def _save_settings(s: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


PAGE_LIMIT = 24

# Cookie configuration — disable COOKIE_SECURE only in local HTTP dev environments
COOKIE_NAME = "rb_session"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() != "false"

# ── Security Headers Middleware ────────────────────────────────────────────────
# Addresses OWASP ZAP findings: Missing CSP, X-Frame-Options, X-Content-Type-Options, HSTS
_CSP = (
    "default-src 'self'; "
    # Tailwind CDN and its runtime-injected <style> blocks require 'unsafe-inline'
    # for styles. For scripts, we allow the CDN origin only.
    "script-src 'self' https://cdn.tailwindcss.com 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    # blob: needed for in-browser image preview; data: for small inline images
    "img-src 'self' data: blob:; "
    # Only connect back to own origin — blocks XSS-driven data exfiltration
    "connect-src 'self'; "
    # Replaces X-Frame-Options in CSP Level 2 but we send both for older browsers
    "frame-ancestors 'none'; "
    "form-action 'self';"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # HSTS: disable with ENABLE_HSTS=false in local HTTP dev environments
        if os.getenv("ENABLE_HSTS", "true").lower() != "false":
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

        path = request.url.path
        if path.startswith("/uploads/"):
            # UUID filenames are content-addressable and never mutate
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith("/static/"):
            # Logo/static assets may be replaced, so allow revalidation after 1 day
            response.headers["Cache-Control"] = "public, max-age=86400"
        elif path == "/" or path.endswith(".html"):
            response.headers["Cache-Control"] = "no-cache"

        return response


app = FastAPI(title="Bib Number Recognition API", version="3.0.0")

# SecurityHeadersMiddleware must be added first so headers are applied to every response.
app.add_middleware(SecurityHeadersMiddleware)

app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # Auth uses HttpOnly cookies (same-origin) — cross-origin credentialed requests
    # are not needed. allow_credentials=True with wildcard origin is rejected by
    # browsers anyway; keep it off.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Auth helpers ───────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    display_name: str
    username: str
    email: str
    password: str


class LoginRequest(BaseModel):
    identifier: str   # username or email
    password: str


class UpdateProfileRequest(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    current_password: Optional[str] = None
    new_password: Optional[str] = None


class SiteSettingsRequest(BaseModel):
    site_title: Optional[str] = None


class AdminUpdateUserRequest(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    new_password: Optional[str] = None


class FactoryResetRequest(BaseModel):
    password: str
    confirm_phrase: str


# Type-to-confirm phrase the admin must enter verbatim before a factory reset is
# allowed to proceed — guards against a single misclick triggering data loss.
RESET_CONFIRM_PHRASE = "確認重置"


def _hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def _verify_password(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())


def _create_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
    return jwt.encode({"sub": str(user_id), "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def _set_session_cookie(response: JSONResponse, token: str) -> None:
    """Attach the JWT as an HttpOnly Secure SameSite=Strict session cookie.

    HttpOnly prevents JavaScript from reading the value (eliminates XSS token theft).
    Secure ensures it is only transmitted over TLS.
    SameSite=Strict prevents the browser from attaching it on cross-site requests
    (eliminates CSRF without needing a separate CSRF token).
    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        max_age=TOKEN_EXPIRE_DAYS * 86_400,
        path="/",
    )


def _clear_session_cookie(response: JSONResponse) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/", httponly=True, secure=COOKIE_SECURE, samesite="strict")


def _get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    rb_session: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    # Prefer HttpOnly cookie; fall back to Bearer token for API/CLI clients
    raw_token = rb_session or (creds.credentials if creds else None)
    if not raw_token:
        return None
    try:
        payload = jwt.decode(raw_token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            return None
        return db.query(User).filter(User.id == int(user_id)).first()
    except JWTError:
        return None


def _require_user(user: Optional[User] = Depends(_get_current_user)) -> User:
    if not user:
        raise HTTPException(401, "Login required")
    return user


def _user_dict(u: User) -> dict:
    return {"id": u.id, "display_name": u.display_name, "username": u.username, "email": u.email, "role": u.role}


def _user_dict_admin(u: User) -> dict:
    return {
        "id": u.id,
        "display_name": u.display_name,
        "username": u.username,
        "email": u.email,
        "role": u.role,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


# ── Auth endpoints ─────────────────────────────────────────────────────────────

@app.post("/api/auth/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    req.display_name = req.display_name.strip()
    req.username = req.username.strip()
    req.email = req.email.strip().lower()
    if len(req.display_name) < 1:
        raise HTTPException(400, "顯示名稱不得為空")
    if len(req.username) < 2:
        raise HTTPException(400, "帳號至少需要 2 個字元")
    if len(req.username) > 32:
        raise HTTPException(400, "帳號最多 32 個字元")
    if not re.fullmatch(r"[A-Za-z0-9_\-\.]+", req.username):
        raise HTTPException(400, "帳號只能使用英文字母、數字及 _ - . 符號")
    if len(req.password) < 6:
        raise HTTPException(400, "密碼至少需要 6 個字元")
    if db.query(User).filter(User.username == req.username).first():
        raise HTTPException(409, "帳號已被使用")
    if db.query(User).filter(User.email == req.email).first():
        raise HTTPException(409, "Email 已被註冊")

    role = "admin" if db.query(User).count() == 0 else "user"
    user = User(
        display_name=req.display_name,
        username=req.username,
        email=req.email,
        hashed_password=_hash_password(req.password),
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = _create_token(user.id)
    # Token is delivered only as an HttpOnly cookie — NOT in the response body.
    # This eliminates the XSS attack surface where malicious scripts read localStorage.
    resp = JSONResponse({"user": _user_dict(user)})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    identifier = req.identifier.strip()
    user = db.query(User).filter(User.username == identifier).first()
    if not user:
        user = db.query(User).filter(User.email == identifier.lower()).first()
    if not user or not _verify_password(req.password, user.hashed_password):
        raise HTTPException(401, "帳號/Email 或密碼錯誤")
    token = _create_token(user.id)
    resp = JSONResponse({"user": _user_dict(user)})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/logout")
def logout():
    """Clear the session cookie so the browser discards the JWT."""
    resp = JSONResponse({"logged_out": True})
    _clear_session_cookie(resp)
    return resp


@app.get("/api/auth/me")
def get_me(user: User = Depends(_require_user)):
    return _user_dict(user)


@app.put("/api/auth/me")
def update_me(
    req: UpdateProfileRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_user),
):
    if req.display_name is not None:
        name = req.display_name.strip()
        if not name:
            raise HTTPException(400, "顯示名稱不得為空")
        current_user.display_name = name

    if req.email is not None:
        email = req.email.strip().lower()
        if email != current_user.email:
            if db.query(User).filter(User.email == email, User.id != current_user.id).first():
                raise HTTPException(409, "Email 已被其他帳號使用")
            current_user.email = email

    if req.new_password:
        if not req.current_password:
            raise HTTPException(400, "請輸入目前密碼")
        if not _verify_password(req.current_password, current_user.hashed_password):
            raise HTTPException(400, "目前密碼不正確")
        if len(req.new_password) < 6:
            raise HTTPException(400, "新密碼至少需要 6 個字元")
        current_user.hashed_password = _hash_password(req.new_password)

    db.commit()
    db.refresh(current_user)
    return _user_dict(current_user)


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def serve_index():
    return FileResponse("index.html")


# ── Albums ────────────────────────────────────────────────────────────────────

@app.get("/api/albums", summary="List all race albums")
def list_albums(db: Session = Depends(get_db)):
    albums = (
        db.query(Album)
        .options(selectinload(Album.owner))
        .order_by(Album.event_date.desc(), Album.created_at.desc())
        .all()
    )

    # Aggregate photo counts in 2 queries instead of 2×N
    has_bib = db.query(BibNumber.photo_id).distinct().subquery("has_bib")
    stats_rows = (
        db.query(
            Photo.album_id,
            func.count(Photo.id).label("total"),
            func.count(case((has_bib.c.photo_id.is_(None), Photo.id))).label("unrecognized"),
        )
        .outerjoin(has_bib, has_bib.c.photo_id == Photo.id)
        .group_by(Photo.album_id)
        .all()
    )
    stats = {row.album_id: (row.total, row.unrecognized) for row in stats_rows}

    result = []
    for a in albums:
        total, unrecognized = stats.get(a.id, (0, 0))
        result.append({
            "id": a.id,
            "name": a.name,
            "event_date": a.event_date,
            "description": a.description,
            "photo_count": total,
            "unrecognized_count": unrecognized,
            "created_at": a.created_at.isoformat(),
            "owner_id": a.owner_id,
            "owner_username": a.owner.username if a.owner else None,
        })

    orphaned, orphaned_unrec = stats.get(None, (0, 0))
    if orphaned > 0:
        result.append({
            "id": 0,
            "name": "未分類照片",
            "event_date": None,
            "description": "尚未歸入相簿的照片",
            "photo_count": orphaned,
            "unrecognized_count": orphaned_unrec,
            "created_at": None,
            "is_uncategorized": True,
            "owner_id": None,
            "owner_username": None,
        })

    return result


@app.post("/api/albums", summary="Create a new race album")
def create_album(
    name: str = Form(...),
    event_date: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_user),
):
    album = Album(
        name=name,
        event_date=event_date,
        description=description,
        owner_id=current_user.id,
    )
    db.add(album)
    db.commit()
    db.refresh(album)
    return {
        "id": album.id,
        "name": album.name,
        "event_date": album.event_date,
        "owner_id": album.owner_id,
    }


@app.delete("/api/albums/{album_id}", summary="Delete an album and all its photos")
def delete_album(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_user),
):
    album = db.query(Album).filter(Album.id == album_id).first()
    if not album:
        raise HTTPException(404, "Album not found")
    if current_user.role != "admin" and album.owner_id != current_user.id:
        raise HTTPException(403, "無權刪除此相簿")

    photos = db.query(Photo).filter(Photo.album_id == album_id).all()
    for photo in photos:
        if photo.storage_url:
            _cloudinary_delete(Path(photo.filename).stem)
        path = UPLOAD_DIR / photo.filename
        if path.exists():
            path.unlink()
        thumb_path = UPLOAD_DIR / photo.filename.replace(".webp", "_thumb.webp")
        if thumb_path.exists():
            thumb_path.unlink()
        db.delete(photo)

    db.delete(album)
    db.commit()
    return {"deleted": True, "album_id": album_id}


# ── Photos ────────────────────────────────────────────────────────────────────

@app.delete("/api/photos/{photo_id}", summary="Delete a single photo")
def delete_photo(
    photo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_user),
):
    photo = db.query(Photo).filter(Photo.id == photo_id).first()
    if not photo:
        raise HTTPException(404, "Photo not found")

    if current_user.role != "admin":
        if photo.album_id is None:
            raise HTTPException(403, "無權刪除此照片")
        album = db.query(Album).filter(Album.id == photo.album_id).first()
        if not album or album.owner_id != current_user.id:
            raise HTTPException(403, "無權刪除此照片")

    if photo.storage_url:
        _cloudinary_delete(Path(photo.filename).stem)
    path = UPLOAD_DIR / photo.filename
    if path.exists():
        path.unlink()
    thumb_path = UPLOAD_DIR / photo.filename.replace(".webp", "_thumb.webp")
    if thumb_path.exists():
        thumb_path.unlink()
    db.delete(photo)
    db.commit()
    return {"deleted": True, "photo_id": photo_id}


# ── Photostream ───────────────────────────────────────────────────────────────

@app.get("/api/photostream", summary="Paginated photo stream")
def photostream(
    page: int = 1,
    limit: int = PAGE_LIMIT,
    album_id: Optional[int] = None,
    unrecognized: bool = False,
    db: Session = Depends(get_db),
):
    query = db.query(Photo)
    if album_id is not None:
        if album_id == 0:
            query = query.filter(Photo.album_id == None)  # noqa: E711
        else:
            query = query.filter(Photo.album_id == album_id)
    if unrecognized:
        query = query.filter(~exists().where(BibNumber.photo_id == Photo.id))

    total = query.count()
    photos = (
        query.options(selectinload(Photo.bib_numbers))
        .order_by(Photo.upload_time.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "limit": limit,
        "has_more": page * limit < total,
        "photos": [_photo_dict(p) for p in photos],
    }


# ── Upload ────────────────────────────────────────────────────────────────────

@app.post("/api/upload", summary="Upload race photos (OCR runs in background)")
async def upload_photos(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    album_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_user),
):
    if not files:
        raise HTTPException(400, "No files provided")

    if album_id is not None:
        album = db.query(Album).filter(Album.id == album_id).first()
        if not album:
            raise HTTPException(404, f"Album {album_id} not found")
        if current_user.role != "admin" and album.owner_id != current_user.id:
            raise HTTPException(403, "無權上傳到此相簿")

    results = []
    for upload in files:
        if upload.content_type not in ALLOWED_TYPES:
            results.append({"original_filename": upload.filename, "success": False,
                            "error": "不支援的檔案格式（請使用 JPG、PNG 或 WebP）"})
            continue

        raw = await upload.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            results.append({"original_filename": upload.filename, "success": False,
                            "error": f"檔案過大（上限 {MAX_UPLOAD_BYTES // (1024*1024)} MB）"})
            continue

        # Trust neither the filename extension nor the Content-Type header (both
        # are client-controlled) — verify the bytes are actually a decodable image
        # and pick the stored extension from the verified format.
        fmt = _verify_image(raw)
        if not EXT_BY_FORMAT.get(fmt or ""):
            results.append({"original_filename": upload.filename, "success": False,
                            "error": "檔案內容不是有效的圖片"})
            continue

        uid = uuid.uuid4()
        stored_name = f"{uid}.webp"
        dest = UPLOAD_DIR / stored_name

        photo = Photo(
            filename=stored_name,
            original_filename=upload.filename,
            upload_time=datetime.utcnow(),
            album_id=album_id,
            ocr_status="pending",
        )
        db.add(photo)
        db.flush()
        db.commit()
        db.refresh(photo)

        # WebP conversion (and OCR) run in the background so a large batch of
        # files doesn't make the upload request hang until every image is
        # transcoded — the client gets a fast response and can keep using the
        # upload page right away. Photos without a converted file yet are
        # shown as "processing" placeholders in the gallery until this completes.
        background_tasks.add_task(_process_upload, photo.id, raw, str(dest))

        results.append({
            "original_filename": upload.filename,
            "photo_id": photo.id,
            "success": True,
        })

    return {"uploaded": sum(1 for r in results if r.get("success")), "results": results}


# ── Search ────────────────────────────────────────────────────────────────────

@app.get("/api/search", summary="Search photos by bib number within an album")
def search_by_bib(
    bib: str,
    album_id: int,
    db: Session = Depends(get_db),
):
    bib = bib.strip()
    if not bib.isdigit():
        raise HTTPException(400, "bib must be numeric")

    query = (
        db.query(Photo)
        .options(selectinload(Photo.bib_numbers))
        .join(BibNumber, BibNumber.photo_id == Photo.id)
        .filter(BibNumber.bib_number == bib)
    )
    if album_id == 0:
        query = query.filter(Photo.album_id == None)  # noqa: E711
    else:
        query = query.filter(Photo.album_id == album_id)

    photos = query.distinct().all()
    return {"bib": bib, "count": len(photos), "photos": [_photo_dict(p) for p in photos]}


# ── Album OCR progress ────────────────────────────────────────────────────────

@app.get("/api/albums/{album_id}/progress", summary="OCR processing progress for an album")
def album_ocr_progress(album_id: int, db: Session = Depends(get_db)):
    q = db.query(Photo.ocr_status, func.count(Photo.id).label("cnt"))
    if album_id == 0:
        q = q.filter(Photo.album_id == None)  # noqa: E711
    else:
        q = q.filter(Photo.album_id == album_id)
    status_map = {row.ocr_status: row.cnt for row in q.group_by(Photo.ocr_status).all()}
    done = status_map.get("done", 0)
    pending = status_map.get("pending", 0)
    failed = status_map.get("failed", 0)
    return {"total": done + pending + failed, "pending": pending, "done": done, "failed": failed}


# ── Download ──────────────────────────────────────────────────────────────────

@app.get("/api/photos/{photo_id}/download", summary="Download original photo")
def download_photo(photo_id: int, db: Session = Depends(get_db)):
    photo = db.query(Photo).filter(Photo.id == photo_id).first()
    if not photo:
        raise HTTPException(404, "Photo not found")
    if photo.storage_url:
        return RedirectResponse(url=photo.storage_url)
    path = UPLOAD_DIR / photo.filename
    if not path.exists():
        raise HTTPException(404, "File not found on disk")
    return FileResponse(path, filename=photo.original_filename, media_type="application/octet-stream")


# ── Reprocess ────────────────────────────────────────────────────────────────

@app.post("/api/reprocess", summary="Re-run OCR on existing photos")
async def reprocess_photos(
    album_id: Optional[int] = Form(None),
    unrecognized_only: bool = Form(True),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_user),
):
    query = db.query(Photo)
    if album_id is not None:
        if album_id == 0:
            query = query.filter(Photo.album_id == None)  # noqa: E711
        else:
            query = query.filter(Photo.album_id == album_id)
    if unrecognized_only:
        query = query.filter(~exists().where(BibNumber.photo_id == Photo.id))

    photos = query.all()
    processed = 0
    recognized = 0

    for photo in photos:
        local_path = UPLOAD_DIR / photo.filename
        temp_file: Optional[str] = None
        if local_path.exists():
            ocr_path = str(local_path)
        elif photo.storage_url:
            import tempfile
            import urllib.request
            suffix = Path(photo.filename).suffix or ".jpg"
            fd, temp_file = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            try:
                urllib.request.urlretrieve(photo.storage_url, temp_file)
                ocr_path = temp_file
            except Exception:
                os.unlink(temp_file)
                temp_file = None
                continue
        else:
            continue

        try:
            bib_list = extract_bib_numbers(ocr_path)
        except Exception:
            bib_list = []
        finally:
            if temp_file and Path(temp_file).exists():
                os.unlink(temp_file)

        db.query(BibNumber).filter(BibNumber.photo_id == photo.id).delete()
        for bib in bib_list:
            db.add(BibNumber(photo_id=photo.id, bib_number=bib))
        photo.ocr_status = "done"
        db.commit()

        processed += 1
        if bib_list:
            recognized += 1

    return {
        "processed": processed,
        "recognized": recognized,
        "unrecognized": processed - recognized,
    }


# ── Stats / Health ────────────────────────────────────────────────────────────

@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    total = db.query(Photo).count()
    unrecognized = db.query(Photo).filter(~exists().where(BibNumber.photo_id == Photo.id)).count()
    return {
        "total_photos": total,
        "recognized_photos": total - unrecognized,
        "unrecognized_photos": unrecognized,
        "total_bib_records": db.query(BibNumber).count(),
        "total_albums": db.query(Album).count(),
        "total_users": db.query(User).count(),
    }


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        **gpu_info(),
    }


# ── Site Settings ─────────────────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    return _load_settings()


@app.put("/api/settings")
def update_settings(
    req: SiteSettingsRequest,
    current_user: User = Depends(_require_user),
):
    if current_user.role != "admin":
        raise HTTPException(403, "管理員權限才能修改網站設定")
    settings = _load_settings()
    if req.site_title is not None:
        title = req.site_title.strip()
        settings["site_title"] = title if title else "路跑相簿"
    _save_settings(settings)
    return settings


@app.post("/api/settings/logo")
async def upload_logo(
    file: UploadFile = File(...),
    current_user: User = Depends(_require_user),
):
    if current_user.role != "admin":
        raise HTTPException(403, "管理員權限才能修改 Logo")
    if file.content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        raise HTTPException(400, "不支援的圖片格式（請使用 JPG、PNG 或 WebP）")

    raw = await file.read()
    fmt = _verify_image(raw)
    if not EXT_BY_FORMAT.get(fmt or ""):
        raise HTTPException(400, "檔案內容不是有效的圖片")

    for old in STATIC_DIR.glob("logo.*"):
        old.unlink(missing_ok=True)

    logo_path = STATIC_DIR / "logo.webp"
    logo_path.write_bytes(_to_webp(raw))

    logo_url = "/static/logo.webp"
    settings = _load_settings()
    settings["logo_url"] = logo_url
    _save_settings(settings)
    return settings


@app.delete("/api/settings/logo")
def delete_logo(current_user: User = Depends(_require_user)):
    if current_user.role != "admin":
        raise HTTPException(403, "管理員權限才能修改 Logo")
    for old in STATIC_DIR.glob("logo.*"):
        old.unlink(missing_ok=True)
    settings = _load_settings()
    settings["logo_url"] = None
    _save_settings(settings)
    return settings


# ── Favicon endpoints ──────────────────────────────────────────────────────────

@app.post("/api/settings/favicon")
async def upload_favicon(
    file: UploadFile = File(...),
    current_user: User = Depends(_require_user),
):
    """Upload a custom browser-tab favicon (admin only).

    Security hardening mirrors upload_logo():
    - Role check before any I/O.
    - MIME type allow-list (SVG excluded — SVG can embed <script>).
    - Content-based format verification via Pillow, not just the
      client-supplied Content-Type header (which is attacker-controlled).
    - Hard 2 MB cap before buffering the full file in memory.
    - Converted to WebP via _to_webp(), which strips EXIF metadata and
      normalises pixel modes — the stored bytes never contain the original
      potentially-crafted file.
    - Old favicon files are removed before the new one is written to avoid
      stale files accumulating in /static.
    """
    if current_user.role != "admin":
        raise HTTPException(403, "管理員權限才能修改 Favicon")
    if file.content_type not in _FAVICON_ALLOWED_MIME:
        raise HTTPException(
            400,
            "不支援的圖片格式（請使用 PNG、JPG、WebP 或 ICO；不接受 SVG）",
        )

    raw = await file.read()

    # Hard size limit — prevents memory exhaustion on oversized uploads.
    # 2 MB is generous for a favicon (typical size is <100 KB).
    _FAVICON_MAX_BYTES = 2 * 1024 * 1024
    if len(raw) > _FAVICON_MAX_BYTES:
        raise HTTPException(400, "Favicon 圖片不得超過 2 MB")

    fmt = _verify_image_favicon(raw)
    if fmt not in _FAVICON_ALLOWED_FORMATS:
        raise HTTPException(400, "檔案內容不是有效的圖片，請確認檔案未損毀")

    # Remove all existing favicon.* files before writing the new one.
    for old in STATIC_DIR.glob("favicon.*"):
        old.unlink(missing_ok=True)

    # Convert to WebP: strips metadata, normalises pixel format, reduces size.
    favicon_path = STATIC_DIR / "favicon.webp"
    favicon_path.write_bytes(_to_webp(raw))

    settings = _load_settings()
    settings["favicon_url"] = "/static/favicon.webp"
    _save_settings(settings)
    return settings


@app.delete("/api/settings/favicon")
def delete_favicon(current_user: User = Depends(_require_user)):
    """Remove the custom favicon and revert to the built-in SVG default (admin only)."""
    if current_user.role != "admin":
        raise HTTPException(403, "管理員權限才能修改 Favicon")
    for old in STATIC_DIR.glob("favicon.*"):
        old.unlink(missing_ok=True)
    settings = _load_settings()
    settings["favicon_url"] = None
    _save_settings(settings)
    return settings


# ── Admin User Management ─────────────────────────────────────────────────────

@app.get("/api/admin/users")
def list_admin_users(
    current_user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    if current_user.role != "admin":
        raise HTTPException(403, "管理員權限才能查看用戶列表")
    users = db.query(User).order_by(User.created_at.asc()).all()
    return [_user_dict_admin(u) for u in users]


@app.put("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: int,
    req: AdminUpdateUserRequest,
    current_user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    if current_user.role != "admin":
        raise HTTPException(403, "管理員權限才能修改用戶資料")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "用戶不存在")

    if req.display_name is not None:
        name = req.display_name.strip()
        if not name:
            raise HTTPException(400, "顯示名稱不得為空")
        user.display_name = name

    if req.email is not None:
        email = req.email.strip().lower()
        if db.query(User).filter(User.email == email, User.id != user_id).first():
            raise HTTPException(409, "Email 已被其他帳號使用")
        user.email = email

    if req.role is not None:
        if req.role not in ("admin", "user"):
            raise HTTPException(400, "角色只能是 admin 或 user")
        if user_id == current_user.id and req.role != "admin":
            raise HTTPException(400, "無法移除自己的管理員權限")
        user.role = req.role

    if req.new_password:
        if len(req.new_password) < 6:
            raise HTTPException(400, "密碼至少需要 6 個字元")
        user.hashed_password = _hash_password(req.new_password)

    db.commit()
    db.refresh(user)
    return _user_dict_admin(user)


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(
    user_id: int,
    current_user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    if current_user.role != "admin":
        raise HTTPException(403, "管理員權限才能刪除用戶")
    if user_id == current_user.id:
        raise HTTPException(400, "無法刪除自己的帳號")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "用戶不存在")
    db.delete(user)
    db.commit()
    return {"deleted": True, "user_id": user_id}


@app.post("/api/admin/factory-reset", summary="Wipe all data and return the system to a fresh-install state")
def factory_reset(
    req: FactoryResetRequest,
    current_user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    if current_user.role != "admin":
        raise HTTPException(403, "管理員權限才能重置系統")
    if req.confirm_phrase != RESET_CONFIRM_PHRASE:
        raise HTTPException(400, f"請輸入確認文字「{RESET_CONFIRM_PHRASE}」以繼續")
    # Re-check the requester's own password — a destructive, irreversible action
    # like this should not be reachable merely by holding a (possibly stale or
    # leaked) bearer token; require proof of the current credentials too.
    if not _verify_password(req.password, current_user.hashed_password):
        raise HTTPException(400, "密碼不正確")

    cloudinary_ids = [
        Path(p.filename).stem
        for p in db.query(Photo).filter(Photo.storage_url.isnot(None)).all()
    ]

    db.query(BibNumber).delete()
    db.query(Photo).delete()
    db.query(Album).delete()
    db.query(User).delete()
    db.commit()

    for cid in cloudinary_ids:
        _cloudinary_delete(cid)

    for f in UPLOAD_DIR.iterdir():
        if f.is_file():
            f.unlink(missing_ok=True)
    for f in STATIC_DIR.glob("logo.*"):
        f.unlink(missing_ok=True)

    _save_settings({"site_title": "路跑相簿", "logo_url": None})

    return {"reset": True}


# ── OCR background task ───────────────────────────────────────────────────────

def _process_upload(photo_id: int, raw: bytes, dest_path: str) -> None:
    """Convert the uploaded image (and thumbnail) to WebP, then run OCR.

    Runs in a background task so the upload request can return as soon as the
    file is validated and recorded, instead of blocking on transcoding.
    """
    try:
        dest = Path(dest_path)
        dest.write_bytes(_to_webp(raw))
        (dest.parent / f"{dest.stem}_thumb.webp").write_bytes(_to_webp(raw, max_width=THUMB_MAX_PX))
    except Exception:
        db = SessionLocal()
        try:
            photo = db.query(Photo).filter(Photo.id == photo_id).first()
            if photo:
                photo.ocr_status = "failed"
                db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
        return

    _process_ocr(photo_id, dest_path)


def _process_ocr(photo_id: int, file_path: str) -> None:
    db = SessionLocal()
    try:
        try:
            bib_list = extract_bib_numbers(file_path)
        except Exception:
            bib_list = []

        # Upload to Cloudinary while local file is still present
        storage_url = _cloudinary_upload(file_path, Path(file_path).stem)

        photo = db.query(Photo).filter(Photo.id == photo_id).first()
        if photo:
            for bib in bib_list:
                db.add(BibNumber(photo_id=photo_id, bib_number=bib))
            photo.ocr_status = "done"
            if storage_url:
                photo.storage_url = storage_url
            db.commit()

        # Local file no longer needed once it's safely on Cloudinary
        if storage_url:
            Path(file_path).unlink(missing_ok=True)

    except Exception:
        db.rollback()
        try:
            photo = db.query(Photo).filter(Photo.id == photo_id).first()
            if photo:
                photo.ocr_status = "failed"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


# ── Helper ────────────────────────────────────────────────────────────────────

def _photo_dict(p: Photo) -> dict:
    url = p.storage_url if p.storage_url else f"/uploads/{p.filename}"
    file_exists = bool(p.storage_url) or (UPLOAD_DIR / p.filename).exists()

    if p.storage_url:
        thumb_url = url
    else:
        thumb_name = p.filename.replace(".webp", "_thumb.webp")
        thumb_url = f"/uploads/{thumb_name}" if (UPLOAD_DIR / thumb_name).exists() else url

    return {
        "photo_id": p.id,
        "url": url,
        "thumb_url": thumb_url,
        "original_filename": p.original_filename,
        "bib_numbers": [b.bib_number for b in p.bib_numbers],
        "album_id": p.album_id,
        "upload_time": p.upload_time.isoformat(),
        "file_exists": file_exists,
        "ocr_status": p.ocr_status,
    }
