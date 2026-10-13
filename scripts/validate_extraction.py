"""Field-level extraction validation against the hand-verified bills in tests/gold (Phase 6.2).

For every gold bill the current reader is run and each verified field is classified:
  CORRECT          value equals the verified value
  WRONG            a value was produced but it differs
  MISSING          the bill prints the value, the reader produced none
  FALSE_POSITIVE   the bill prints no value (verified absent) but the reader produced one that was not flagged for review
Also reported per field: how many correct values were released automatically vs sent to review (review rate), and how many WRONG values
were released as AUTO (these are the dangerous ones: the target is 0).
Usage: .venv\\Scripts\\python scripts\\validate_extraction.py [--md docs/VALIDATION.md]
"""
import collections
import json
import pathlib
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from eb_app.reader.loader import load_document  # noqa: E402
from eb_app.reader.pipeline import analyze_document  # noqa: E402

SETS = [("development", "bills.json"), ("held-out 1", "heldout1.json"), ("held-out 2", "heldout2.json"), ("scans / photos", "scans.json"), ("penalties", "penalties.json")]
KEYMAP = {"pf": ("pf_billed", "pf"), "excess_demand_penalty": ("charge_excess_demand_penalty",)}
ORDER = ["consumer_number", "billing_period", "kwh", "kvah", "pf", "basis", "contract_demand", "max_demand", "pf_penalty", "excess_demand_penalty", "total_payable"]
LABEL = {"consumer_number": "Consumer number", "billing_period": "Billing period", "kwh": "kWh", "kvah": "kVAh", "pf": "PF", "basis": "Billing basis",
         "contract_demand": "Contract demand", "max_demand": "Maximum demand", "pf_penalty": "PF penalty (amount)", "excess_demand_penalty": "Demand penalty (amount)",
         "total_payable": "Total payable"}


def got(res, field):
    if field == "basis":
        b = res["billing_basis"]
        return (b["basis"] if b["basis"] != "Unknown" else None), None
    for n in KEYMAP.get(field, (field,)):
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
        return abs(float(have) - want) <= max(1.0 if field == "total_payable" else 0.0, abs(want) * 2e-4 + 0.005)
    return str(have) == str(want)


def main():
    db = sqlite3.connect(ROOT / "data" / "eb.db")
    tally = collections.defaultdict(collections.Counter)
    per_set = collections.defaultdict(collections.Counter)
    seen = set()
    wrong_auto = []
    for setname, fname in SETS:
        gold = json.loads((ROOT / "tests" / "gold" / fname).read_text(encoding="utf-8"))["bills"]
        for g in gold:
            row = db.execute("SELECT local_path FROM electricity_attachments WHERE attached_document_id = ?", (g["id"],)).fetchone()
            if not row:
                continue
            res = analyze_document(load_document(ROOT / "storage" / row[0]))
            for field, want in g["expect"].items():
                if (g["id"], field) in seen or field not in ORDER:
                    continue
                seen.add((g["id"], field))
                have, rec = got(res, field)
                ver = rec["verification"] if rec else None
                if want is None:
                    out = "OK_ABSENT" if have is None or (rec and (rec.get("status") == "CALCULATED" or ver == "NEEDS_REVIEW")) else "FALSE_POSITIVE"
                elif have is None:
                    out = "MISSING"
                else:
                    out = "CORRECT" if same(field, want, have) else "WRONG"
                tally[field][out] += 1
                per_set[setname][out] += 1
                if out == "CORRECT":
                    tally[field]["auto" if ver == "AUTO" else "review"] += 1
                if out in ("WRONG", "FALSE_POSITIVE") and ver == "AUTO":
                    wrong_auto.append((g["id"], field, want, have))
    lines = ["| Field | Verified values | Correct | Wrong | Missing | False positive | Correct → released automatically | Correct → sent to review |", "|---|---|---|---|---|---|---|---|"]
    tot = collections.Counter()
    for f in ORDER:
        t = tally[f]
        n = t["CORRECT"] + t["WRONG"] + t["MISSING"] + t["FALSE_POSITIVE"] + t["OK_ABSENT"]
        if not n:
            lines.append(f"| {LABEL[f]} | 0 | — | — | — | — | — | — |")
            continue
        lines.append(f"| {LABEL[f]} | {n} | {t['CORRECT']} ({100 * t['CORRECT'] / n:.0f}%) | {t['WRONG']} | {t['MISSING']} | {t['FALSE_POSITIVE']} | {t['auto']} | {t['review']} |")
        tot.update({"n": n, "c": t["CORRECT"], "w": t["WRONG"], "m": t["MISSING"], "fp": t["FALSE_POSITIVE"], "a": t["auto"], "r": t["review"]})
    lines.append(f"| **All fields** | {tot['n']} | {tot['c']} ({100 * tot['c'] / max(tot['n'], 1):.0f}%) | {tot['w']} | {tot['m']} | {tot['fp']} | {tot['a']} | {tot['r']} |")
    text = "\n".join(lines)
    print(text)
    print(f"\nWRONG or FALSE-POSITIVE values released as AUTO (no human review): {len(wrong_auto)}  {wrong_auto}")
    print("\nby set:", {k: dict(v) for k, v in per_set.items()})
    if "--md" in sys.argv:
        path = ROOT / sys.argv[sys.argv.index("--md") + 1]
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            "# Extraction validation against hand-verified bills\n\nGenerated by `scripts/validate_extraction.py` from `tests/gold/*.json` (values read off the printed bills by a person). "
            "These bills were used while developing the reader (the held-out sets became development data after their first-run scores were recorded — README, 66 % and 83 % of fields right on first read), "
            "so this table is a **regression** measure, not an estimate for unseen layouts. Plant mapping is not an extracted field (it comes from the Oracle PO distribution) and is not part of this table.\n\n"
            + text + f"\n\nWrong or false-positive values released as AUTO: **{len(wrong_auto)}** {wrong_auto}\n", encoding="utf-8")
        print("wrote", path)


if __name__ == "__main__":
    main()
