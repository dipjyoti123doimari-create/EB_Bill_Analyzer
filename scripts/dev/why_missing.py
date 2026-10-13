"""Dev helper: for bills (one per supplier) where a field was not extracted, show the text rows that mention the topic.
usage: why_missing.py <field> <regex> [max_suppliers] [rows_per_bill]"""
import json, pathlib, re, sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from eb_app.reader.loader import load_document
from eb_app.reader.layout import build_rows

field, rx = sys.argv[1], re.compile(sys.argv[2], re.I)
max_sup = int(sys.argv[3]) if len(sys.argv) > 3 else 10
per = int(sys.argv[4]) if len(sys.argv) > 4 else 7
d = json.load(open(ROOT / "data" / "corpus_results.json", encoding="utf-8"))
c = sqlite3.connect(ROOT / "data" / "eb.db")
seen = set(); shown = 0
for att_id, rec in d.items():
    f = rec["result"]["fields"]
    if rec["result"]["classification"]["verdict"] == "NOT_ELECTRICITY_BILL" or field in f or rec["supplier"] in seen:
        continue
    seen.add(rec["supplier"]); shown += 1
    if shown > max_sup:
        break
    path = c.execute("select local_path from electricity_attachments where attached_document_id=?", (int(att_id),)).fetchone()[0]
    doc = load_document(ROOT / "storage" / path, run_ocr=False)
    print(f"##### {rec['supplier'][:40]} | id={att_id} | {rec['file'][:34]}")
    n = 0
    for pg in doc.pages:
        for row in build_rows(pg.cells):
            t = " | ".join(x.text.strip()[:30] for x in row)
            if rx.search(t):
                print(f"   p{pg.number} {t[:170]}")
                n += 1
                if n >= per:
                    break
        if n >= per:
            break
