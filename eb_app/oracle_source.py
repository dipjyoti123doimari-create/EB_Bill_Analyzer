"""
oracle_source.py
================
The only module that talks to Oracle EBS R12. Everything here is READ-ONLY.

* Connection settings come from ops_settings (the existing RDC-OPS configuration).
* Connections come from a small pool; credentials never leave this process.
* Every statement passes through _assert_read_only() before it is executed.
* Object names were verified against the live data dictionary (Phase 1 discovery):
  PO_HEADERS_ALL / PO_LINES_ALL / PO_DISTRIBUTIONS_ALL / AP_SUPPLIERS /
  ORG_ORGANIZATION_DEFINITIONS / MTL_SYSTEM_ITEMS_B / FND_ATTACHED_DOCUMENTS /
  FND_DOCUMENTS / FND_DOCUMENTS_TL / FND_LOBS, all readable through the APPS synonyms.
"""

import logging
import re
import threading
from contextlib import contextmanager

import oracledb

from . import config
from .ops_settings import OracleSettings, load_oracle_settings

log = logging.getLogger(__name__)

_thick_lock = threading.Lock()
_thick_done = False

_READ_ONLY_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|execute|exec|begin|call|commit|rollback|lock)\b",
    re.IGNORECASE)


class OracleError(RuntimeError):
    """Raised for any Oracle problem; the message never contains credentials."""


def _assert_read_only(sql: str) -> None:
    if not _READ_ONLY_START.match(sql) or ";" in sql or _FORBIDDEN.search(sql):
        raise OracleError("Refusing to run a statement that is not a plain SELECT.")


def _chunks(seq, size):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _in_clause(prefix: str, values) -> tuple[str, dict]:
    """Build ':p0,:p1,...' plus its bind dict."""
    values = list(values)
    return ",".join(f":{prefix}{i}" for i in range(len(values))), {f"{prefix}{i}": v for i, v in enumerate(values)}


def _init_thick(lib_dir: str) -> None:
    """oracledb thick mode (Instant Client) can be initialised once per process."""
    global _thick_done
    with _thick_lock:
        if not _thick_done:
            if not lib_dir:
                raise OracleError("Oracle Instant Client folder is not configured.")
            oracledb.init_oracle_client(lib_dir=lib_dir)
            _thick_done = True


class OracleSource:
    def __init__(self, settings: OracleSettings | None = None):
        self.settings = settings or load_oracle_settings()
        self._pool = None
        self._pool_lock = threading.Lock()

    # ----- connection handling -------------------------------------------------
    def _get_pool(self):
        with self._pool_lock:
            if self._pool is None:
                s = self.settings
                if not s.is_complete():
                    raise OracleError("Oracle connection settings are incomplete.")
                try:
                    _init_thick(s.instantclient)
                    self._pool = oracledb.create_pool(
                        user=s.user, password=s.password, dsn=s.dsn,
                        min=1, max=config.ORACLE_POOL_MAX, increment=1)
                except oracledb.Error as exc:
                    raise OracleError(f"Could not open Oracle pool: {self._safe(exc)}") from None
            return self._pool

    def _safe(self, exc: Exception) -> str:
        text = str(exc)
        pw = self.settings.password
        return text.replace(pw, "***") if pw else text

    @contextmanager
    def connection(self):
        pool = self._get_pool()
        try:
            conn = pool.acquire()
        except oracledb.Error as exc:
            raise OracleError(f"Could not acquire Oracle connection: {self._safe(exc)}") from None
        conn.call_timeout = config.ORACLE_CALL_TIMEOUT_MS
        try:
            yield conn
        finally:
            pool.release(conn)

    def close(self):
        with self._pool_lock:
            if self._pool is not None:
                self._pool.close(force=True)
                self._pool = None

    # ----- generic read-only query --------------------------------------------
    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        _assert_read_only(sql)
        try:
            with self.connection() as conn:
                cur = conn.cursor()
                cur.arraysize = 500
                cur.execute(sql, params or {})
                cols = [d[0].lower() for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except oracledb.Error as exc:
            raise OracleError(f"Oracle query failed: {self._safe(exc)}") from None

    def ping(self) -> dict:
        row = self.query("SELECT user AS usr, sys_context('USERENV','DB_NAME') AS db FROM dual")[0]
        return {"user": row["usr"], "database": row["db"]}

    # ----- domain queries -------------------------------------------------------
    def resolve_item_ids(self, item_codes) -> dict[str, list[int]]:
        """Item code (MTL_SYSTEM_ITEMS_B.SEGMENT1) -> distinct INVENTORY_ITEM_IDs."""
        out: dict[str, list[int]] = {}
        codes = [c.strip() for c in item_codes if c and c.strip()]
        if not codes:
            return out
        binds, params = _in_clause("c", codes)
        rows = self.query(
            f"SELECT DISTINCT segment1 AS code, inventory_item_id AS item_id FROM apps.mtl_system_items_b "
            f"WHERE segment1 IN ({binds})", params)
        for r in rows:
            out.setdefault(r["code"], []).append(int(r["item_id"]))
        return out

    def po_lines_for_items(self, item_ids) -> list[dict]:
        """Every PO line that carries one of the given inventory items."""
        item_ids = [int(i) for i in item_ids]
        if not item_ids:
            return []
        binds, params = _in_clause("i", item_ids)
        return self.query(
            f"SELECT l.po_line_id, l.po_header_id, l.line_num, l.item_description, l.item_id "
            f"FROM apps.po_lines_all l WHERE l.item_id IN ({binds})", params)

    def po_headers(self, header_ids, item_ids) -> list[dict]:
        """PO header + supplier + plant (from the distribution of the electricity line)."""
        item_binds, item_params = _in_clause("i", [int(i) for i in item_ids])
        rows: list[dict] = []
        for chunk in _chunks(header_ids, config.ORACLE_IN_CHUNK - len(item_params)):
            h_binds, h_params = _in_clause("h", chunk)
            sql = f"""
                SELECT h.po_header_id, h.segment1 AS po_number, h.org_id, h.vendor_id,
                       s.vendor_name AS supplier_name, s.segment1 AS supplier_code, h.vendor_site_id,
                       h.creation_date, h.last_update_date, h.authorization_status, h.cancel_flag,
                       h.closed_code, h.type_lookup_code, h.comments,
                       h.attribute1, h.attribute2, h.attribute3,
                       MIN(d.destination_organization_id) AS plant_org_id,
                       COUNT(DISTINCT d.destination_organization_id) AS plant_count,
                       MIN(od.organization_code) AS plant_code,
                       MIN(od.organization_name) AS plant_name,
                       MIN(l.item_description) AS line_description
                  FROM apps.po_headers_all h
                  LEFT JOIN apps.ap_suppliers s ON s.vendor_id = h.vendor_id
                  LEFT JOIN apps.po_lines_all l ON l.po_header_id = h.po_header_id AND l.item_id IN ({item_binds})
                  LEFT JOIN apps.po_distributions_all d ON d.po_line_id = l.po_line_id
                  LEFT JOIN apps.org_organization_definitions od ON od.organization_id = d.destination_organization_id
                 WHERE h.po_header_id IN ({h_binds})
                 GROUP BY h.po_header_id, h.segment1, h.org_id, h.vendor_id, s.vendor_name, s.segment1,
                          h.vendor_site_id, h.creation_date, h.last_update_date, h.authorization_status,
                          h.cancel_flag, h.closed_code, h.type_lookup_code, h.comments,
                          h.attribute1, h.attribute2, h.attribute3"""
            rows += self.query(sql, {**item_params, **h_params})
        return rows

    def attachments(self, header_ids, line_ids, since=None) -> list[dict]:
        """Attachments of the given PO headers and PO lines (metadata + LOB size, no content).

        since: only attachments created/updated on or after this datetime (incremental scan).
        """
        out: list[dict] = []
        since_sql = " AND a.last_update_date >= :since" if since is not None else ""
        for entity, ids in (("PO_HEADERS", header_ids), ("PO_LINES", line_ids)):
            for chunk in _chunks([str(i) for i in ids], config.ORACLE_IN_CHUNK - 3):
                binds, params = _in_clause("p", chunk)
                params["entity"] = entity
                if since is not None:
                    params["since"] = since
                sql = f"""
                    SELECT a.attached_document_id, a.entity_name, a.pk1_value, a.document_id, a.seq_num,
                           a.creation_date AS att_created, a.last_update_date AS att_updated,
                           d.datatype_id, d.media_id, d.file_name AS doc_file_name, d.url, d.category_id,
                           tl.description AS doc_description, tl.title AS doc_title,
                           lb.file_name AS lob_file_name, lb.file_content_type AS content_type,
                           dbms_lob.getlength(lb.file_data) AS size_bytes
                      FROM apps.fnd_attached_documents a
                      JOIN apps.fnd_documents d ON d.document_id = a.document_id
                      LEFT JOIN apps.fnd_documents_tl tl ON tl.document_id = d.document_id AND tl.language = 'US'
                      LEFT JOIN apps.fnd_lobs lb ON lb.file_id = d.media_id AND d.datatype_id = {config.DATATYPE_FILE}
                     WHERE a.entity_name = :entity AND a.pk1_value IN ({binds}){since_sql}"""
                out += self.query(sql, params)
        return out

    def fetch_attachment_bytes(self, media_id: int) -> tuple[bytes, str | None, str | None]:
        """Read one FND_LOBS file. Returns (content, file_name, content_type)."""
        sql = "SELECT file_name, file_content_type, file_data FROM apps.fnd_lobs WHERE file_id = :f"
        _assert_read_only(sql)
        try:
            with self.connection() as conn:
                cur = conn.cursor()
                cur.execute(sql, {"f": int(media_id)})
                row = cur.fetchone()
                if row is None:
                    raise OracleError(f"No FND_LOBS row for file_id {media_id}.")
                name, ctype, lob = row
                size = lob.size() if lob is not None else 0
                if size > config.MAX_ATTACHMENT_BYTES:
                    raise OracleError(f"Attachment {media_id} is {size} bytes, above the {config.MAX_ATTACHMENT_BYTES} limit.")
                data = lob.read() if lob is not None else b""
                return data, name, ctype
        except oracledb.Error as exc:
            raise OracleError(f"Attachment download failed: {self._safe(exc)}") from None
