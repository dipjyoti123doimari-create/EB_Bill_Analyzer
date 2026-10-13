"""
store.py
========
Runs the reader on stored attachments and persists the result.

* process_attachment()   one attachment -> extraction row + per-field evidence + charge lines
* process_pending()      every downloaded attachment not yet read by the current reader version
* apply_correction()     human correction: original and corrected value kept, audit row written,
                         regression candidate recorded (never changes extraction behaviour by itself)
* mark_verified()        human confirms an extracted value
* approve_correction()   a reviewer approves a correction for the regression dataset
* export_regression()    JSON-lines dataset of approved corrections

Nothing is ever overwritten: a new reader version creates a new extraction row; the original file is only read.
"""

import hashlib
import json
import logging
import re
from datetime import date

from .. import audit, bills, config, settings, storage
from . import fields as F
from .loader import load_document
from .model import Document
from . import pipeline
from .pipeline import analyze_document
from .textnorm import norm, parse_dates, parse_numbers

log = logging.getLogger(__name__)

READER_VERSION = "reader-2.2"          # 2.1: penalty lines whose number is a PF value / rate / bill total are UNREADABLE, not an amount; 2.2: 'Fuel Adjustment' is its own charge category, 3.234.47 = 3,234.47

# A bill needs these to be usable by the analysis engine (Phase 3).
REQUIRED_FIELDS = ("consumer_number", "billing_period", "total_payable")
REVIEW_FIELDS = REQUIRED_FIELDS + ("kwh", "kvah", "pf_billed", "pf", "contract_demand", "max_demand", "bill_date")


# --------------------------------------------------------------------------- helpers
def state_from_supplier(name: str | None) -> str | None:
    """The supplier name on the PO usually names the DISCOM ('MSEDCL-PUNE-2', 'KSEB WAYANAD'); its state picks the Indic OCR model."""
    text = (name or "").lower()
    for pattern, _code, state in F.DISCOMS:
        if re.search(pattern, text):
            return state
    return None


def _fnum(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return " .. ".join(str(x) for x in v)
    return str(v)


def layout_signature(doc: Document, result: dict) -> str:
    """Stable fingerprint of a bill TEMPLATE: issuer, page count, scan/native, and WHICH fields were found by WHICH kind of relation
    (e.g. kwh by 'table', pf by 'inline'). Two bills of the same board and template share it, so corrections can be grouped by layout."""
    shape = sorted(f"{k}:{(v.get('relation') or v.get('method') or '').split(':')[0]}" for k, v in result["fields"].items()
                   if k in ("consumer_number", "bill_date", "billing_period", "kwh", "kvah", "pf_billed", "pf", "contract_demand", "max_demand",
                            "total_payable") and v.get("relation") != "derived")
    methods = "".join(sorted({p.method[:1] for p in doc.pages}))
    base = "|".join([result["classification"].get("discom") or "?", str(len(doc.pages)), methods] + shape)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def bill_status(result: dict) -> str:
    doc = result["document"]
    if doc["kind"] == "unsupported":
        return "UNSUPPORTED"
    if result["classification"]["verdict"] == "NOT_ELECTRICITY_BILL":
        return "NOT_A_BILL"
    f = result["fields"]
    if not f:
        return "FAILED"
    has_energy = "kwh" in f or "kvah" in f
    has_pf = any(k in f for k in ("pf_billed", "pf", "pf_calculated"))
    missing = [k for k in REQUIRED_FIELDS if k not in f] + ([] if has_energy else ["energy"]) + ([] if has_pf else ["pf"])
    if missing:
        return "PARTIAL"
    if any(f[k].get("verification") == "NEEDS_REVIEW" for k in REVIEW_FIELDS if k in f):
        return "NEEDS_REVIEW"
    if result["flags"]:
        return "NEEDS_REVIEW"
    return "VERIFIED"


def po_crosscheck(result: dict, po: dict | None) -> dict:
    """Compare what the bill says with what the Oracle PO header says. Differences are flagged, never silently resolved."""
    out = {"checked": po is not None, "flags": []}
    if not po:
        return out
    f = result["fields"]
    hint = (po.get("consumer_hint") or "").strip()
    cn = f.get("consumer_number", {}).get("value")
    if hint and cn:
        a, b = norm(hint), norm(str(cn))
        if a and b and a not in b and b not in a:
            out["flags"].append(f"Consumer number on the bill ({cn}) differs from the PO header ({hint})")
    bp = f.get("billing_period", {}).get("value")
    if po.get("billing_start") and po.get("billing_end") and bp:
        try:
            s, e = date.fromisoformat(bp[0]), date.fromisoformat(bp[1])
            ps, pe = date.fromisoformat(po["billing_start"]), date.fromisoformat(po["billing_end"])
            overlap = (min(e, pe) - max(s, ps)).days
            if overlap < 10:
                out["flags"].append(f"Billing period on the bill ({bp[0]}..{bp[1]}) does not match the PO period ({ps}..{pe})")
        except ValueError:
            pass
    out["po_period"] = [po.get("billing_start"), po.get("billing_end")]
    out["plant"] = po.get("plant_code")
    return out


def _apply_po_evidence(result: dict, po: dict | None, pocheck: dict) -> None:
    """The Oracle PO header was typed by a person, independently of the bill. Agreement with the bill is real corroboration (raises
    confidence); a bill that prints no period at all gets the PO period as a clearly-labelled, review-flagged fallback."""
    if not po:
        return
    f = result["fields"]
    mismatch = {fl.split()[0] for fl in pocheck["flags"]}
    hint = (po.get("consumer_hint") or "").strip()
    cn = f.get("consumer_number")
    if cn and hint and "Consumer" not in mismatch and norm(hint) and (norm(hint) in norm(str(cn["value"])) or norm(str(cn["value"])) in norm(hint)):
        cn["confidence"] = min(0.99, cn["confidence"] + 0.12)
        cn["evidence"] = (cn.get("evidence") or "") + " | agrees with the consumer number on the PO header"
        if cn["confidence"] >= pipeline.CRITICAL_CONFIDENCE and cn.get("verification") == "NEEDS_REVIEW":
            cn["verification"] = "AUTO"
    bp = f.get("billing_period")
    if bp and po.get("billing_start") and po.get("billing_end") and "Billing" not in mismatch and bp.get("relation") != "derived":
        try:
            s, e = date.fromisoformat(bp["value"][0]), date.fromisoformat(bp["value"][1])
            ps, pe = date.fromisoformat(po["billing_start"]), date.fromisoformat(po["billing_end"])
            if (min(e, pe) - max(s, ps)).days >= 20:
                bp["confidence"] = min(0.99, bp["confidence"] + 0.1)
                bp["evidence"] = (bp.get("evidence") or "") + " | agrees with the period on the PO header"
                if bp["confidence"] >= pipeline.CRITICAL_CONFIDENCE and bp.get("verification") == "NEEDS_REVIEW":
                    bp["verification"] = "AUTO"
        except ValueError:
            pass
    if "billing_period" not in f and po.get("billing_start") and po.get("billing_end"):
        label = po.get("label") or "Oracle PO header (ATTRIBUTE1/ATTRIBUTE2)"
        method = "manual_upload_form" if po.get("label") else "oracle_po_header"
        f["billing_period"] = {"value": [po["billing_start"], po["billing_end"]], "raw": f"{po['billing_start']} .. {po['billing_end']}", "unit": None,
                               "page": 0, "bbox": [0, 0, 0, 0], "label": label, "relation": "po_header",
                               "method": method, "ocr_conf": 1.0, "confidence": 0.7, "status": "FOUND", "cross_checked": False, "alternatives": [],
                               "evidence": f"this bill prints no billing period; the period comes from {label}", "verification": "NEEDS_REVIEW"}


# --------------------------------------------------------------------------- processing
def _insert_result(conn, att_id: int | None, sha: str, doc: Document, result: dict | None, error: str | None, po: dict | None,
                   upload_id: int | None = None) -> int:
    now = storage.now_iso()
    if result is None:
        status, bstatus = "FAILED", "FAILED"
        cur = conn.execute(
            "INSERT INTO electricity_bill_extractions (attachment_id, upload_id, sha256, reader_version, status, bill_status, doc_kind, error, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)", (att_id, upload_id, sha, READER_VERSION, status, bstatus, doc.kind if doc else None, error, now))
        bills.refresh_bill(conn, cur.lastrowid)
        return cur.lastrowid
    pocheck = po_crosscheck(result, po)
    if pocheck["flags"]:
        result["flags"] = list(result["flags"]) + pocheck["flags"]
    _apply_po_evidence(result, po, pocheck)
    bstatus = bill_status(result)
    # an identical file that was already read in this reader version is a duplicate (whether it came from Oracle or a manual upload)
    dup = conn.execute(
        "SELECT id, attachment_id, upload_id FROM electricity_bill_extractions WHERE sha256 = ? AND reader_version = ? AND archived = 0 "
        "AND COALESCE(attachment_id, -1) <> COALESCE(?, -2) AND COALESCE(upload_id, -1) <> COALESCE(?, -2) "
        "AND bill_status <> 'DUPLICATE' ORDER BY id LIMIT 1", (sha, READER_VERSION, att_id, upload_id)).fetchone()
    if dup and bstatus not in ("UNSUPPORTED", "NOT_A_BILL"):
        bstatus = "DUPLICATE"
    d = result["document"]
    ocr_pages = [p for p in d["pages"] if p["method"] == "ocr"]
    cur = conn.execute(
        "INSERT INTO electricity_bill_extractions (attachment_id, sha256, reader_version, status, bill_status, duplicate_of, doc_kind, pages,"
        " ocr_pages, mean_ocr_conf, discom_code, state, issuer_type, layout_signature, billing_basis, basis_confidence, needs_review_count,"
        " missing_json, flags_json, warnings_json, po_check_json, result_json, created_at, upload_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (att_id, sha, READER_VERSION, "OK", bstatus, dup["id"] if dup and bstatus == "DUPLICATE" else None, d["kind"], len(d["pages"]),
         len(ocr_pages), (sum(p["mean_conf"] for p in ocr_pages) / len(ocr_pages)) if ocr_pages else None,
         result["classification"].get("discom"), result["classification"].get("state"), result["classification"].get("issuer_type"),
         result.get("layout_signature"), result["billing_basis"]["basis"], result["billing_basis"]["confidence"],
         len(result["needs_review_fields"]), json.dumps(result["missing_fields"]), json.dumps(result["flags"]),
         json.dumps(d["warnings"]), json.dumps(pocheck), json.dumps(result, default=str), now, upload_id))
    ext_id = cur.lastrowid
    for name, f in result["fields"].items():
        val = f.get("value")
        conn.execute(
            "INSERT INTO electricity_bill_fields (extraction_id, field_name, status, value_text, value_num, unit_original, unit_normalized,"
            " original_label, page, bbox_json, method, ocr_conf, confidence, verification_status, evidence, alternatives_json, extracted_value_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ext_id, name, f.get("status", "FOUND"), _text(val), _fnum(val), f.get("raw") if isinstance(f.get("raw"), str) else None,
             f.get("unit"), f.get("label"), f.get("page"), json.dumps(f.get("bbox")), f.get("method"), f.get("ocr_conf"),
             f.get("confidence"), f.get("verification", "AUTO"), f.get("evidence"), json.dumps(f.get("alternatives", []), default=str), _text(val)))
    # a field that is not on the bill is stored as NOT_FOUND; if the page itself could not be read it is UNREADABLE (never a silent zero)
    unreadable = any(p["method"] == "none" for p in d["pages"]) or any("script" in w.lower() for w in d["warnings"]) or \
        any(p["method"] == "ocr" and p["mean_conf"] < 0.6 for p in d["pages"])
    expected = ("consumer_number", "bill_date", "billing_period", "kwh", "kvah", "pf", "contract_demand", "max_demand", "total_payable")
    have = set(result["fields"]) | ({"pf"} if any(k in result["fields"] for k in ("pf_billed", "pf_calculated")) else set())
    for name in expected:
        if name not in have:
            conn.execute("INSERT INTO electricity_bill_fields (extraction_id, field_name, status, verification_status, evidence) VALUES (?,?,?,?,?)",
                         (ext_id, name, "UNREADABLE" if unreadable else "NOT_FOUND",
                          "NEEDS_REVIEW" if name in REQUIRED_FIELDS else "AUTO",
                          "the page could not be read reliably" if unreadable else "not printed on the bill / not found by the reader"))
    for ln in result["charge_lines"]:
        conn.execute("INSERT INTO electricity_charge_lines (extraction_id, label, category, amount, numbers_json, page, bbox_json, conf)"
                     " VALUES (?,?,?,?,?,?,?,?)", (ext_id, ln["label"], ln["category"], ln["amount"], json.dumps(ln["numbers"]),
                                                  ln["page"], json.dumps(ln["bbox"]), ln["conf"]))
    if error:
        audit.record_error(conn, "READ", "attachment" if att_id else "upload", att_id or upload_id, error)
    if dup and bstatus == "DUPLICATE":
        _link_duplicate(conn, ext_id, att_id, upload_id, dup)
    bills.refresh_bill(conn, ext_id)
    if upload_id is not None:
        conn.execute("UPDATE electricity_uploads SET extraction_id = ?, status = 'PROCESSED' WHERE id = ?", (ext_id, upload_id))
    return ext_id


def _link_duplicate(conn, ext_id, att_id, upload_id, dup) -> None:
    """A manual upload and an Oracle attachment with the same content are the same bill: record the link on the upload and refresh the
    surviving bill so that it shows the Oracle PO."""
    if upload_id is not None and dup["attachment_id"] is not None:
        conn.execute("UPDATE electricity_uploads SET linked_attachment_id = ?, link_note = ? WHERE id = ?",
                     (dup["attachment_id"], "this file is already attached to a PO in Oracle EBS", upload_id))
    elif att_id is not None and dup["upload_id"] is not None:
        conn.execute("UPDATE electricity_uploads SET linked_attachment_id = ?, link_note = ? WHERE id = ?",
                     (att_id, "this manually uploaded bill was later found attached to a PO in Oracle EBS", dup["upload_id"]))
        bills.refresh_bill(conn, dup["id"])


def _po_hint_from_row(row) -> dict:
    return {"consumer_hint": row["consumer_hint"], "billing_start": row["billing_start"], "billing_end": row["billing_end"], "plant_code": row["plant_code"]}


def _read_file(path, run_ocr, state_hint, threshold):
    doc = load_document(path, run_ocr=run_ocr, state_hint=state_hint)
    result = analyze_document(doc, threshold)
    result["layout_signature"] = layout_signature(doc, result)
    return doc, result


def process_attachment(attachment_id: int, db_path=None, run_ocr: bool = True, force: bool = False) -> dict:
    """Read one downloaded attachment. Returns {'extraction_id', 'bill_status', ...}. Safe to call repeatedly.
    force=True re-reads it: the earlier extraction (with any corrections) is ARCHIVED, never deleted."""
    with storage.session(db_path) as conn:
        row = conn.execute(
            "SELECT a.attached_document_id, a.local_path, a.fetch_status, a.sha256, p.consumer_hint, p.billing_start, p.billing_end, p.plant_code, p.supplier_name "
            "FROM electricity_attachments a JOIN electricity_pos p ON p.po_header_id = a.po_header_id WHERE a.attached_document_id = ?",
            (attachment_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown attachment {attachment_id}")
        if row["fetch_status"] != "DOWNLOADED" or not row["local_path"]:
            raise ValueError(f"Attachment {attachment_id} has not been downloaded (status {row['fetch_status']})")
        prev = conn.execute("SELECT id, bill_status FROM electricity_bill_extractions WHERE attachment_id = ? AND sha256 = ? AND reader_version = ? "
                            "AND archived = 0", (attachment_id, row["sha256"], READER_VERSION)).fetchone()
        if prev and not force:
            return {"extraction_id": prev["id"], "bill_status": prev["bill_status"], "reused": True}
        threshold = settings.get(conn, "critical_confidence")
    doc = result = None
    error = None
    try:
        doc, result = _read_file(config.STORAGE_DIR / row["local_path"], run_ocr, state_from_supplier(row["supplier_name"]), threshold)
    except Exception as exc:                      # a damaged file must never stop the scan
        log.exception("Reading attachment %s failed", attachment_id)
        error = f"{type(exc).__name__}: {exc}"
    with storage.session(db_path) as conn:
        if prev and force:
            _archive(conn, prev["id"])
        ext_id = _insert_result(conn, attachment_id, row["sha256"], doc, result, error, _po_hint_from_row(row))
        st = conn.execute("SELECT bill_status FROM electricity_bill_extractions WHERE id = ?", (ext_id,)).fetchone()["bill_status"]
    return {"extraction_id": ext_id, "bill_status": st, "reused": False, "error": error}


def _archive(conn, extraction_id) -> None:
    """Keep the old extraction (fields, corrections, evidence) but take it out of every list and total."""
    conn.execute("UPDATE electricity_bill_extractions SET archived = 1, reader_version = reader_version || '#archived' || id WHERE id = ?", (extraction_id,))
    bills.refresh_bill(conn, extraction_id)


def process_upload(upload_id: int, db_path=None, run_ocr: bool = True, force: bool = False) -> dict:
    """Read a manually uploaded bill with exactly the same engine. The upload form's consumer number / billing month act as the
    independent hint that an Oracle PO header gives for Oracle bills."""
    from datetime import date as _d
    import calendar
    with storage.session(db_path) as conn:
        u = conn.execute("SELECT * FROM electricity_uploads WHERE id = ?", (upload_id,)).fetchone()
        if u is None:
            raise ValueError(f"Unknown upload {upload_id}")
        prev = conn.execute("SELECT id, bill_status FROM electricity_bill_extractions WHERE upload_id = ? AND sha256 = ? AND reader_version = ? AND archived = 0",
                            (upload_id, u["sha256"], READER_VERSION)).fetchone()
        if prev and not force:
            return {"extraction_id": prev["id"], "bill_status": prev["bill_status"], "reused": True}
        conn.execute("UPDATE electricity_uploads SET status = 'PROCESSING', error = NULL WHERE id = ?", (upload_id,))
        threshold = settings.get(conn, "critical_confidence")
    po = {"consumer_hint": u["consumer_number"], "plant_code": u["plant_code"], "billing_start": None, "billing_end": None,
          "label": "the billing month entered on the upload form"}
    if u["billing_month"]:
        try:
            y, m = (int(x) for x in u["billing_month"].split("-"))
            po["billing_start"], po["billing_end"] = _d(y, m, 1).isoformat(), _d(y, m, calendar.monthrange(y, m)[1]).isoformat()
        except ValueError:
            pass
    state = None
    for pat, code, st in F.DISCOMS:
        if u["discom_code"] and code == u["discom_code"]:
            state = st
            break
    doc = result = None
    error = None
    try:
        doc, result = _read_file(config.STORAGE_DIR / u["local_path"], run_ocr, state, threshold)
    except Exception as exc:
        log.exception("Reading upload %s failed", upload_id)
        error = f"{type(exc).__name__}: {exc}"
    with storage.session(db_path) as conn:
        if prev and force:
            _archive(conn, prev["id"])
        ext_id = _insert_result(conn, None, u["sha256"], doc, result, error, po, upload_id=upload_id)
        st = conn.execute("SELECT bill_status FROM electricity_bill_extractions WHERE id = ?", (ext_id,)).fetchone()["bill_status"]
        conn.execute("UPDATE electricity_uploads SET extraction_id = ?, status = ?, error = ? WHERE id = ?",
                     (ext_id, "FAILED" if st == "FAILED" else "PROCESSED", error, upload_id))
    return {"extraction_id": ext_id, "bill_status": st, "reused": False, "error": error}


def retry_extraction(extraction_id: int, user: str, db_path=None, run_ocr: bool = True) -> dict:
    """Re-read the document of an extraction with the current reader. The old extraction is archived, not deleted."""
    with storage.session(db_path) as conn:
        e = conn.execute("SELECT attachment_id, upload_id FROM electricity_bill_extractions WHERE id = ?", (extraction_id,)).fetchone()
        if e is None:
            raise ValueError("unknown extraction")
        n_cor = conn.execute("SELECT COUNT(*) FROM electricity_manual_corrections WHERE extraction_id = ?", (extraction_id,)).fetchone()[0]
        audit.log(conn, user, "RETRY_EXTRACTION", "extraction", extraction_id, {"corrections_on_archived_version": n_cor})
    if e["attachment_id"] is not None:
        r = process_attachment(e["attachment_id"], db_path=db_path, run_ocr=run_ocr, force=True)
    else:
        r = process_upload(e["upload_id"], db_path=db_path, run_ocr=run_ocr, force=True)
    r["archived_corrections"] = n_cor
    return r


def pending_attachment_ids(limit: int, db_path=None, only_eligible: bool = True) -> list:
    with storage.session(db_path) as conn:
        return [r[0] for r in conn.execute(
            "SELECT a.attached_document_id FROM electricity_attachments a JOIN electricity_pos p ON p.po_header_id = a.po_header_id "
            "WHERE a.fetch_status = 'DOWNLOADED' " + ("AND p.eligible = 1 " if only_eligible else "") +
            "AND NOT EXISTS (SELECT 1 FROM electricity_bill_extractions e WHERE e.attachment_id = a.attached_document_id "
            "AND e.sha256 = a.sha256 AND e.archived = 0) "              # any version: older ones are handled by outdated_extraction_ids()
            "ORDER BY a.att_created DESC LIMIT ?", (limit,))]


def outdated_extraction_ids(limit: int, db_path=None) -> list:
    """Extractions made by an older reader version that no person has touched: safe to re-read (the old one is archived, not deleted).
    Extractions with human corrections / verifications are never replaced automatically."""
    with storage.session(db_path) as conn:
        return [r[0] for r in conn.execute(
            "SELECT e.id FROM electricity_bill_extractions e WHERE e.archived = 0 AND e.reader_version NOT LIKE '%#archived%' AND e.reader_version <> ? "
            "AND NOT EXISTS (SELECT 1 FROM electricity_manual_corrections c WHERE c.extraction_id = e.id) "
            "AND NOT EXISTS (SELECT 1 FROM electricity_bill_fields f WHERE f.extraction_id = e.id AND f.verification_status IN ('VERIFIED','CORRECTED')) "
            "ORDER BY e.id LIMIT ?", (READER_VERSION, limit))]


def reread_outdated(extraction_id: int, db_path=None, run_ocr: bool = True) -> dict:
    with storage.session(db_path) as conn:
        e = conn.execute("SELECT attachment_id, upload_id FROM electricity_bill_extractions WHERE id = ?", (extraction_id,)).fetchone()
        _archive(conn, extraction_id)
    if e["attachment_id"] is not None:
        return process_attachment(e["attachment_id"], db_path=db_path, run_ocr=run_ocr)
    return process_upload(e["upload_id"], db_path=db_path, run_ocr=run_ocr)


def process_pending(limit: int = 50, db_path=None, run_ocr: bool = True, only_eligible: bool = True, include_outdated: bool = True) -> dict:
    counts: dict = {}
    n = 0
    for i in pending_attachment_ids(limit, db_path, only_eligible):
        r = process_attachment(i, db_path=db_path, run_ocr=run_ocr)
        counts[r["bill_status"]] = counts.get(r["bill_status"], 0) + 1
        n += 1
    if include_outdated and n < limit:
        for i in outdated_extraction_ids(limit - n, db_path):
            r = reread_outdated(i, db_path=db_path, run_ocr=run_ocr)
            counts[r["bill_status"]] = counts.get(r["bill_status"], 0) + 1
            n += 1
    return {"processed": n, "by_status": counts}


# --------------------------------------------------------------------------- human corrections
def _coerce(field: str, raw: str):
    """Parse a user's correction into the value type of the field; raise ValueError if it cannot be read."""
    spec = F.FIELDS.get(field)
    kind = spec.kind if spec else "number"
    raw = (raw or "").strip()
    if kind == "number":
        n = parse_numbers(raw)
        if not n:
            raise ValueError(f"'{raw}' is not a number")
        return n[0]
    if kind == "date":
        d = parse_dates(raw)
        if not d:
            raise ValueError(f"'{raw}' is not a date")
        return d[0].isoformat()
    if kind == "daterange":
        d = parse_dates(raw)
        if len(d) < 2:
            raise ValueError("a billing period needs two dates")
        return [d[0].isoformat(), d[1].isoformat()]
    if not raw:
        raise ValueError("value is empty")
    return raw


def _context_cells(conn, ext_row, field_row, radius: float = 90.0) -> list:
    """Texts of the cells around the corrected value on the page (for the regression dataset)."""
    try:
        att = conn.execute("SELECT local_path FROM electricity_attachments WHERE attached_document_id = ?", (ext_row["attachment_id"],)).fetchone()
        doc = load_document(config.STORAGE_DIR / att["local_path"], run_ocr=True)
        bbox = json.loads(field_row["bbox_json"] or "null")
        if not bbox:
            return []
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        pg = next((p for p in doc.pages if p.number == field_row["page"]), None)
        if not pg:
            return []
        near = sorted((c for c in pg.cells if abs(c.xc - cx) <= radius * 3 and abs(c.yc - cy) <= radius),
                      key=lambda c: (c.yc - cy) ** 2 + (c.xc - cx) ** 2)[:14]
        return [c.text for c in near]
    except Exception:
        return []


def apply_correction(extraction_id: int, field: str, value, user: str, unit: str | None = None, reason: str | None = None, db_path=None) -> dict:
    """Record a human correction. The extracted value is preserved; the correction is audited and queued as a regression candidate."""
    if not user:
        raise ValueError("a correction needs the name of the person making it")
    with storage.session(db_path) as conn:
        ext = conn.execute("SELECT * FROM electricity_bill_extractions WHERE id = ?", (extraction_id,)).fetchone()
        if ext is None:
            raise ValueError(f"Unknown extraction {extraction_id}")
        new = _coerce(field, value if isinstance(value, str) else json.dumps(value) if isinstance(value, list) else str(value))
        fr = conn.execute("SELECT * FROM electricity_bill_fields WHERE extraction_id = ? AND field_name = ?", (extraction_id, field)).fetchone()
        now = storage.now_iso()
        original = fr["extracted_value_text"] if fr else None
        if fr:
            conn.execute(
                "UPDATE electricity_bill_fields SET value_text = ?, value_num = ?, unit_normalized = COALESCE(?, unit_normalized), status = ?, "
                "verification_status = 'CORRECTED', corrected_by = ?, corrected_at = ?, confidence = 1.0 WHERE id = ?",
                (_text(new), _fnum(new), unit, "ZERO" if _fnum(new) == 0 else "FOUND", user, now, fr["id"]))
        else:
            conn.execute(
                "INSERT INTO electricity_bill_fields (extraction_id, field_name, status, value_text, value_num, unit_normalized, method, confidence,"
                " verification_status, evidence, corrected_by, corrected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (extraction_id, field, "ZERO" if _fnum(new) == 0 else "FOUND", _text(new), _fnum(new), unit, "manual", 1.0, "CORRECTED", "entered by a person", user, now))
            fr = conn.execute("SELECT * FROM electricity_bill_fields WHERE extraction_id = ? AND field_name = ?", (extraction_id, field)).fetchone()
        ctx = _context_cells(conn, ext, fr)
        conn.execute(
            "INSERT INTO electricity_manual_corrections (extraction_id, field_name, original_value, corrected_value, original_unit, corrected_unit,"
            " corrected_by, corrected_at, reason, discom_code, layout_signature, page, bbox_json, context_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (extraction_id, field, original, _text(new), fr["unit_original"], unit, user, now, reason, ext["discom_code"], ext["layout_signature"],
             fr["page"], fr["bbox_json"], json.dumps(ctx)))
        _refresh_bill_status(conn, extraction_id)
        audit.log(conn, user, "CORRECT_FIELD", "extraction", extraction_id, {"field": field, "original": original, "corrected": _text(new), "unit": unit, "reason": reason})
        bills.refresh_bill(conn, extraction_id)
    return {"field": field, "original": original, "corrected": new}


def mark_verified(extraction_id: int, field: str, user: str, db_path=None) -> None:
    with storage.session(db_path) as conn:
        n = conn.execute("UPDATE electricity_bill_fields SET verification_status = 'VERIFIED', corrected_by = ?, corrected_at = ? "
                         "WHERE extraction_id = ? AND field_name = ? AND verification_status <> 'CORRECTED'",
                         (user, storage.now_iso(), extraction_id, field)).rowcount
        if n:
            _refresh_bill_status(conn, extraction_id)
            audit.log(conn, user, "VERIFY_FIELD", "extraction", extraction_id, {"field": field})
            bills.refresh_bill(conn, extraction_id)


BASES = ("kWh", "kVAh", "Mixed", "Unknown")


def set_billing_basis(extraction_id: int, basis: str, user: str, reason: str | None = None, db_path=None) -> None:
    """A person decides the billing basis the reader could not (or got wrong). The original value stays in the audit trail."""
    if basis not in BASES:
        raise ValueError(f"basis must be one of {BASES}")
    with storage.session(db_path) as conn:
        e = conn.execute("SELECT billing_basis FROM electricity_bill_extractions WHERE id = ?", (extraction_id,)).fetchone()
        if e is None:
            raise ValueError("unknown extraction")
        conn.execute("UPDATE electricity_bill_extractions SET billing_basis = ?, basis_confidence = 1.0 WHERE id = ?", (basis, extraction_id))
        audit.log(conn, user, "SET_BILLING_BASIS", "extraction", extraction_id, {"original": e["billing_basis"], "corrected": basis, "reason": reason})
        bills.refresh_bill(conn, extraction_id)


def _refresh_bill_status(conn, extraction_id: int) -> None:
    rows = {r["field_name"]: r["verification_status"] for r in conn.execute(
        "SELECT field_name, verification_status FROM electricity_bill_fields WHERE extraction_id = ?", (extraction_id,))}
    ext = conn.execute("SELECT bill_status FROM electricity_bill_extractions WHERE id = ?", (extraction_id,)).fetchone()
    if ext["bill_status"] not in ("NEEDS_REVIEW", "PARTIAL"):
        return
    has_energy = "kwh" in rows or "kvah" in rows
    has_pf = any(k in rows for k in ("pf_billed", "pf", "pf_calculated"))
    missing = [k for k in REQUIRED_FIELDS if k not in rows] or ([] if has_energy else ["energy"]) or ([] if has_pf else ["pf"])
    if missing:
        return
    pending = [k for k in REVIEW_FIELDS if rows.get(k) == "NEEDS_REVIEW"]
    flags = json.loads(conn.execute("SELECT flags_json FROM electricity_bill_extractions WHERE id = ?", (extraction_id,)).fetchone()[0] or "[]")
    if not pending and not any(f for f in flags if not f.startswith("Printed PF")):
        conn.execute("UPDATE electricity_bill_extractions SET bill_status = 'VERIFIED', needs_review_count = 0 WHERE id = ?", (extraction_id,))
    else:
        conn.execute("UPDATE electricity_bill_extractions SET needs_review_count = ? WHERE id = ?", (len(pending), extraction_id))


def approve_correction(correction_id: int, approved: bool = True, db_path=None) -> None:
    with storage.session(db_path) as conn:
        conn.execute("UPDATE electricity_manual_corrections SET regression_status = ? WHERE id = ?",
                     ("APPROVED" if approved else "REJECTED", correction_id))


def export_regression(path, db_path=None) -> int:
    """Write approved corrections as JSON lines (one verified case each) for the regression suite."""
    n = 0
    with storage.session(db_path) as conn, open(path, "w", encoding="utf-8") as out:
        for r in conn.execute(
                "SELECT c.*, e.sha256, e.doc_kind FROM electricity_manual_corrections c JOIN electricity_bill_extractions e ON e.id = c.extraction_id "
                "WHERE c.regression_status = 'APPROVED' ORDER BY c.id"):
            out.write(json.dumps({"sha256": r["sha256"], "discom": r["discom_code"], "layout": r["layout_signature"], "field": r["field_name"],
                                  "extracted": r["original_value"], "verified": r["corrected_value"], "unit": r["corrected_unit"],
                                  "page": r["page"], "bbox": json.loads(r["bbox_json"] or "null"),
                                  "context": json.loads(r["context_json"] or "[]")}, ensure_ascii=False) + "\n")
            n += 1
    return n
