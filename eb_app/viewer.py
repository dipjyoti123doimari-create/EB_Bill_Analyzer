"""
viewer.py
=========
Renders one page of the ORIGINAL bill as a PNG, optionally with a rectangle around the place a value was read from — the source
evidence for the Universal Bill Viewer. The original file is only read.

Coordinates: a text-layer page stores positions in PDF points of the rotation-corrected page, so the box is exact. A scanned page stores
positions in pixels of the image that was OCR'd; the box is exact when no turning/deskew was needed and approximate otherwise (the
viewer says so).
"""

import functools
import io
import json
import re

import pymupdf
from PIL import Image, ImageDraw

from . import config
from .reader.loader import OCR_DPI, load_document, sniff_type

MAX_SIDE = 2400


def original_bytes(path):
    """(bytes, mime) of a stored original. A file saved as a raw HTTP response is served without its HTTP header."""
    from .reader.loader import _unwrap_http
    data, _ = _unwrap_http(open(path, "rb").read())
    head = data[:16]
    if head.startswith(b"%PDF"):
        mime = "application/pdf"
    elif head.startswith(b"\x89PNG"):
        mime = "image/png"
    elif head.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif head[:4] in (b"II*\x00", b"MM\x00*"):
        mime = "image/tiff"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        mime = "image/webp"
    elif head[:2] == b"BM":
        mime = "image/bmp"
    else:
        mime = "application/octet-stream"
    return data, mime


@functools.lru_cache(maxsize=12)
def _page_info(path: str):
    doc = load_document(path, run_ocr=True, use_cache=True)
    return {p.number: {"w": p.width, "h": p.height, "method": p.method, "rot": p.rotation_applied, "note": p.ocr_note} for p in doc.pages}, doc.kind


def render(path, page_no: int, bbox=None, label: str = ""):
    """(png bytes, note). page_no is 1-based."""
    data, _mime = original_bytes(path)
    kind = sniff_type(data[:16])
    info, _ = _page_info(str(path))
    pinfo = info.get(page_no)
    note = ""
    if kind == "pdf":
        pdf = pymupdf.open(stream=data, filetype="pdf")
        if not 1 <= page_no <= len(pdf):
            raise ValueError("no such page")
        pg = pdf[page_no - 1]
        native = pinfo is None or pinfo["method"] != "ocr"
        zoom = 1.7 if native else OCR_DPI / 72.0
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        if native:
            sx = sy = zoom
        else:
            sx = pix.width / pinfo["w"] if pinfo["rot"] in (0, 180) else 0
            sy = pix.height / pinfo["h"] if pinfo["rot"] in (0, 180) else 0
            if pinfo["rot"] or re.search(r"deskew\s+(?!0(\.0+)?\b)", pinfo.get("note", "")):
                note = "This scan was turned or straightened before reading, so the highlighted box is only approximate."
            if pinfo["rot"] not in (0, 180):
                bbox = None
                note = "This scan was rotated before reading, so the position of the value cannot be drawn on the original."
    else:
        im = Image.open(io.BytesIO(data))
        im.seek(page_no - 1)
        img = im.convert("RGB")
        sx = img.width / pinfo["w"] if pinfo and pinfo["w"] else 1.0
        sy = img.height / pinfo["h"] if pinfo and pinfo["h"] else 1.0
        if pinfo and pinfo["rot"]:
            bbox = None
            note = "This image was rotated before reading, so the position of the value cannot be drawn on the original."
    if bbox and sx and sy and len(bbox) == 4 and any(bbox):
        d = ImageDraw.Draw(img, "RGBA")
        x0, y0, x1, y1 = bbox[0] * sx, bbox[1] * sy, bbox[2] * sx, bbox[3] * sy
        pad = 4
        d.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], outline=(255, 40, 60, 255), width=3, fill=(255, 220, 0, 70))
    scale = min(1.0, MAX_SIDE / max(img.size))
    if scale < 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=False)
    return out.getvalue(), note


def original_path(conn, extraction_id: int):
    """Absolute path of the stored original behind an extraction (Oracle attachment or manual upload), or None."""
    e = conn.execute("SELECT attachment_id, upload_id FROM electricity_bill_extractions WHERE id = ?", (extraction_id,)).fetchone()
    if e is None:
        return None
    if e["attachment_id"] is not None:
        r = conn.execute("SELECT local_path, file_name FROM electricity_attachments WHERE attached_document_id = ?", (e["attachment_id"],)).fetchone()
    else:
        r = conn.execute("SELECT local_path, original_name AS file_name FROM electricity_uploads WHERE id = ?", (e["upload_id"],)).fetchone()
    if not r or not r["local_path"]:
        return None
    p = (config.STORAGE_DIR / r["local_path"]).resolve()
    if config.STORAGE_DIR.resolve() not in p.parents:          # never serve anything outside the document store
        return None
    return (p, r["file_name"]) if p.is_file() else None
