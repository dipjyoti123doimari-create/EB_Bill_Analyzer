"""Dev helper: for the bills that carry (or may carry) a PF penalty / excess-demand penalty, print the printed lines that mention it, as the reader
sees them (label | numbers), next to what was extracted — so a person can verify the actual amounts against the bill.
usage: penalty_lines.py [--zero N]     (N bills with a printed zero penalty are added as controls)"""
import pathlib, re, sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from eb_app.reader.loader import load_document

zero_n = int(sys.argv[sys.argv.index("--zero") + 1]) if "--zero" in sys.argv else 6
c = sqlite3.connect(ROOT / "data" / "eb.db")
c.row_factory = sqlite3.Row
ids = [r["attachment_id"] for r in c.execute(
    "SELECT attachment_id FROM electricity_bills WHERE source = 'ORACLE_EBS' AND (pf_penalty_status IN ('FOUND','UNREADABLE') OR excess_demand_status IN ('FOUND','UNREADABLE')) ORDER BY id")]
for col in ("pf_penalty_status", "excess_demand_status"):
    ids += [r["attachment_id"] for r in c.execute(f"SELECT attachment_id FROM electricity_bills WHERE source = 'ORACLE_EBS' AND {col} = 'ZERO' ORDER BY id LIMIT ?", (zero_n,))]
ids = list(dict.fromkeys(ids))
KEY = re.compile(r"p\.?\s?f|power\s*factor|penal|surcharge|excess|incentive|disincentive|rebate|overdrawal|exceed|rkvah|low\s*pf", re.I)
for aid in ids:
    b = c.execute("SELECT b.*, a.local_path FROM electricity_bills b JOIN electricity_attachments a ON a.attached_document_id = b.attachment_id WHERE b.attachment_id = ?", (aid,)).fetchone()
    doc = load_document(ROOT / "storage" / b["local_path"], run_ocr=True)
    print(f"\n##### att {aid} | {b['plant_code']} {b['discom_code']} {b['billing_month']} | extracted: pf_penalty={b['pf_penalty']} ({b['pf_penalty_status']}) "
          f"excess={b['excess_demand_penalty']} ({b['excess_demand_status']}) payable={b['total_payable']}")
    for pg in doc.pages:
        cells = sorted(pg.cells, key=lambda x: (x.yc, x.x0))
        rows, cur, cy = [], [], None
        for cell in cells:
            if cy is not None and abs(cell.yc - cy) > 0.55 * cell.h:
                rows.append(cur); cur = []
            cur.append(cell); cy = cell.yc if not cur[:-1] else cy
        if cur:
            rows.append(cur)
        for row in rows:
            row.sort(key=lambda x: x.x0)
            txt = " | ".join(x.text[:42] for x in row)
            if KEY.search(txt):
                print(f"   p{pg.number}: {txt[:230]}")
