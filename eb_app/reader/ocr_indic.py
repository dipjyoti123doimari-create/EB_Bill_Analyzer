"""
ocr_indic.py
============
Second-pass OCR for pages printed in an Indic script (Hindi/Marathi, Kannada, Tamil, Telugu), using the multilingual
PP-OCR recognition models shipped with the `rapidocr` package. Everything runs locally; the only network use is the one-time
download of the model *files* (never of any document).

The first-pass engine (ocr.py) reads Latin text and numerals very well but turns Devanagari into CJK noise. When a page shows
that noise (see needs_indic_pass) the page is read again here and the new text replaces the first pass. The Indic models also
read Latin text and digits, so bilingual bills lose nothing.

Scripts without a recognition model (Bengali, Gujarati, Malayalam, Odia, Punjabi) are NOT covered: such pages are reported as
needing review, never guessed.
"""

import json
import logging
import os
import threading

import cv2
import numpy as np

from .. import config
from .model import Cell

log = logging.getLogger(__name__)

ENGINE_VERSION = "rapidocr3-ppocrv5"
CACHE_DIR = config.DATA_DIR / "ocr_cache"
OCR_THREADS = 4
ENABLED = os.environ.get("EB_INDIC_OCR", "1") != "0"

# Which recognition model to use for a state's usual script. Devanagari is also the default when the noise pattern
# says "Indic" but the language is unknown.
STATE_LANG = {
    "Karnataka": "KA", "Tamil Nadu": "TA", "Telangana": "TE", "Andhra Pradesh": "TE",
    "Maharashtra": "DEVANAGARI", "Uttar Pradesh": "DEVANAGARI", "Madhya Pradesh": "DEVANAGARI", "Rajasthan": "DEVANAGARI",
    "Haryana": "DEVANAGARI", "Chhattisgarh": "DEVANAGARI", "Bihar": "DEVANAGARI", "Jharkhand": "DEVANAGARI", "Delhi": "DEVANAGARI",
}
SUPPORTED = ("DEVANAGARI", "KA", "TA", "TE")

_engines: dict = {}
_lock = threading.Lock()
_run_lock = threading.Lock()


def _engine(lang: str):
    with _lock:
        if lang not in _engines:
            from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
            _engines[lang] = RapidOCR(params={
                "Rec.lang_type": getattr(LangRec, lang), "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE,
                "EngineConfig.onnxruntime.intra_op_num_threads": OCR_THREADS,
                "Global.log_level": "warning"})
        return _engines[lang]


def needs_indic_pass(cells: list) -> bool:
    """CJK / full-width characters among first-pass OCR boxes are how Indic print looks to a Latin-trained model."""
    if len(cells) < 8:
        return False
    noisy = sum(1 for c in cells if any("⺀" <= ch <= "鿿" or "＀" <= ch <= "￯" for ch in c.text))
    return noisy / len(cells) >= 0.06


def pick_language(state: str | None) -> str:
    return STATE_LANG.get(state or "", "DEVANAGARI")


def ocr_page(img_bgr: np.ndarray, lang: str, page: int, first_pass_info: dict, use_cache: bool = True):
    """Read one page with the `lang` recognition model, in the same (rotated/deskewed) frame as the first pass.
    Returns (cells, info)."""
    from . import ocr as first
    key = f"{first.image_key(img_bgr, f'p{page}')}_{lang}_{ENGINE_VERSION}"
    cp = CACHE_DIR / f"{key}.json"
    if use_cache and cp.exists():
        data = json.loads(cp.read_text(encoding="utf-8"))
        return [Cell.from_json(d, page) for d in data["cells"]], data["info"] | {"cached": True}

    img = first._fit(img_bgr)
    rot = first_pass_info.get("rotation", 0)
    if rot:
        img = cv2.rotate(img, {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}[rot])
    img = first.deskew(img, first_pass_info.get("deskew_deg", 0.0)) if abs(first_pass_info.get("deskew_deg", 0.0)) >= first.DESKEW_MIN_DEG else img
    with _run_lock:
        out = _engine(lang)(img)
    cells = []
    if out is not None and out.txts:
        for box, text, score in zip(out.boxes, out.txts, out.scores):
            text = (text or "").strip()
            if text:
                xs, ys = [p[0] for p in box], [p[1] for p in box]
                cells.append(Cell(text, float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)), float(score), "ocr", page))
    info = {"engine": f"{ENGINE_VERSION}:{lang}", "lang": lang, "width": img.shape[1], "height": img.shape[0],
            "mean_conf": round(float(np.mean([c.conf for c in cells])), 3) if cells else 0.0, "cached": False}
    if use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps({"cells": [c.to_json() for c in cells], "info": info}), encoding="utf-8")
    return cells, info
