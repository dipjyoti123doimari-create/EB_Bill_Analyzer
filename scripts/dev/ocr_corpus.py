"""Dev helper: OCR every scanned page of the corpus into the on-disk cache (2 worker processes)."""
import multiprocessing as mp
import pathlib
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def work(args):
    name, rel = args
    from eb_app.reader import loader
    t = time.time()
    d = loader.load_document(ROOT / "storage" / rel, run_ocr=True)
    return name, round(time.time() - t, 1), sum(1 for p in d.pages if p.method == "ocr"), d.warnings[:1]


if __name__ == "__main__":
    c = sqlite3.connect(ROOT / "data" / "eb.db")
    rows = c.execute("select a.file_name, a.local_path from electricity_attachments a where a.fetch_status='DOWNLOADED'").fetchall()
    t0 = time.time()
    done = 0
    with mp.Pool(2) as pool:
        for name, secs, npages, warn in pool.imap_unordered(work, rows):
            done += 1
            if npages:
                print(f"[{done}/{len(rows)}] {name[:40]:<40} ocr pages={npages} {secs}s {warn}", flush=True)
    print(f"finished in {time.time() - t0:.0f}s", flush=True)
