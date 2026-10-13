"""Dev helper: re-read one attachment with the current reader and print the named fields.   usage: field.py ATT_ID [field ...]"""
import pathlib, sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from eb_app.reader.loader import load_document
from eb_app.reader.pipeline import analyze_document

c = sqlite3.connect(ROOT / "data" / "eb.db")
p = c.execute("select local_path from electricity_attachments where attached_document_id = ?", (int(sys.argv[1]),)).fetchone()[0]
res = analyze_document(load_document(ROOT / "storage" / p))
names = sys.argv[2:] or list(res["fields"])
for n in names:
    f = res["fields"].get(n)
    print(f"{n:34} " + (f"{f['value']!s:28} {f.get('status')} conf={f.get('confidence')} {f.get('verification')} | {f.get('evidence', '')[:90]}" if f else "-"))
print("basis:", res["billing_basis"]["basis"], "| flags:", res["flags"][:2])
