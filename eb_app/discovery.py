"""
discovery.py
============
Finds electricity POs and their attachments in Oracle EBS and mirrors the
metadata into the application database.

Modes
-----
full         every electricity PO and every attachment (backfill / daily reconcile)
incremental  PO list is always refreshed (cheap); attachments are re-read only if
             created/updated since (last successful scan - INCREMENTAL_OVERLAP_HOURS).
             An old PO that receives a new attachment months later is therefore found.

Attachment bytes are fetched separately by download_pending() so a slow or failing
download never blocks discovery.
"""

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import audit, config, settings, storage
from .identification import RuleSet
from .oracle_source import OracleError, OracleSource

log = logging.getLogger(__name__)

_SAFE_EXT = re.compile(r"^\.[a-z0-9]{1,5}$")
_STATE_LAST_SUCCESS = "last_success_started_at"


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat(sep=" ")
    return str(value)


def _parse_ebs_date(text) -> str | None:
    """Header attributes hold dates as text like '2026/09/01 00:00:00'."""
    if not text:
        return None
    m = re.match(r"^\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})", str(text))
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date().isoformat()
    except ValueError:
        return None


def _clean_hint(text) -> str | None:
    """Consumer number on the PO header is a free-text hint; junk values are dropped."""
    t = (text or "").strip()
    if len(t) < 3 or re.fullmatch(r"[-_.\s]+", t):
        return None
    return t


class DiscoveryError(RuntimeError):
    pass


def run_scan(source: OracleSource, mode: str = "incremental", trigger: str = "manual",
             db_path=None, now: datetime | None = None) -> dict:
    """Run one discovery scan. Returns a summary dict; raises nothing for Oracle failures
    (the run is recorded as FAILED so the scheduler can back off and alert)."""
    if mode not in ("full", "incremental"):
        raise ValueError("mode must be 'full' or 'incremental'")
    now = now or datetime.now()
    with storage.session(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO electricity_scan_runs (mode, trigger, started_at) VALUES (?,?,?)",
            (mode, trigger, storage.now_iso()))
        run_id = cur.lastrowid
        conn.commit()
        rules = RuleSet.load(conn)
        last_success = storage.get_state(conn, _STATE_LAST_SUCCESS)

    stats = {"run_id": run_id, "mode": mode, "pos_inspected": 0, "new_pos": 0, "attachments_seen": 0,
             "new_attachments": 0, "changed_attachments": 0, "status": "SUCCESS", "message": ""}
    try:
        if not rules.item_codes:
            raise DiscoveryError("No active ITEM_CODE identification rule is configured.")

        since = None
        if mode == "incremental" and last_success:
            since = datetime.fromisoformat(last_success) - timedelta(hours=config.INCREMENTAL_OVERLAP_HOURS)

        # 1. Which inventory items are electricity?
        by_code = source.resolve_item_ids(rules.item_codes)
        missing = [c for c in rules.item_codes if c not in by_code]
        item_ids = sorted({i for ids in by_code.values() for i in ids})
        if not item_ids:
            raise DiscoveryError(f"Item code(s) not found in Oracle: {', '.join(rules.item_codes)}")
        if missing:
            stats["message"] = f"Item code(s) not found in Oracle: {', '.join(missing)}"

        # 2. All PO lines for those items -> header ids / line ids
        lines = source.po_lines_for_items(item_ids)
        header_ids = sorted({int(r["po_header_id"]) for r in lines})
        line_to_header = {int(r["po_line_id"]): int(r["po_header_id"]) for r in lines}
        stats["pos_inspected"] = len(header_ids)

        # 3. PO details (supplier, plant, billing hints) and rule evaluation
        headers = source.po_headers(header_ids, item_ids)
        with storage.session(db_path) as conn:
            new_ids = _upsert_pos(conn, headers, rules)
        stats["new_pos"] = len(new_ids)

        # 4. Attachments (headers and lines). A PO we have never seen gets its complete
        #    attachment history even in incremental mode.
        atts = source.attachments(header_ids, list(line_to_header), since=since)
        if since is not None and new_ids:
            new_lines = [l for l, h in line_to_header.items() if h in new_ids]
            seen_ids = {a["attached_document_id"] for a in atts}
            atts += [a for a in source.attachments(sorted(new_ids), new_lines) if a["attached_document_id"] not in seen_ids]
        stats["attachments_seen"] = len(atts)
        with storage.session(db_path) as conn:
            new, changed = _upsert_attachments(conn, atts, line_to_header)
            stats["new_attachments"], stats["changed_attachments"] = new, changed
            storage.set_state(conn, _STATE_LAST_SUCCESS, storage.now_iso())
    except (OracleError, DiscoveryError) as exc:
        stats["status"] = "FAILED"
        stats["message"] = str(exc)
        log.error("Discovery scan %s failed: %s", run_id, exc)
    except Exception as exc:                                    # keep the scheduler alive, record the cause
        stats["status"] = "FAILED"
        stats["message"] = f"Unexpected error: {type(exc).__name__}: {exc}"
        log.exception("Discovery scan %s crashed", run_id)

    with storage.session(db_path) as conn:
        conn.execute(
            "UPDATE electricity_scan_runs SET finished_at=?, status=?, pos_inspected=?, new_pos=?, "
            "attachments_seen=?, new_attachments=?, changed_attachments=?, message=? WHERE id=?",
            (storage.now_iso(), stats["status"], stats["pos_inspected"], stats["new_pos"],
             stats["attachments_seen"], stats["new_attachments"], stats["changed_attachments"],
             stats["message"], run_id))
    return stats


def _upsert_pos(conn, headers: list[dict], rules: RuleSet) -> set[int]:
    """Insert/update PO rows; return the set of PO header ids seen for the first time."""
    now = storage.now_iso()
    new_ids: set[int] = set()
    for h in headers:
        po = {
            "po_header_id": int(h["po_header_id"]),
            "po_number": str(h["po_number"]),
            "org_id": h.get("org_id"),
            "supplier_id": h.get("vendor_id"),
            "supplier_name": h.get("supplier_name"),
            "supplier_code": h.get("supplier_code"),
            "vendor_site_id": h.get("vendor_site_id"),
            "plant_org_id": h.get("plant_org_id"),
            "plant_code": h.get("plant_code"),
            "plant_name": h.get("plant_name"),
            "plant_count": h.get("plant_count"),
            "po_created": _iso(h.get("creation_date")),
            "po_updated": _iso(h.get("last_update_date")),
            "auth_status": h.get("authorization_status"),
            "cancel_flag": h.get("cancel_flag"),
            "closed_code": h.get("closed_code"),
            "po_type": h.get("type_lookup_code"),
            "comments": h.get("comments"),
            "line_description": h.get("line_description"),
            "billing_start": _parse_ebs_date(h.get("attribute1")),
            "billing_end": _parse_ebs_date(h.get("attribute2")),
            "consumer_hint": _clean_hint(h.get("attribute3")),
        }
        eligible, reason = rules.evaluate(po)
        if eligible and (po["cancel_flag"] or "").upper() == "Y":
            eligible, reason = False, "PO is cancelled"
        po["eligible"], po["exclusion_reason"] = int(eligible), reason

        exists = conn.execute("SELECT 1 FROM electricity_pos WHERE po_header_id = ?", (po["po_header_id"],)).fetchone()
        cols = list(po)
        if exists:
            sets = ", ".join(f"{c} = :{c}" for c in cols if c != "po_header_id")
            conn.execute(f"UPDATE electricity_pos SET {sets}, last_seen = :last_seen WHERE po_header_id = :po_header_id",
                         {**po, "last_seen": now})
        else:
            conn.execute(
                f"INSERT INTO electricity_pos ({', '.join(cols)}, first_seen, last_seen) "
                f"VALUES ({', '.join(':' + c for c in cols)}, :now, :now)", {**po, "now": now})
            new_ids.add(po["po_header_id"])
    return new_ids


def _upsert_attachments(conn, atts: list[dict], line_to_header: dict[int, int]) -> tuple[int, int]:
    now = storage.now_iso()
    new_count = changed = 0
    for a in atts:
        pk1 = str(a["pk1_value"])
        try:
            header_id = int(pk1) if a["entity_name"] == "PO_HEADERS" else line_to_header[int(pk1)]
        except (ValueError, KeyError):
            continue
        datatype = a.get("datatype_id")
        file_name = a.get("lob_file_name") or a.get("doc_file_name")
        is_web = datatype == config.DATATYPE_WEB_PAGE or (a.get("url") and not a.get("media_id"))
        row = {
            "attached_document_id": int(a["attached_document_id"]), "po_header_id": header_id,
            "entity_name": a["entity_name"], "pk1_value": pk1, "document_id": int(a["document_id"]),
            "seq_num": a.get("seq_num"), "datatype_id": datatype, "media_id": a.get("media_id"),
            "file_name": file_name, "url": a.get("url"), "doc_description": a.get("doc_description"),
            "content_type": a.get("content_type"), "size_bytes": a.get("size_bytes"),
            "att_created": _iso(a.get("att_created")), "att_updated": _iso(a.get("att_updated")),
        }
        existing = conn.execute(
            "SELECT att_updated, media_id, fetch_status FROM electricity_attachments WHERE attached_document_id = ?",
            (row["attached_document_id"],)).fetchone()
        if existing is None:
            status = "WEB_LINK" if is_web else "PENDING"
            conn.execute(
                f"INSERT INTO electricity_attachments ({', '.join(row)}, fetch_status, first_seen, last_seen) "
                f"VALUES ({', '.join(':' + c for c in row)}, :status, :now, :now)",
                {**row, "status": status, "now": now})
            new_count += 1
        else:
            file_replaced = (existing["media_id"] != row["media_id"]) and row["media_id"] is not None
            if file_replaced:
                changed += 1
            sets = ", ".join(f"{c} = :{c}" for c in row if c != "attached_document_id")
            extra = ""
            if file_replaced:   # revised attachment: re-download, keep the old copy on disk
                extra = ", fetch_status = 'PENDING', fetch_error = NULL, revision = revision + 1, retry_count = 0, next_retry_at = NULL"
            conn.execute(f"UPDATE electricity_attachments SET {sets}, last_seen = :now{extra} "
                         f"WHERE attached_document_id = :attached_document_id", {**row, "now": now})
    return new_count, changed


# --------------------------------------------------------------------------- downloads
def _safe_extension(file_name: str | None, content_type: str | None) -> str:
    ext = os.path.splitext(file_name or "")[1].lower()
    if _SAFE_EXT.match(ext):
        return ext
    return {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/pjpeg": ".jpg", "image/png": ".png",
            "image/x-png": ".png"}.get((content_type or "").lower(), ".bin")


MAX_DOWNLOAD_RETRIES = 5


def download_pending(source: OracleSource, limit: int = 25, db_path=None, only_eligible: bool = True,
                     attachment_ids=None, now: datetime | None = None) -> dict:
    """Download up to `limit` pending attachments into the original-document store.

    Files are stored content-addressed (sha256) and never overwritten or modified.
    attachment_ids: restrict the download to these attachments (e.g. a sample corpus).
    """
    config.ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)
    done = failed = 0
    only_ids = ""
    params: list = []
    if attachment_ids is not None:
        attachment_ids = [int(i) for i in attachment_ids]
        only_ids = f"AND a.attached_document_id IN ({','.join('?' * len(attachment_ids))}) "
        params += attachment_ids
    now_txt = (now or datetime.now()).replace(microsecond=0).isoformat(sep=" ")
    with storage.session(db_path) as conn:
        backfill_from = settings.get(conn, "backfill_from")
        since_sql, since_params = ("AND p.po_created >= ? ", [backfill_from]) if backfill_from else ("", [])
        # a failed download is retried with exponential back-off (10 min, 20, 40, ... up to MAX_DOWNLOAD_RETRIES attempts)
        rows = conn.execute(
            "SELECT a.attached_document_id, a.media_id, a.file_name, a.content_type, a.retry_count FROM electricity_attachments a "
            "JOIN electricity_pos p ON p.po_header_id = a.po_header_id "
            "WHERE a.media_id IS NOT NULL AND (a.fetch_status = 'PENDING' OR (a.fetch_status = 'FAILED' AND a.retry_count < ? "
            "AND (a.next_retry_at IS NULL OR a.next_retry_at <= ?))) "
            + ("AND p.eligible = 1 " if only_eligible else "") + since_sql + only_ids +
            "ORDER BY a.att_created DESC LIMIT ?", (MAX_DOWNLOAD_RETRIES, now_txt, *since_params, *params, limit)).fetchall()
    for r in rows:
        try:
            data, _, ctype = source.fetch_attachment_bytes(r["media_id"])
            digest = hashlib.sha256(data).hexdigest()
            ext = _safe_extension(r["file_name"], ctype or r["content_type"])
            target = config.ORIGINALS_DIR / digest[:2] / f"{digest}{ext}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                tmp = target.with_suffix(target.suffix + ".part")
                tmp.write_bytes(data)
                os.replace(tmp, target)
            with storage.session(db_path) as conn:
                conn.execute(
                    "UPDATE electricity_attachments SET fetch_status='DOWNLOADED', sha256=?, local_path=?, "
                    "size_bytes=?, downloaded_at=?, fetch_error=NULL WHERE attached_document_id=?",
                    (digest, str(target.relative_to(config.STORAGE_DIR)), len(data), storage.now_iso(),
                     r["attached_document_id"]))
            done += 1
        except (OracleError, OSError) as exc:                  # Oracle failure, or the disk refused the file: record it, retry later, go on
            status = "TOO_LARGE" if "above the" in str(exc) else "FAILED"
            retries = (r["retry_count"] or 0) + 1
            delay = timedelta(minutes=10 * 2 ** (retries - 1))
            with storage.session(db_path) as conn:
                conn.execute("UPDATE electricity_attachments SET fetch_status=?, fetch_error=?, retry_count=?, next_retry_at=? WHERE attached_document_id=?",
                             (status, str(exc)[:500], retries, (( now or datetime.now()) + delay).replace(microsecond=0).isoformat(sep=" "),
                              r["attached_document_id"]))
                audit.record_error(conn, "DOWNLOAD", "attachment", r["attached_document_id"], str(exc)[:300])
            failed += 1
    return {"downloaded": done, "failed": failed, "attempted": len(rows)}
