"""
uploads.py
==========
Manual bill upload (Phase 4.3). The uploaded file goes through exactly the same reader as an Oracle attachment (queued, so the
web request returns at once); the form's consumer number and billing month act as the independent hint that a PO header gives
for Oracle bills.

Validation (a bill is untrusted input):
  * the file TYPE is decided from its content, never from the file name or the browser's content-type;
  * only PDF and common image formats; at most MAX_UPLOAD_BYTES; images must not be absurdly large (decompression bomb);
  * PDFs that contain active content (JavaScript, launch actions, embedded files, form submit) are rejected;
  * the stored name is the SHA-256 of the content: the client's file name is kept only as a display label, so path tricks are impossible;
  * an identical file that was already uploaded is not stored twice.
"""

import hashlib
import io
import os
import re
from datetime import date

from . import audit, config, jobs, storage
from .reader import fields as F
from .reader.loader import sniff_type

MAX_UPLOAD_BYTES = config.MAX_ATTACHMENT_BYTES
MAX_IMAGE_PIXELS = 120_000_000
_ACTIVE_PDF = re.compile(rb"/(JavaScript|JS|Launch|EmbeddedFile|SubmitForm|ImportData|RichMedia)\b")
_EXT = {"pdf": ".pdf", "jpeg": ".jpg", "png": ".png", "tiff": ".tif", "webp": ".webp", "bmp": ".bmp", "gif": ".gif"}


class UploadError(ValueError):
    """The upload was refused; the message is safe to show to the user."""


def _image_format(data: bytes) -> str:
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))                 # reads the header only: the pixels are not decoded before the size check below
        w, h = im.size
        fmt = (im.format or "").lower()
        if w * h <= MAX_IMAGE_PIXELS:
            im.verify()
    except Exception:
        raise UploadError("The image is damaged or cannot be read.") from None
    if w * h > MAX_IMAGE_PIXELS:
        raise UploadError(f"The image is too large ({w}x{h} pixels).")
    if fmt not in _EXT:
        raise UploadError(f"Image format '{fmt}' is not supported (use PDF, JPG, PNG, TIFF, WebP or BMP).")
    return fmt


def validate(data: bytes) -> str:
    """Return the real file format ('pdf', 'jpeg', ...) or raise UploadError."""
    if not data:
        raise UploadError("The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    kind = sniff_type(data[:16])
    if kind == "unsupported":
        raise UploadError("Only PDF or image files (JPG, PNG, TIFF, WebP, BMP) can be uploaded; this file is something else.")
    if kind == "pdf":
        if _ACTIVE_PDF.search(data):
            raise UploadError("This PDF contains active content (scripts, launch actions or embedded files) and was rejected for security reasons. "
                              "Please upload a plain scan or the original bill.")
        return "pdf"
    return _image_format(data)


def plants(conn) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT plant_code, MIN(plant_name) AS plant_name FROM electricity_pos WHERE plant_code IS NOT NULL GROUP BY plant_code ORDER BY plant_code")]


def discom_options() -> list:
    seen, out = set(), []
    for _pat, code, state in F.DISCOMS:
        if code not in seen:
            seen.add(code)
            out.append({"code": code, "state": state})
    return sorted(out, key=lambda d: d["code"])


def save_upload(conn, data: bytes, filename: str, plant_code: str, user: str, consumer_number: str = "", discom_code: str = "",
                po_number: str = "", billing_month: str = "") -> dict:
    """Validate, store and queue a bill. Returns {'upload_id', 'duplicate'}. Raises UploadError."""
    plant_code = (plant_code or "").strip()
    row = conn.execute("SELECT MIN(plant_name) AS n FROM electricity_pos WHERE plant_code = ?", (plant_code,)).fetchone() if plant_code else None
    if not plant_code or row is None or row["n"] is None:
        raise UploadError("Choose the plant this bill belongs to.")
    billing_month = (billing_month or "").strip()
    if billing_month:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", billing_month):
            raise UploadError("Billing month must look like 2026-09.")
        today = date.today()
        if (int(billing_month[:4]), int(billing_month[5:])) > (today.year, today.month):
            raise UploadError("Billing month is in the future.")
    discom_code = (discom_code or "").strip()
    if discom_code and discom_code not in {d["code"] for d in discom_options()}:
        raise UploadError("Unknown electricity board.")
    consumer_number = (consumer_number or "").strip()[:60]
    po_number = (po_number or "").strip()[:40]
    if po_number and not re.fullmatch(r"[A-Za-z0-9._\-/ ]+", po_number):
        raise UploadError("PO number contains invalid characters.")
    fmt = validate(data)
    sha = hashlib.sha256(data).hexdigest()
    prior = conn.execute("SELECT id, extraction_id, status FROM electricity_uploads WHERE sha256 = ? AND status <> 'REJECTED' ORDER BY id LIMIT 1", (sha,)).fetchone()
    if prior:
        audit.log(conn, user, "UPLOAD_DUPLICATE", "upload", prior["id"], {"file": os.path.basename(filename or "")[:120], "plant": plant_code})
        return {"upload_id": prior["id"], "duplicate": True, "extraction_id": prior["extraction_id"]}
    target = config.STORAGE_DIR / "uploads" / sha[:2] / f"{sha}{_EXT[fmt]}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    safe_name = re.sub(r"[^\w.\- ()]", "_", os.path.basename(filename or "bill"))[:120]
    uid = conn.execute(
        "INSERT INTO electricity_uploads (plant_code, plant_name, consumer_number, discom_code, po_number, billing_month, original_name, sha256,"
        " local_path, size_bytes, content_type, uploaded_by, uploaded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (plant_code, row["n"], consumer_number or None, discom_code or None, po_number or None, billing_month or None, safe_name, sha,
         str(target.relative_to(config.STORAGE_DIR)), len(data), fmt, user, storage.now_iso())).lastrowid
    jobs.enqueue(conn, "PROCESS_UPLOAD", uid)
    audit.log(conn, user, "UPLOAD_BILL", "upload", uid, {"file": safe_name, "plant": plant_code, "consumer": consumer_number, "po": po_number, "month": billing_month})
    return {"upload_id": uid, "duplicate": False, "extraction_id": None}
