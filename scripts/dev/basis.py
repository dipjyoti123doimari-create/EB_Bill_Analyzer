"""Dev helper: show billing-basis evidence + charge lines of one bill. usage: basis.py <attachment_id>"""
import json, pathlib, sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from eb_app.reader.pipeline import analyze_file
c = sqlite3.connect(ROOT / "data" / "eb.db")
path = c.execute("select local_path from electricity_attachments where attached_document_id=?", (int(sys.argv[1]),)).fetchone()[0]
r = analyze_file(ROOT / "storage" / path)
print(json.dumps(r["billing_basis"], indent=1, default=str)[:1500])
print("kwh", r["fields"].get("kwh", {}).get("value"), "kvah", r["fields"].get("kvah", {}).get("value"))
for ln in r["charge_lines"][:int(sys.argv[2]) if len(sys.argv) > 2 else 25]:
    print(f"  p{ln['page']} {str(ln['category']):<22} {ln['label'][:38]:<38} {ln['numbers']}")
