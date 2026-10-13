"""audit.py — append-only audit trail of important actions (corrections, approvals, settings, uploads, logins, scans)."""

import json

from . import storage


def log(conn, username, action, entity_type=None, entity_id=None, details=None, role=None, ip=None) -> None:
    conn.execute(
        "INSERT INTO electricity_audit_logs (ts, username, role, action, entity_type, entity_id, details_json, ip_address) VALUES (?,?,?,?,?,?,?,?)",
        (storage.now_iso(), username, role, action, entity_type, None if entity_id is None else str(entity_id),
         json.dumps(details or {}, default=str, ensure_ascii=False), ip))


def record_error(conn, stage, message, ref_type=None, ref_id=None, detail=None) -> int:
    return conn.execute(
        "INSERT INTO electricity_processing_errors (stage, ref_type, ref_id, message, detail, created_at) VALUES (?,?,?,?,?,?)",
        (stage, ref_type, ref_id, str(message)[:500], (str(detail)[:4000] if detail else None), storage.now_iso())).lastrowid
