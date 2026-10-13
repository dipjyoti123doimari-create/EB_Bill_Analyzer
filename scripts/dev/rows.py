"""Dev helper: print a bill page as y-clustered rows of cells ('|' separated) to study layouts."""
import pathlib, sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))
from eb_app.reader import loader

fam, pageno = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1
maxrows = int(sys.argv[3]) if len(sys.argv) > 3 else 90
filt = len(sys.argv) > 4 and sys.argv[4] == "filter"
import re
KEY = re.compile(r"(p\.?\s?f|power\s*factor|kvah|kwh|kva|md|demand|contract|sanction|billed|units|consum|bill\s*(no|date|period|amount)|due|consumer|meter|total|payable|penal|incentive|rebate|energy|fixed|arrear|tariff|reading|multipl|mf|charges)", re.I)
c = sqlite3.connect("data/eb.db"); c.row_factory = sqlite3.Row
rows = c.execute("select a.attached_document_id id, a.file_name, a.local_path, p.supplier_name, p.plant_code from electricity_attachments a join electricity_pos p using(po_header_id) where a.fetch_status='DOWNLOADED' and a.content_type='application/pdf' and (upper(p.supplier_name) like ? or a.attached_document_id = ?) order by a.att_created desc", (f"%{fam.upper()}%", fam if fam.isdigit() else -1)).fetchall()
for r in rows:
    d = loader.load_document(pathlib.Path("storage") / r["local_path"], run_ocr=True)
    if not d.pages or d.pages[0].method == "none":
        continue
    pg = d.pages[min(pageno, len(d.pages)) - 1]
    print(f"##### {r['supplier_name'][:46]} | {r['plant_code']} | {r['file_name']} | id={r['id']} | page {pg.number}/{len(d.pages)} {pg.method} {pg.width:.0f}x{pg.height:.0f}")
    cells = sorted(pg.cells, key=lambda c: (c.yc, c.x0))
    rows_, cur, cy = [], [], None
    for cell in cells:
        if cy is not None and abs(cell.yc - cy) > 0.55 * cell.h:
            rows_.append(cur); cur = []
        cur.append(cell); cy = cell.yc if not cur[:-1] else cy
    if cur:
        rows_.append(cur)
    prev = False
    shown = 0
    for i, row in enumerate(rows_):
        row.sort(key=lambda c: c.x0)
        hit = bool(KEY.search(" ".join(c.text for c in row)))
        if filt and not (hit or prev):
            prev = False; continue
        prev = hit
        shown += 1
        if shown > maxrows: break
        print(f"{i:>3} y={row[0].yc:>5.0f} " + " | ".join(f"{c.text[:34]}@{c.x0:.0f}" for c in row))
    break
