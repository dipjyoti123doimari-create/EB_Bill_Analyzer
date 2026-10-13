"""
charges.py
==========
Turns the raw "label ... amount" lines of a bill into the separate charge fields the analysis engine needs
(fixed, demand, energy, fuel adjustment, electricity duty, wheeling, PF penalty, PF incentive, excess-demand penalty,
late payment, taxes/surcharges, arrears, adjustments/credits).

Rules
  * lines are never netted against each other; every category keeps its own evidence (the lines it came from)
  * a 'Total'/'Sub total' line is used only when it IS the category total ('Total Energy Charge'); otherwise the category's
    own lines are summed, and the field says so ('derived: sum of N lines')
  * the PF line is split by sign: a positive amount is a PENALTY, a negative amount an INCENTIVE (a combined 'PF Penal/Incentive'
    label is interpreted by the sign printed on the bill, never assumed)
  * a category with no line is simply absent; a printed 0.00 is kept as ZERO
"""

import re

SUMMARY_CATEGORIES = ("fixed_charges", "demand_charges", "energy_charges", "fuel_adjustment", "electricity_duty", "wheeling",
                      "excess_demand_penalty", "late_payment", "taxes_surcharges", "arrears", "adjustments_credits")

_TOTAL_WORDS = re.compile(r"\b(sub\s*-?\s*total|total)\b", re.I)
_OWN_TOTAL = {"energy_charges": re.compile(r"total\s*energy\s*charge", re.I), "demand_charges": re.compile(r"total\s*demand\s*charge", re.I)}


_MONEY_CHECKED = ("pf_adjustment", "excess_demand_penalty")


def _not_money(cat: str, ln: dict, total_payable) -> str | None:
    """Reason why a number on a penalty line cannot be the penalty amount, else None.
    Found on real bills: 'PF Penalty (Recorded PF : 0.88)' whose only number is the PF itself, 'PF Surcharge: 6807 KWH * Rs 0.54' whose only
    number is the RATE, and a label row whose number is the bill total."""
    amt = abs(ln["amount"])
    if cat == "pf_adjustment" and 0 < amt <= 1.0:
        return f"the only number on the line ({ln['amount']}) looks like a power factor or a rate, not a money amount"
    if cat in _MONEY_CHECKED and total_payable and total_payable > 0 and amt >= 0.9 * total_payable:
        return f"the number ({ln['amount']:,.2f}) is as large as the whole bill ({total_payable:,.2f}); it is probably a total, not this charge"
    return None


def summarise(charge_lines: list, total_payable=None) -> dict:
    """{field_name: record} for each charge category present. Records follow the same shape as extracted fields."""
    out: dict = {}
    by_cat: dict = {}
    rejected: dict = {}
    for ln in charge_lines:
        if ln.get("category"):
            reason = _not_money(ln["category"], ln, total_payable) if ln["category"] in _MONEY_CHECKED else None
            if reason:
                rejected.setdefault(ln["category"], []).append((ln, reason))
                continue
            by_cat.setdefault(ln["category"], []).append(ln)

    def record(name, value, lines, how):
        first = lines[0]
        out[name] = {
            "value": round(value, 2), "raw": f"{value:.2f}", "unit": "INR", "page": first["page"], "bbox": first["bbox"],
            "label": "; ".join(dict.fromkeys(l["label"] for l in lines))[:120], "relation": "charge_lines", "method": f"charges:{how}",
            "ocr_conf": min(l["conf"] for l in lines), "confidence": round(min(0.9, min(l["conf"] for l in lines) * 0.95), 3),
            "status": "ZERO" if abs(value) < 1e-9 else "FOUND", "cross_checked": False, "alternatives": [],
            "evidence": f"{how}: " + "; ".join(f"{l['label'][:30]} = {l['amount']}" for l in lines[:6]), "verification": "AUTO"}

    for cat in SUMMARY_CATEGORIES:
        lines = by_cat.get(cat, [])
        if not lines:
            continue
        own = _OWN_TOTAL.get(cat)
        totals = [l for l in lines if own and own.search(l["label"])]
        if totals:
            record(f"charge_{cat}", totals[-1]["amount"], [totals[-1]], "category total printed on the bill")
            continue
        parts = [l for l in lines if not _TOTAL_WORDS.search(l["label"])] or lines
        record(f"charge_{cat}", sum(l["amount"] for l in parts), parts, f"sum of {len(parts)} line(s)" if len(parts) > 1 else "single line")

    pf_lines = by_cat.get("pf_adjustment", [])
    if len(pf_lines) > 1:
        # a line such as 'PF Penalty (Recorded PF : 0.88) ... 0.88' repeats the power factor VALUE, not money; keep the money lines
        money = [l for l in pf_lines if abs(l["amount"]) > 1.0001 or len(pf_lines) == 1]
        pf_lines = money or pf_lines
    if pf_lines:
        net = sum(l["amount"] for l in pf_lines if not _TOTAL_WORDS.search(l["label"])) if len(pf_lines) > 1 else pf_lines[0]["amount"]
        if net > 1e-9:
            record("pf_penalty", net, pf_lines, "positive amount on the PF line = penalty")
        elif net < -1e-9:
            record("pf_incentive", abs(net), pf_lines, "negative amount on the PF line = incentive")
        else:
            record("pf_penalty", 0.0, pf_lines, "PF line printed as zero")
    # the label is on the bill but no number on its line can be the amount: say so (UNREADABLE), never report 0 or a wrong amount
    for cat, name in (("pf_adjustment", "pf_penalty"), ("excess_demand_penalty", "charge_excess_demand_penalty")):
        if cat in rejected and not by_cat.get(cat):
            ln, reason = rejected[cat][0]
            out[name] = {"value": None, "raw": None, "unit": "INR", "page": ln["page"], "bbox": ln["bbox"], "label": ln["label"][:120],
                         "relation": "charge_lines", "method": "charges:amount_unreadable", "ocr_conf": ln["conf"], "confidence": 0.3,
                         "status": "UNREADABLE", "cross_checked": False, "alternatives": [], "evidence": f"{ln['label'][:40]}: {reason}",
                         "verification": "NEEDS_REVIEW"}
    # an actual penalty is a financial finding: a person confirms it before it is reported (zero lines need no confirmation)
    for name in ("pf_penalty", "pf_incentive", "charge_excess_demand_penalty"):
        if name in out and out[name]["value"] is not None and abs(out[name]["value"]) > 1e-9:
            out[name]["verification"] = "NEEDS_REVIEW"
    return out
