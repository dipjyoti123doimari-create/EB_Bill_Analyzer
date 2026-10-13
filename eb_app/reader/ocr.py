"""
ocr.py
======
Local OCR (RapidOCR / PP-OCR on ONNX Runtime). No document ever leaves this machine.

Why this engine: pip-installable without administrator rights, reads printed English text and numerals
reliably (the numerals are what the bills are decided by), returns a box and a confidence for every
line, and runs on CPU. Indic scripts are NOT recognised by this model; a bill whose labels are only in an
Indic script is detected (see loader/pipeline) and routed to human review instead of being guessed.

Pipeline per image: orientation check -> light deskew -> OCR -> cells.
Results are cached on disk by (image hash, engine version) so a document is never OCR'd twice.
"""

import hashlib
import json
import logging
import threading
from pathlib import Path

import cv2
import numpy as np

from .. import config
from .model import Cell

log = logging.getLogger(__name__)

ENGINE_VERSION = "rapidocr-1.4.4+pre2"
OCR_THREADS = 4               # measured: 1 thread = 52 s, 4 threads = 10 s for a dense A4 page
MAX_SIDE_PX = 2600            # longest side fed to the engine
DESKEW_MIN_DEG = 1.0
MIN_USEFUL_BOXES = 8          # fewer boxes than this on a page -> try other orientations
CACHE_DIR = config.DATA_DIR / "ocr_cache"

_engine = None
_engine_lock = threading.Lock()
_run_lock = threading.Lock()   # one OCR call at a time per process; parallelism comes from worker processes


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR(intra_op_num_threads=OCR_THREADS, inter_op_num_threads=1)
        return _engine


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def image_key(img: np.ndarray, tag: str = "") -> str:
    h = hashlib.sha256(img.tobytes()).hexdigest()[:32]
    return f"{h}_{tag}_{ENGINE_VERSION}"


def estimate_skew_degrees(gray: np.ndarray) -> float:
    """Dominant text-line angle in degrees (positive = counter-clockwise), 0.0 if unclear."""
    h, w = gray.shape[:2]
    scale = 1000.0 / max(h, w)
    small = cv2.resize(gray, (int(w * scale), int(h * scale))) if scale < 1 else gray
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    bw = cv2.dilate(bw, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 1)))
    lines = cv2.HoughLinesP(bw, 1, np.pi / 720, threshold=120, minLineLength=small.shape[1] // 4, maxLineGap=10)
    if lines is None:
        return 0.0
    angles = []
    for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):      # OpenCV 4 returns (N,1,4), OpenCV 5 returns (N,4)
        a = np.degrees(np.arctan2(y1 - y2, x2 - x1))
        if abs(a) < 20:
            angles.append(a)
    if len(angles) < 5:
        return 0.0
    return float(np.median(angles))


def box_angles(res) -> list:
    """Angle (degrees, clockwise-positive) of the top edge of every reasonably long text box."""
    out = []
    for box, text, _ in res:
        (x0, y0), (x1, y1) = box[0], box[1]
        if len(text) >= 4 and ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 > 60:
            out.append(float(np.degrees(np.arctan2(y1 - y0, x1 - x0))))
    return out


def deskew(img: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.05:
        return img
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), -angle, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _fit(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    s = MAX_SIDE_PX / max(h, w)
    return cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA) if s < 1 else img


def _run(img: np.ndarray):
    with _run_lock:
        res, _ = _get_engine()(img)
    return res or []


def _looks_sideways(res) -> bool:
    """Text lines are wide; if most detected boxes are TALLER than wide the page has been scanned/photographed on its side."""
    ratios = []
    for box, text, _ in res:
        if len(text) >= 3:
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            if h > 0:
                ratios.append(w / h)
    return len(ratios) >= 5 and float(np.median(ratios)) < 0.8


def _upright_fraction(res) -> float:
    """Share of rows where a word-label sits LEFT of a number (bill layout: 'Units Consumed ... 14097'). An upside-down page reads the
    same strings but mirrored, so this is close to 0 there."""
    rows = {}
    for box, text, _ in res:
        ys = [pt[1] for pt in box]
        rows.setdefault(round(((min(ys) + max(ys)) / 2) / 14), []).append((min(pt[0] for pt in box), text))
    ok = bad = 0
    for items in rows.values():
        items.sort()
        kinds = ["n" if any(ch.isdigit() for ch in t) and sum(ch.isalpha() for ch in t) <= 2 else "w" if sum(ch.isalpha() for ch in t) >= 3 else "?" for _, t in items]
        for a, b in zip(kinds, kinds[1:]):
            ok += (a, b) == ("w", "n")
            bad += (a, b) == ("n", "w")
    return ok / (ok + bad) if ok + bad else 0.5


def _score(res) -> float:
    return sum(float(c) for _, t, c in res if any(ch.isalnum() for ch in t))


def ocr_image(img_bgr: np.ndarray, page: int = 1, use_cache: bool = True):
    """OCR one BGR page image. Returns (cells, info). Cell coordinates are in the pixel space of the
    image that was actually read (info width/height), i.e. after any rotation / deskew."""
    key = image_key(img_bgr, f"p{page}")
    cp = _cache_path(key)
    if use_cache and cp.exists():
        data = json.loads(cp.read_text(encoding="utf-8"))
        return [Cell.from_json(d, page) for d in data["cells"]], data["info"] | {"cached": True}

    img = _fit(img_bgr)
    rotation = 0
    res = _run(img)
    sideways = _looks_sideways(res)
    if sideways or len(res) < MIN_USEFUL_BOXES or float(np.mean([c for _, _, c in res])) < 0.6:
        # Maybe the page is sideways / upside down: try the other orientations on a reduced copy, keep the best.
        base = _score(res)
        small = cv2.resize(img, None, fx=0.6, fy=0.6, interpolation=cv2.INTER_AREA) if max(img.shape[:2]) > 1500 else img
        best_rot, best_score = 0, base
        candidates = ((90, cv2.ROTATE_90_CLOCKWISE), (270, cv2.ROTATE_90_COUNTERCLOCKWISE)) if sideways else \
            ((90, cv2.ROTATE_90_CLOCKWISE), (180, cv2.ROTATE_180), (270, cv2.ROTATE_90_COUNTERCLOCKWISE))
        for rot, code in candidates:
            r = _run(cv2.rotate(small, code))
            s = _score(r)
            if sideways:
                # the per-line classifier may have already READ the sideways text; what is wrong is its geometry.
                # Take the rotation that makes the text boxes wide again, provided it reads at least as well.
                s = s * (0.5 + _upright_fraction(r))           # upright beats upside-down when the recognition score ties
                if not _looks_sideways(r) and s >= 0.5 * base and s > (best_score if best_rot else 0):
                    best_rot, best_score = rot, s
            elif s > best_score * 1.5 + 2:
                best_rot, best_score = rot, s
        if best_rot:
            code = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}[best_rot]
            rotation, img = best_rot, cv2.rotate(img, code)
            res = _run(img)
    angs = box_angles(res)
    if len(angs) >= 15:
        angle = -float(np.median(angs))             # the OCR boxes themselves are the best measure of how the text is tilted
    else:
        angle = estimate_skew_degrees(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    if abs(angle) >= DESKEW_MIN_DEG:                # small tilt is absorbed by the detector; correcting it costs a 2nd OCR pass
        img = deskew(img, angle)
        res = _run(img)
        angs = box_angles(res)
    spread = float(np.percentile(angs, 90) - np.percentile(angs, 10)) if len(angs) >= 15 else 0.0

    cells = []
    for box, text, conf in res:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        text = (text or "").strip()
        if text:
            cells.append(Cell(text, min(xs), min(ys), max(xs), max(ys), float(conf), "ocr", page))
    info = {"engine": ENGINE_VERSION, "rotation": rotation, "deskew_deg": round(angle, 2), "angle_spread": round(spread, 2), "width": img.shape[1],
            "height": img.shape[0], "mean_conf": round(float(np.mean([c.conf for c in cells])), 3) if cells else 0.0,
            "cached": False}
    if use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps({"cells": [c.to_json() for c in cells], "info": info}), encoding="utf-8")
    return cells, info
