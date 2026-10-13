"""Dev helper: for given attachment ids print the extracted summary and the key printed rows to verify by eye.
usage: verify.py id1,id2,... [rows_per_bill]"""
import pathlib, re, sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from eb_app.reader.layout import build_rows
from eb_app.reader.loader import load_document
from eb_app.reader.pipeline import analyze_document

ids = [int(x) for x in sys.argv[1].split(",")]
maxrows = int(sys.argv[2]) if len(sys.argv) > 2 else 30
KEY = re.compile(r"(p\.?\s?f|power\s*factor|kvah|kwh|kva|\bmd\b|mdi|demand|contract|sanction|billed|units|consum|bill\s*(no|date|period|amount)|due|payable|net|total|period|consumer|reading date)", re.I)
c = sqlite3.connect(ROOT / "data" / "eb.db"); c.row_factory = sqlite3.Row
for att in ids:
    r = c.execute("select a.file_name, a.local_path, p.supplier_name, p.plant_code, p.billing_start, p.billing_end, p.consumer_hint from electricity_attachments a join electricity_pos p using(po_header_id) where a.attached_document_id=?", (att,)).fetchone()
    doc = load_document(ROOT / "storage" / r["local_path"])
    res = analyze_document(doc)
    print(f"\n######## {att} | {r['supplier_name'][:44]} | {r['plant_code']} | {r['file_name'][:40]} | PO period {r['billing_start']}..{r['billing_end']} hint {r['consumer_hint']}")
    for k in ("consumer_number", "bill_date", "billing_period", "kwh", "kvah", "pf_billed", "pf", "pf_calculated", "contract_demand", "sanctioned_load",
              "max_demand", "billing_demand", "total_payable", "total_bill_amount"):
        f = res["fields"].get(k)
        if f:
            print(f"  {k:<16} {str(f['value']):<26} {str(f.get('unit') or ''):<5} p{f.get('page', '-')} {f.get('relation', f.get('method', '')):<12} conf={f['confidence']:.2f} {f['verification']:<12} <- '{str(f.get('label', ''))[:34]}' '{str(f.get('raw', ''))[:18]}'")
        else:
            print(f"  {k:<16} -")
    print(f"  basis={res['billing_basis']['basis']} ({res['billing_basis']['confidence']}) flags={res['flags']}")
    n = 0
    for pg in doc.pages[:3]:
        for row in build_rows(pg.cells):
            t = " | ".join(x.text.strip()[:26] for x in row)
            if KEY.search(t) and not re.search(r"^[\W\d]*$", t):
                print(f"     p{pg.number}: {t[:165]}")
                n += 1
                if n >= maxrows:
                    break
        if n >= maxrows:
            break
