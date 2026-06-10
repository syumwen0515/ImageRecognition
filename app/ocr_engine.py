# ocr_engine.py — Bib number OCR with GPU acceleration
#
# Backend priority (local):
#   1. EasyOCR  — deep-learning OCR, auto-uses NVIDIA GPU via PyTorch CUDA
#   2. Tesseract — traditional CPU-only fallback
#
# Cloud backends (optional, highest accuracy):
#   3. Claude Vision API  (ANTHROPIC_API_KEY)
#   4. Google Cloud Vision (GOOGLE_APPLICATION_CREDENTIALS)
#
# GPU setup (one-time, before pip install easyocr):
#   Check your CUDA version: nvidia-smi
#   CUDA 12.x: pip install torch --index-url https://download.pytorch.org/whl/cu121
#   CUDA 11.8: pip install torch --index-url https://download.pytorch.org/whl/cu118
#   Then: pip install easyocr

import base64
import re
import threading
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

# ── GPU availability ──────────────────────────────────────────────────────────

def _detect_gpu() -> Tuple[bool, str]:
    """Return (cuda_available, device_name)."""
    try:
        import torch
        if torch.cuda.is_available():
            return True, torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return False, "N/A"

_GPU_AVAILABLE, _GPU_NAME = _detect_gpu()


# ── EasyOCR (lazy singleton — loaded once per process) ───────────────────────

_EASYOCR_READER = None

# The model + GPU memory stay resident for as long as the process holds a
# reference. Release them after a period of inactivity so the GPU is freed
# once a batch of OCR work is done, instead of camping on VRAM forever.
_IDLE_RELEASE_SECONDS = 300
_release_timer: Optional[threading.Timer] = None
_reader_lock = threading.Lock()


def _release_reader() -> None:
    """Drop the EasyOCR model and free GPU memory after an idle period."""
    global _EASYOCR_READER, _release_timer
    with _reader_lock:
        if _EASYOCR_READER is None:
            return
        print("[ocr_engine] Releasing EasyOCR model — idle timeout reached")
        _EASYOCR_READER = None
        _release_timer = None

    import gc
    gc.collect()
    if _GPU_AVAILABLE:
        try:
            import torch  # type: ignore
            torch.cuda.empty_cache()
        except ImportError:
            pass


def _arm_release_timer() -> None:
    """(Re)start the idle countdown — called every time the model is used."""
    global _release_timer
    with _reader_lock:
        if _release_timer is not None:
            _release_timer.cancel()
        _release_timer = threading.Timer(_IDLE_RELEASE_SECONDS, _release_reader)
        _release_timer.daemon = True
        _release_timer.start()


def _get_reader():
    global _EASYOCR_READER
    with _reader_lock:
        if _EASYOCR_READER is None:
            import easyocr  # type: ignore
            if _GPU_AVAILABLE:
                print(f"[ocr_engine] EasyOCR initialising — GPU: {_GPU_NAME}")
            else:
                print("[ocr_engine] EasyOCR initialising — CPU only "
                      "(install torch+CUDA for GPU acceleration)")
            _EASYOCR_READER = easyocr.Reader(
                ['en'],
                gpu=_GPU_AVAILABLE,
                verbose=False,
            )
        reader = _EASYOCR_READER

    _arm_release_timer()
    return reader


def _ocr_easyocr(image: np.ndarray) -> List[str]:
    """
    Run EasyOCR on a (possibly cropped/upscaled) BGR numpy image.
    Returns filtered bib number strings.
    """
    reader = _get_reader()
    results = reader.readtext(
        image,
        allowlist='0123456789',
        paragraph=False,
        min_size=18,
        text_threshold=0.55,
        low_text=0.35,
        link_threshold=0.40,
        decoder='greedy',
        batch_size=4 if _GPU_AVAILABLE else 1,
    )
    found: set[str] = set()
    weak: List[Tuple[list, str]] = []
    for (bbox, text, conf) in results:
        tok = text.strip()
        if not _is_valid_bib(tok):
            continue
        # Short numbers (2-3 digits) are usually partial reads — require higher confidence
        min_conf = 0.85 if len(tok) <= 2 else (0.75 if len(tok) == 3 else 0.55)
        if conf >= min_conf:
            found.add(tok)
        elif conf >= 0.25 and _looks_like_digit_string(bbox):
            # Plausible bib shape but too small/angled to score well at full-frame
            # resolution — worth a focused recheck rather than discarding outright.
            # The shape gate matters: zooming into a low-confidence box can make
            # EasyOCR confidently misread non-bib text (race-distance badges like
            # "21K", merged/garbled regions) — genuine bib digit strings are
            # consistently wider than tall (ratio >= ~1.7), while those false
            # leads come back squarish or sliver-thin.
            weak.append((bbox, tok))

    for bbox, tok in weak:
        if tok in found:
            continue
        if _recheck_low_confidence(reader, image, bbox, tok):
            found.add(tok)

    return sorted(found, key=lambda x: int(x))


def _looks_like_digit_string(bbox: list) -> bool:
    """
    Sanity-check a detection box's proportions before spending a recheck on it.
    Real bib number strings are noticeably wider than tall (observed ratio
    ~1.7-2.7 across genuine detections); squarish or sliver-thin boxes are
    usually merged/garbled regions or small badge text, not a digit string.
    """
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    if h <= 0:
        return False
    return w / h >= 1.6


def _recheck_low_confidence(reader, image: np.ndarray, bbox: list, token: str) -> bool:
    """
    Re-OCR a low-confidence detection by cropping generously around its
    bounding box, upscaling to bib-crop resolution, and boosting contrast.
    Recovers numbers that are genuinely there but score too low at full-frame
    scale — e.g. small, angled, or distant bibs.

    Requires the rechecked read to exactly match the original token: the wider
    crop can sweep in a neighbouring runner's bib, so an exact match is the
    signal that we re-read the *same* number with more pixels to work with,
    rather than stumbling onto a different one.
    """
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    x1, y1, x2, y2 = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
    bw, bh = x2 - x1, y2 - y1
    if bw <= 0 or bh <= 0:
        return False

    ih, iw = image.shape[:2]
    pad_x, pad_y = int(bw * 1.5), int(bh * 1.8)
    cx1, cy1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
    cx2, cy2 = min(iw, x2 + pad_x), min(ih, y2 + pad_y)
    crop = image[cy1:cy2, cx1:cx2]
    if crop.size == 0:
        return False
    crop_min_width = max(_BIB_MIN_WIDTH, 600)
    if crop.shape[1] < crop_min_width:
        scale = crop_min_width / crop.shape[1]
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    crop = _enhance_contrast(crop)

    results = reader.readtext(
        crop,
        allowlist='0123456789',
        paragraph=False,
        min_size=10,
        text_threshold=0.4,
        low_text=0.3,
        link_threshold=0.3,
        decoder='greedy',
    )
    min_conf = 0.85 if len(token) <= 2 else (0.75 if len(token) == 3 else 0.55)
    for (_b, text, conf) in results:
        if text.strip() == token and conf >= min_conf:
            return True
    return False


# ── Image helpers ─────────────────────────────────────────────────────────────

def _load_and_resize(image_path: str, max_width: int = 2000) -> np.ndarray:
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError(f"Cannot open image: {image_path}")
    h, w = img.shape[:2]
    if w > max_width:
        scale = max_width / w
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    elif w < 800:
        scale = 800 / w
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return img


def _enhance_contrast(image: np.ndarray) -> np.ndarray:
    """
    CLAHE contrast boost on the luminance channel.
    Recovers bib numbers washed out by sunlight glare or cast in shadow —
    a common failure mode in outdoor race photos that raw EasyOCR misses.
    """
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)


_BIB_MIN_WIDTH = 400


def _make_crop(image: np.ndarray, x: int, y: int, bw: int, bh: int) -> np.ndarray:
    ih, iw = image.shape[:2]
    pad_x, pad_y = int(bw * 0.15), int(bh * 0.15)
    x1, y1 = max(0, x - pad_x), max(0, y - pad_y)
    x2, y2 = min(iw, x + bw + pad_x), min(ih, y + bh + pad_y)
    crop = image[y1:y2, x1:x2].copy()
    if crop.shape[1] < _BIB_MIN_WIDTH:
        scale = _BIB_MIN_WIDTH / crop.shape[1]
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return crop


def _split_bib_region(
    image: np.ndarray,
    white_mask: np.ndarray,
    x: int, y: int, bw: int, bh: int,
) -> List[np.ndarray]:
    """
    Wide contour that likely covers multiple bibs side-by-side.
    Analyse column density of the white mask to find gaps between bibs,
    then return one crop per segment.
    """
    roi = white_mask[y:y + bh, x:x + bw]
    col_density = roi.sum(axis=0).astype(float) / max(bh * 255, 1)

    # Smooth over ~5% of width to reduce noise
    k = max(1, bw // 20)
    col_smooth = np.convolve(col_density, np.ones(k) / k, mode='same')

    threshold = col_smooth.max() * 0.25
    active = col_smooth > threshold

    segments: List[Tuple[int, int]] = []
    seg_start: Optional[int] = None
    for i, is_active in enumerate(active):
        if is_active and seg_start is None:
            seg_start = i
        elif not is_active and seg_start is not None:
            segments.append((seg_start, i))
            seg_start = None
    if seg_start is not None:
        segments.append((seg_start, bw))

    # Discard slivers narrower than 20% of bib height
    min_w = max(bh * 0.2, 20)
    segments = [(s, e) for s, e in segments if e - s >= min_w]

    if not segments:
        return [_make_crop(image, x, y, bw, bh)]

    crops = []
    for seg_x1, seg_x2 in segments:
        crops.append(_make_crop(image, x + seg_x1, y, seg_x2 - seg_x1, bh))
    return crops


def _detect_bib_regions(image: np.ndarray) -> List[np.ndarray]:
    """
    HSV colour segmentation to find white-body + blue-header bib rectangles.
    Returns upscaled crops. Falls back to empty list if nothing found.
    Wide contours (multiple bibs side-by-side) are split rather than discarded.
    """
    h, w = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    white_mask = cv2.inRange(hsv, np.array([0, 0, 170]),  np.array([180, 55, 255]))
    blue_mask  = cv2.inRange(hsv, np.array([90, 60, 60]), np.array([140, 255, 255]))

    # Smaller dilation (20 vs 40) to avoid bridging adjacent runners' bibs
    blue_stretch = cv2.dilate(blue_mask, np.ones((20, 1), np.uint8))
    bib_mask = cv2.bitwise_and(white_mask, blue_stretch)
    # Smaller closing (10x10 vs 20x20) to keep nearby bibs as separate blobs
    bib_mask = cv2.morphologyEx(bib_mask, cv2.MORPH_CLOSE,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (10, 10)))
    bib_mask = cv2.morphologyEx(bib_mask, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)))

    contours, _ = cv2.findContours(bib_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    img_area = w * h
    regions: List[np.ndarray] = []

    for cnt in contours:
        area = cv2.contourArea(cnt)
        # Upper limit raised to 0.60 so merged multi-bib blobs are not thrown away
        if area < img_area * 0.002 or area > img_area * 0.60:
            continue
        x, y, bw, bh = cv2.boundingRect(cnt)
        ratio = bw / max(bh, 1)

        if ratio < 0.4:
            continue

        if ratio > 2.8:
            # Likely two or more bibs merged — split horizontally
            for sub in _split_bib_region(image, white_mask, x, y, bw, bh):
                regions.append(sub)
        else:
            regions.append(_make_crop(image, x, y, bw, bh))

    regions.sort(key=lambda r: r.shape[0] * r.shape[1], reverse=True)
    return regions


# ── Tesseract fallback ────────────────────────────────────────────────────────

def _preprocess_variants(image: np.ndarray) -> list:
    from PIL import Image  # type: ignore
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image.copy()
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    blur = cv2.GaussianBlur(enhanced, (3, 3), 0)
    _, otsu   = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    inv_otsu  = cv2.bitwise_not(otsu)
    adaptive  = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                      cv2.THRESH_BINARY, 15, 4)
    closed    = cv2.morphologyEx(otsu, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    red_gray  = _red_to_black(image)
    return [Image.fromarray(v) for v in [otsu, inv_otsu, adaptive, closed, red_gray]]


def _red_to_black(image: np.ndarray) -> np.ndarray:
    if image.ndim != 3:
        return image.copy()
    hsv  = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    red1 = cv2.inRange(hsv, np.array([0, 80, 80]),   np.array([12, 255, 255]))
    red2 = cv2.inRange(hsv, np.array([165, 80, 80]), np.array([180, 255, 255]))
    dark = cv2.inRange(hsv, np.array([0, 0, 0]),     np.array([180, 255, 80]))
    mask = cv2.bitwise_or(cv2.bitwise_or(red1, red2), dark)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    return cv2.bitwise_not(mask)


def _ocr_tesseract(variants: list) -> List[str]:
    import pytesseract  # type: ignore
    PSM_MODES = ["--psm 11", "--psm 6", "--psm 3"]
    TESS_BASE = "--oem 3 -c tessedit_char_whitelist=0123456789"
    found: set[str] = set()
    for pil_img in variants:
        for psm in PSM_MODES:
            try:
                raw = pytesseract.image_to_string(pil_img, config=f"{psm} {TESS_BASE}")
                for tok in re.findall(r"\d+", raw):
                    if _is_valid_bib(tok):
                        found.add(tok)
            except Exception:
                continue
    return sorted(found, key=lambda x: int(x))


# ── Bib validation ────────────────────────────────────────────────────────────

def _is_valid_bib(token: str) -> bool:
    if not token.isdigit():
        return False
    n = len(token)
    if n < 2 or n > 5:          # single digits never bib; 6+ likely concatenation
        return False
    # Filter out calendar years (1900–2099) — they appear on race banners, not bibs
    val = int(token)
    if 1900 <= val <= 2099:
        return False
    return True


# ── Cloud backends ────────────────────────────────────────────────────────────

def _ocr_claude_api(image_path: str) -> List[str]:
    """Claude Vision API — needs ANTHROPIC_API_KEY."""
    import anthropic  # type: ignore
    suffix = Path(image_path).suffix.lower()
    media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".png": "image/png", ".webp": "image/webp"}
    with open(image_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64",
                "media_type": media_map.get(suffix, "image/jpeg"),
                "data": data,
            }},
            {"type": "text", "text": (
                "This is a running race photo. "
                "List every bib number (race number) worn by runners. "
                "Reply with ONLY the numbers, one per line. "
                "If none are visible, reply with 'none'."
            )},
        ]}],
    )
    raw = msg.content[0].text
    if raw.strip().lower() == "none":
        return []
    return sorted({tok for tok in re.findall(r"\d+", raw) if _is_valid_bib(tok)},
                  key=lambda x: int(x))


def _ocr_vision_api(image_path: str) -> List[str]:
    """Google Cloud Vision — needs GOOGLE_APPLICATION_CREDENTIALS."""
    from google.cloud import vision  # type: ignore
    client = vision.ImageAnnotatorClient()
    with open(image_path, "rb") as f:
        content = f.read()
    resp = client.text_detection(image=vision.Image(content=content))
    if resp.error.message:
        raise RuntimeError(f"Vision API error: {resp.error.message}")
    if not resp.text_annotations:
        return []
    full = resp.text_annotations[0].description
    return sorted({tok for tok in re.findall(r"\d+", full) if _is_valid_bib(tok)},
                  key=lambda x: int(x))


# ── Public API ────────────────────────────────────────────────────────────────

def gpu_info() -> dict:
    """Return GPU availability info — useful for health-check endpoints."""
    return {
        "cuda_available": _GPU_AVAILABLE,
        "device_name": _GPU_NAME,
        "easyocr_ready": _EASYOCR_READER is not None,
    }


def extract_bib_numbers(
    image_path: str,
    use_claude_api: bool = False,
    use_vision_api: bool = False,
) -> List[str]:
    """
    Extract bib numbers from a race photo.

    Tries backends in this order:
      1. Claude Vision API  (if use_claude_api=True)
      2. Google Vision      (if use_vision_api=True)
      3. EasyOCR + GPU      (auto-detected; falls back to CPU)
      4. Tesseract          (if EasyOCR not installed)
    """
    # ── Cloud paths ───────────────────────────────────────────────────────────
    if use_claude_api:
        try:
            result = _ocr_claude_api(image_path)
            if result:
                return result
        except Exception:
            pass

    if use_vision_api:
        try:
            result = _ocr_vision_api(image_path)
            if result:
                return result
        except Exception:
            pass

    # ── Load image ────────────────────────────────────────────────────────────
    img = _load_and_resize(image_path)
    all_found: set[str] = set()

    # ── EasyOCR path (GPU or CPU deep-learning) ───────────────────────────────
    try:
        # Pass 1 — full image (EasyOCR's CRAFT detector finds text regions)
        for bib in _ocr_easyocr(img):
            all_found.add(bib)

        # Pass 2 — colour-segmented bib crops (catches small/partially occluded bibs)
        for crop in _detect_bib_regions(img):
            for bib in _ocr_easyocr(crop):
                all_found.add(bib)
            # Pass 2b — contrast-enhanced crop recovers numbers washed out by
            # sun glare or shadow that the raw crop misses
            for bib in _ocr_easyocr(_enhance_contrast(crop)):
                all_found.add(bib)

        # Post-merge filter: remove 5-digit numbers that are a 4-digit bib + stray digit
        # e.g. "22745" = "2274" + "5" → discard "22745", keep "2274"
        four_digit = {b for b in all_found if len(b) == 4}
        all_found = {
            b for b in all_found
            if not (len(b) == 5 and any(b.startswith(b4) for b4 in four_digit))
        }

        return sorted(all_found, key=lambda x: int(x))

    except ImportError:
        pass  # EasyOCR not installed — fall through to Tesseract

    # ── Tesseract fallback ────────────────────────────────────────────────────
    for region in (_detect_bib_regions(img) or [img]):
        for bib in _ocr_tesseract(_preprocess_variants(region)):
            all_found.add(bib)

    for bib in _ocr_tesseract(_preprocess_variants(img)):
        all_found.add(bib)

    return sorted(all_found, key=lambda x: int(x))
