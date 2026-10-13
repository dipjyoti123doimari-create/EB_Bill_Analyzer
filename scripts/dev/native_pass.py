"""Dev helper: native-only pass over the corpus to check loader robustness and speed."""
import collections, pathlib, sqlite3, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))
from eb_app.reader import loader

c = sqlite3.connect("data/eb.db"); c.row_factory = sqlite3.Row
rows = c.execute("select a.file_name, a.local_path from electricity_attachments a where a.fetch_status='DOWNLOADED'").fetchall()
t = time.time(); stat = collections.Counter(); cells = 0; slow = []; warns = collections.Counter()
for r in rows:
    t0 = time.time()
    d = loader.load_document(pathlib.Path("storage") / r["local_path"], run_ocr=False)
    stat[(d.kind, tuple(sorted({p.method for p in d.pages})))] += 1
    cells += sum(len(p.cells) for p in d.pages)
    for w in d.warnings:
        warns[w[:60]] += 1
    if time.time() - t0 > 1.0:
        slow.append((r["file_name"], round(time.time() - t0, 1)))
print(f"native-only pass over {len(rows)} files in {time.time() - t:.1f}s, cells={cells}")
for k, v in stat.items():
    print("  ", k, v)
print("warnings:", dict(warns))
print("slow:", slow[:5])
