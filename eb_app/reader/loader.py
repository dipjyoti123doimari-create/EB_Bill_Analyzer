"""
loader.py
=========
Opens a bill file and turns every page into layout Cells.

* The real file type is decided from the file's bytes (magic numbers), never from the filename.
* PDF pages with a trustworthy text layer are read natively (exact, instant, no OCR error).
  Pages without text, or whose text layer is garbage, are rendered and OCR'd.
* Images (JPEG/PNG/TIFF/WebP/BMP/GIF, multi-page TIFF) are OCR'd after EXIF orientation is applied.
* The original file is only read, never modified.
"""

import hashlib
import io
import logging
import re
from pathlib import Path

import cv2
import numpy as np
import pymupdf
from PIL import Image, ImageOps

from . import ocr, ocr_indic
from .model import Cell, Document, Page
from .textnorm import script_counts

log = logging.getLogger(__name__)

MAX_PAGES = 40                 # a 27-page bill exists in the corpus; beyond this we stop and warn
MIN_NATIVE_CHARS = 40          # fewer printable characters on a page -> treat as scanned
MAX_GARBAGE_RATIO = 0.25       # share of replacement/private-use characters above which the text layer is not trusted
OCR_DPI = 170


def sniff_type(head: bytes) -> str:
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"BM", b"II*\x00", b"MM\x00*")) or \
            (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    return "unsupported"


def _native_cells(page, number: int) -> list:
    """Spans of the text layer as Cells in visual (rotation-corrected) page coordinates."""
    rot = page.rotation_matrix
    cells = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            merged: list = []
            for s in spans:
                r = pymupdf.Rect(s["bbox"]) * rot
                if merged:
                    last = merged[-1]
                    gap = r.x0 - last["x1"]
                    if 0 <= gap < 0.35 * max(r.height, 1) and abs(r.y0 - last["y0"]) < 0.5 * r.height:
                        last["text"] += (" " if gap > 0.12 * r.height else "") + s["text"].strip()
                        last["x1"] = max(last["x1"], r.x1)
                        last["y1"] = max(last["y1"], r.y1)
                        continue
                merged.append({"text": s["text"].strip(), "x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1})
            for m in merged:
                cells.append(Cell(m["text"], m["x0"], m["y0"], m["x1"], m["y1"], 1.0, "native", number))
    return cells


_WORD_THEN_DECIMAL = re.compile(r"^([A-Za-z][A-Za-z .()/%&-]{1,40}?)\s+([-+]?\d[\d,]*\.\d+|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})\s*(?:Cr|Dr)?$")
_CURRENCY_WORDS = {"rs", "ps", "inr", "paise", "rupees"}


def _split_label_value_cells(cells: list) -> list:
    """The PDF writer often puts 'Normal' and '4906.500' so close that they merge into one span. A word followed by a
    decimal number is split into two cells, positioned proportionally, so every spatial relation sees them separately."""
    out = []
    expanded = []
    for c in cells:                                    # 'Normal | 4906.500' -> 'Normal', '4906.500'
        parts = [p.strip() for p in c.text.split("|")] if "|" in c.text else []
        if len(parts) >= 2 and all(parts):
            total = sum(len(p) for p in parts) or 1
            x = c.x0
            for p in parts:
                w = (c.x1 - c.x0) * len(p) / total
                expanded.append(Cell(p, x, c.y0, x + w, c.y1, c.conf, c.src, c.page))
                x += w
        else:
            expanded.append(c)
    for c in expanded:
        m = _WORD_THEN_DECIMAL.match(c.text.strip())
        if m and m.group(1).strip().lower().rstrip(".") not in _CURRENCY_WORDS and len(c.text) >= 6:
            cut = len(m.group(1)) / max(len(c.text.strip()), 1)
            xm = c.x0 + (c.x1 - c.x0) * cut
            out.append(Cell(m.group(1).strip(), c.x0, c.y0, xm, c.y1, c.conf, c.src, c.page))
            out.append(Cell(c.text.strip()[len(m.group(1)):].strip(), xm, c.y0, c.x1, c.y1, c.conf, c.src, c.page))
        else:
            out.append(c)
    return out


def _text_layer_ok(cells: list) -> bool:
    text = "".join(c.text for c in cells)
    printable = sum(ch.isalnum() for ch in text)
    if printable < MIN_NATIVE_CHARS:
        return False
    bad = sum(1 for ch in text if ch == "�" or 0xE000 <= ord(ch) <= 0xF8FF)
    return bad / max(len(text), 1) <= MAX_GARBAGE_RATIO


def _pix_to_bgr(pix) -> np.ndarray:
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return arr[:, :, :3][:, :, ::-1].copy() if pix.n >= 3 else cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)


def _pil_to_bgr(im: Image.Image) -> np.ndarray:
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    return cv2.cvtColor(np.array(im.convert("RGB")), cv2.COLOR_RGB2BGR)


def _unwrap_http(data: bytes):
    """Some users save a raw HTTP response (headers + body) instead of the file. Return (body, note)."""
    if data[:5] == b"HTTP/":
        cut = data.find(b"\r\n\r\n")
        if 0 < cut < 4096:
            body = data[cut + 4:]
            if sniff_type(body[:16]) != "unsupported":
                return body, "File was saved as a raw HTTP response; the HTTP header was ignored."
    return data, None


def load_document(path, run_ocr: bool = True, use_cache: bool = True, state_hint: str | None = None) -> Document:
    path = Path(path)
    raw = path.read_bytes()
    data, note = _unwrap_http(raw)
    doc = Document(path=str(path), kind=sniff_type(data[:16]), sha256=hashlib.sha256(raw).hexdigest())
    if note:
        doc.warnings.append(note)
    if doc.kind == "unsupported":
        doc.warnings.append("Unsupported file type (not PDF or image).")
        return doc
    try:
        if doc.kind == "pdf":
            _load_pdf(doc, data, run_ocr, use_cache, state_hint)
        else:
            _load_image(doc, data, run_ocr, use_cache, state_hint)
    except Exception as exc:                       # damaged file: report, never crash the scan
        log.warning("Could not read %s: %s", path.name, exc)
        doc.warnings.append(f"File could not be read: {type(exc).__name__}: {exc}")
        if not doc.pages:
            doc.kind = "unsupported"
    counts: dict = {}
    for c in doc.all_cells:
        for k, v in script_counts(c.text).items():
            counts[k] = counts.get(k, 0) + v
    doc.scripts = counts
    return doc


def _indic_second_pass(img, oc: list, info: dict, number: int, use_cache: bool, state_hint, doc: Document):
    """If the first-pass text shows Indic-script noise, read the page again with the matching Indic model."""
    if not (ocr_indic.ENABLED and ocr_indic.needs_indic_pass(oc)):
        return oc, info, ""
    lang = ocr_indic.pick_language(state_hint)
    try:
        oc2, info2 = ocr_indic.ocr_page(img, lang, number, info, use_cache=use_cache)
    except Exception as exc:                          # model missing / download blocked: keep the first pass and say so
        log.warning("Indic OCR (%s) failed on page %s: %s", lang, number, exc)
        doc.warnings.append(f"Page {number}: Indic-script text detected but the {lang} OCR model could not run ({type(exc).__name__}).")
        return oc, info, ""
    if oc2 and info2["mean_conf"] >= 0.6:
        return oc2, info | {"mean_conf": info2["mean_conf"]}, f", read again with the {lang} model"
    return oc, info, ""


def _load_pdf(doc: Document, data: bytes, run_ocr: bool, use_cache: bool, state_hint=None) -> None:
    pdf = pymupdf.open(stream=data, filetype="pdf")
    if pdf.needs_pass:
        doc.warnings.append("PDF is password-protected.")
        doc.kind = "unsupported"
        return
    if len(pdf) > MAX_PAGES:
        doc.warnings.append(f"PDF has {len(pdf)} pages; only the first {MAX_PAGES} were read.")
    for i, pg in enumerate(pdf):
        if i >= MAX_PAGES:
            break
        number = i + 1
        rect = pg.rect * pg.rotation_matrix
        page = Page(number, abs(rect.width), abs(rect.height))
        cells = _native_cells(pg, number)
        if _text_layer_ok(cells):
            page.cells, page.method = _split_label_value_cells(cells), "native"
        elif run_ocr:
            img = _pix_to_bgr(pg.get_pixmap(dpi=OCR_DPI))
            oc, info = ocr.ocr_image(img, number, use_cache=use_cache)
            oc, info, extra = _indic_second_pass(img, oc, info, number, use_cache, state_hint, doc)
            page.cells, page.method = _split_label_value_cells(oc), "ocr"
            page.width, page.height = info["width"], info["height"]
            page.rotation_applied, page.mean_conf = info["rotation"], info["mean_conf"]
            page.angle_spread = info.get("angle_spread", 0.0)
            page.ocr_note = f"deskew {info['deskew_deg']} deg" + (", cached" if info.get("cached") else "") + extra
        else:
            page.method = "none"
            doc.warnings.append(f"Page {number} has no text layer and OCR was disabled.")
        doc.pages.append(page)


def _load_image(doc: Document, data: bytes, run_ocr: bool, use_cache: bool, state_hint=None) -> None:
    im = Image.open(io.BytesIO(data))
    n = getattr(im, "n_frames", 1)
    if n > MAX_PAGES:
        doc.warnings.append(f"Image has {n} frames; only the first {MAX_PAGES} were read.")
    for i in range(min(n, MAX_PAGES)):
        im.seek(i)
        img = _pil_to_bgr(im)
        page = Page(i + 1, img.shape[1], img.shape[0], method="ocr" if run_ocr else "none")
        if run_ocr:
            oc, info = ocr.ocr_image(img, i + 1, use_cache=use_cache)
            oc, info, extra = _indic_second_pass(img, oc, info, i + 1, use_cache, state_hint, doc)
            page.cells, page.width, page.height = _split_label_value_cells(oc), info["width"], info["height"]
            page.rotation_applied, page.mean_conf = info["rotation"], info["mean_conf"]
            page.angle_spread = info.get("angle_spread", 0.0)
            page.ocr_note = f"deskew {info['deskew_deg']} deg" + (", cached" if info.get("cached") else "") + extra
        doc.pages.append(page)
