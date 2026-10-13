"""Download the newest bill of every distinct supplier as a development/regression corpus (read-only Oracle)."""
import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from eb_app import config, discovery
from eb_app.oracle_source import OracleSource

conn = sqlite3.connect(config.DB_PATH); conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT a.attached_document_id, p.supplier_name, p.po_created,
           ROW_NUMBER() OVER (PARTITION BY p.supplier_name ORDER BY p.po_created DESC) AS rn
      FROM electricity_attachments a JOIN electricity_pos p USING (po_header_id)
     WHERE p.eligible = 1 AND a.fetch_status = 'PENDING' AND a.media_id IS NOT NULL
       AND (a.content_type LIKE 'image/%' OR a.content_type = 'application/pdf')
       AND p.po_created >= date('now', '-200 day')""").fetchall()
ids = [r["attached_document_id"] for r in rows if r["rn"] <= 2]     # 2 newest per supplier
print("selected", len(ids), "attachments from", len({r['supplier_name'] for r in rows}), "suppliers")
src = OracleSource()
try:
    print(discovery.download_pending(src, limit=len(ids), attachment_ids=ids))
finally:
    src.close()
