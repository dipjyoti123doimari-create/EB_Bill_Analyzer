"""Dev helper: print keyword-bearing native text lines of one bill per supplier family."""
import re, sqlite3, sys, pathlib, pymupdf
fam = sys.argv[1]; maxl = int(sys.argv[2]) if len(sys.argv) > 2 else 45
c = sqlite3.connect("data/eb.db"); c.row_factory = sqlite3.Row
rows = c.execute("select a.attached_document_id id, a.file_name, a.local_path, p.supplier_name, p.plant_code from electricity_attachments a join electricity_pos p using(po_header_id) where a.fetch_status='DOWNLOADED' and a.content_type='application/pdf' and upper(p.supplier_name) like ? order by a.att_created desc", (f"%{fam.upper()}%",)).fetchall()
KEY = re.compile(r"(p\.?\s?f\b|power\s*factor|kvah|kwh|kva\b|\bmd\b|demand|contract|sanction|load|units|consumption|bill\s*(no|date|period)|due|consumer|meter|total|payable|penal|incentive|rebate|surcharge|arrear|tariff|reading|multipl|mf\b)", re.I)
shown = 0
for r in rows:
    d = pymupdf.open(pathlib.Path("storage")/r["local_path"])
    txt = "\n".join(p.get_text() for p in d)
    if len(txt.strip()) < 100: continue
    print(f"##### {r['supplier_name'][:50]} | {r['plant_code']} | {r['file_name']} | pages={len(d)} id={r['id']}")
    n = 0
    for ln in txt.splitlines():
        ln = ln.strip()
        if ln and KEY.search(ln) and len(ln) < 160:
            print("  ", ln); n += 1
            if n >= maxl: break
    shown += 1
    if shown >= 1: break
