"""Dev helper: run the reader over the corpus and print one compact line per bill.
usage: run_corpus.py [native|ocr|all] [substring-of-supplier]  -> also writes data/corpus_results.json"""
import json, pathlib, sqlite3, sys, time, traceback
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from eb_app.reader.loader import load_document
from eb_app.reader.pipeline import analyze_document

mode = sys.argv[1] if len(sys.argv) > 1 else "native"
filt = sys.argv[2].upper() if len(sys.argv) > 2 else ""
c = sqlite3.connect(ROOT / "data" / "eb.db"); c.row_factory = sqlite3.Row
rows = c.execute("select a.attached_document_id id, a.file_name, a.local_path, p.supplier_name, p.plant_code, p.po_number from electricity_attachments a join electricity_pos p using(po_header_id) where a.fetch_status='DOWNLOADED' order by p.supplier_name, a.att_created desc").fetchall()


def v(res, f, nd=None):
    r = res["fields"].get(f)
    if not r:
        return "-"
    x = r["value"]
    if isinstance(x, list):
        return "/".join(s[5:] for s in x)
    if isinstance(x, float):
        x = round(x, 3)
        x = int(x) if x == int(x) and abs(x) > 50 else x
    mark = "" if r.get("verification") == "AUTO" else "?"
    return f"{x}{mark}"


out = {}
t0 = time.time()
print(f"{'id':>9} {'supplier':<22} {'plant':<4} {'kwh':>9} {'kvah':>9} {'pfb':>6} {'pf':>6} {'pfcalc':>6} {'CD':>7} {'MD':>8} {'basis':<6} {'payable':>10} {'consumer':<16} {'bdate':>8} {'period':>11}")
for r in rows:
    if filt and filt not in (r["supplier_name"] or "").upper():
        continue
    path = ROOT / "storage" / r["local_path"]
    doc = load_document(path, run_ocr=(mode != "native"))
    if doc.kind == "unsupported" or not doc.pages:
        continue
    if mode == "native" and any(p.method == "none" for p in doc.pages):
        continue
    if mode == "ocr" and not any(p.method == "ocr" for p in doc.pages):
        continue
    try:
        res = analyze_document(doc)
    except Exception:
        print(f"{r['id']:>9} {r['supplier_name'][:22]:<22} EXCEPTION"); traceback.print_exc(); continue
    out[r["id"]] = {"file": r["file_name"], "supplier": r["supplier_name"], "plant": r["plant_code"], "po": r["po_number"], "result": res}
    cl = res["classification"]["verdict"][:3]
    print(f"{r['id']:>9} {r['supplier_name'][:22]:<22} {r['plant_code'][:4]:<4} {v(res,'kwh'):>9} {v(res,'kvah'):>9} {v(res,'pf_billed'):>6} {v(res,'pf'):>6} {v(res,'pf_calculated'):>6} "
          f"{v(res,'contract_demand'):>7} {v(res,'max_demand'):>8} {res['billing_basis']['basis'][:6]:<6} {v(res,'total_payable'):>10} {v(res,'consumer_number')[:16]:<16} {v(res,'bill_date'):>8} {v(res,'billing_period'):>11} {cl}")
(ROOT / "data" / "corpus_results.json").write_text(json.dumps(out, default=str), encoding="utf-8")
print(f"{len(out)} bills in {time.time() - t0:.0f}s")

# ---- coverage summary -------------------------------------------------------------------------------
import collections
KEYF = ["kwh", "kvah", "pf_any", "contract_demand", "max_demand", "total_payable", "consumer_number", "bill_date", "billing_period"]
cov = collections.Counter(); ok_auto = collections.Counter(); n = 0
for rec in out.values():
    r = rec["result"]
    if r["classification"]["verdict"] == "NOT_ELECTRICITY_BILL":
        continue
    n += 1
    f = r["fields"]
    for k in KEYF:
        have = (("pf_billed" in f or "pf" in f or "pf_calculated" in f) if k == "pf_any" else k in f)
        cov[k] += have
        if have:
            ok_auto[k] += (f.get(k, f.get("pf_billed", f.get("pf", {}))).get("verification") == "AUTO") if k != "pf_any" else 0
print(f"coverage over {n} bills: " + ", ".join(f"{k} {cov[k]}" for k in KEYF))
print("basis: ", dict(collections.Counter(r["result"]["billing_basis"]["basis"] for r in out.values())))
