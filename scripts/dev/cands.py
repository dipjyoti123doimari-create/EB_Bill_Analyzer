"""Dev helper: show every candidate the extractor found for chosen fields of one bill.
usage: cands.py <attachment_id> field1,field2 [maxrows]"""
import pathlib, sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from eb_app.reader.extractor import Extractor
from eb_app.reader.loader import load_document
from eb_app.reader.pipeline import _dedupe

att_id, fields = int(sys.argv[1]), sys.argv[2].split(",")
c = sqlite3.connect(ROOT / "data" / "eb.db")
path = c.execute("select local_path from electricity_attachments where attached_document_id=?", (att_id,)).fetchone()[0]
doc = load_document(ROOT / "storage" / path)
ex = Extractor(doc).run()
for f in fields:
    print(f"--- {f}")
    cands = ex.cands.get(f) if f in ex.cands else {"total_payable": ex.total_payable, "total_bill_amount": ex.total_bill}.get(f, [])
    for cd in _dedupe(cands)[:int(sys.argv[3]) if len(sys.argv) > 3 else 8]:
        print(f"  {cd.score:5.2f} {str(cd.value)[:22]:<22} {cd.relation:<13} p{cd.page} unit={cd.unit} label='{cd.label[:44]}' raw='{cd.raw[:24]}' {cd.note[:50]} {cd.extra.get('also', '')}")
