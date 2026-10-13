"""Evaluate the reader against the hand-verified gold set. Prints field-level correctness, review rate and accuracy by confidence band.
usage: evaluate_gold.py [--verbose]"""
import collections, json, pathlib, sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from eb_app.reader.loader import load_document
from eb_app.reader.pipeline import analyze_document

verbose = "--verbose" in sys.argv
GOLD_FILE = next((a for a in sys.argv[1:] if a.endswith(".json")), "bills.json")
gold = json.loads((ROOT / "tests" / "gold" / GOLD_FILE).read_text(encoding="utf-8"))["bills"]
c = sqlite3.connect(ROOT / "data" / "eb.db")

KEYMAP = {"pf": ("pf_billed", "pf"), "consumer_number": ("consumer_number",), "billing_period": ("billing_period",),
          "excess_demand_penalty": ("charge_excess_demand_penalty",)}
TOL = {"total_payable": 1.0}


def got(res, field):
    if field == "basis":
        b = res["billing_basis"]
        return (b["basis"] if b["basis"] != "Unknown" else None), None
    names = KEYMAP.get(field, (field,))
    for n in names:
        f = res["fields"].get(n)
        if f:
            return f["value"], f
    return None, None


def same(field, want, have):
    if isinstance(want, list) and field == "consumer_number":
        return str(have) in [str(w) for w in want]
    if isinstance(want, list):
        return list(have) == want
    if isinstance(want, (int, float)):
        return have is not None and abs(float(have) - want) <= max(TOL.get(field, 0.0), abs(want) * 2e-4 + 0.005)
    return str(have) == str(want)


review_rate = collections.Counter(); tally = collections.Counter(); per_field = collections.defaultdict(collections.Counter); bands = collections.defaultdict(lambda: [0, 0]); rows = []
for g in gold:
    p = c.execute("select local_path from electricity_attachments where attached_document_id=?", (g["id"],)).fetchone()
    if not p:
        print("missing attachment", g["id"]); continue
    res = analyze_document(load_document(ROOT / "storage" / p[0]))
    for field, want in g["expect"].items():
        have, rec = got(res, field)
        conf = rec["confidence"] if rec else None
        ver = rec["verification"] if rec else None
        if want is None:
            outcome = "OK_ABSENT" if have is None or (rec and rec.get("status") == "CALCULATED") or (rec and ver == "NEEDS_REVIEW") else "FALSE_POSITIVE"
        elif have is None:
            outcome = "MISSING"
        else:
            outcome = "CORRECT" if same(field, want, have) else "WRONG"
        tally[outcome] += 1; per_field[field][outcome] += 1
        if outcome == "CORRECT" and rec: review_rate[ver] += 1
        if rec and want is not None and have is not None:
            b = "ge0.95" if conf >= 0.95 else "0.85-0.95" if conf >= 0.85 else "0.70-0.85" if conf >= 0.70 else "lt0.70"
            bands[b][0] += 1; bands[b][1] += outcome == "CORRECT"
        if outcome in ("WRONG", "MISSING", "FALSE_POSITIVE") or verbose:
            rows.append(f"  {outcome:<14} {g['id']} {g['supplier'][:18]:<18} {field:<16} want={want!s:<28} got={str(have)[:28]!s:<28} conf={conf if conf is None else round(conf, 2)} {ver or ''}")
print("\n".join(rows))
n = sum(tally.values())
print(f"\nfields checked: {n} | " + ", ".join(f"{k} {v}" for k, v in tally.most_common()))
print("\nby field:")
for f, t in sorted(per_field.items()):
    print(f"  {f:<16} " + ", ".join(f"{k} {v}" for k, v in t.most_common()))
print("\naccuracy by confidence band (found values only):")
for b in ("ge0.95", "0.85-0.95", "0.70-0.85", "lt0.70"):
    t, ok = bands[b]
    print(f"  {b:<10} n={t:<3} correct={ok:<3} " + (f"{100 * ok / t:.0f}%" if t else "-"))
print(f"\ncorrect values: {review_rate['AUTO']} released automatically, {review_rate['NEEDS_REVIEW']} sent to review (over-flagging cost)")
wrong_auto = sum(1 for r in rows if r.strip().startswith("WRONG") and "AUTO" in r)
print(f"\nWRONG values released as AUTO (no human review): {wrong_auto}")
