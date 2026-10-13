"""
pipeline.py
===========
Orchestrates a bill end to end:

  load (native text / OCR) -> classify -> candidates -> joint selection with electrical cross-checks
  -> billing-basis recognition -> per-field evidence, confidence and verification status -> JSON-able result.

Honesty rules enforced here:
  * a field that is not on the bill is NOT_FOUND (never 0, never a guess)
  * a value printed as zero is ZERO (kept distinct from NOT_FOUND)
  * conflicting candidates that cannot be resolved by an electrical identity => NEEDS_REVIEW
  * confidence below the (configurable) threshold on a critical field => NEEDS_REVIEW
  * billing basis (kWh / kVAh / Mixed / Unknown) is decided from how the energy charge was calculated,
    never from the mere presence of both readings
"""

import itertools
import re
from datetime import date

from . import ai_assist, charges
from . import fields as F
from .extractor import Candidate, Extractor
from .loader import load_document
from .model import Document
from .textnorm import detect_unit, fix_devanagari_order, parse_dates, parse_numbers

CRITICAL_CONFIDENCE = 0.95          # review threshold for critical fields (configurable; NOT an accuracy guarantee)
PF_TOLERANCE = 0.02                 # |kWh/kVAh - printed PF| accepted as a match
ENERGY_TOLERANCE = 0.005            # relative tolerance when matching billed units to kWh / kVAh

_BILL_KEYWORDS = [r"kwh", r"kvah", r"units? consumed|consumption", r"meter (no|number|sl)", r"consumer (no|number|id|name)|service (no|connection)",
                  r"tariff", r"sanction(ed)? load|contract(ed)? demand|connected load", r"energy charge", r"electricity (bill|duty|tax)|power bill|eb bill",
                  r"due date|bill date", r"power factor|\bp\.?f\.?\b", r"fixed charge|demand charge", r"discom|vidyut|bijli|electricity board|power (distribution|corporation)",
                  # Hindi / Devanagari wording of the same things
                  r"विद्युत|बिजली|ऊर्जा|उर्जा", r"उपभोक्ता|सर्विस क्रमांक|सेवा क्रमांक", r"मीटर", r"यूनिट|इकाई", r"देयक|बिल अवधि|बिल दिनांक|बिल तिथि|बिल माह",
                  r"नियत प्रभार|मांग प्रभार|ऊर्जा प्रभार|उर्जा प्रभार|विद्युत शुल्क", r"स्वीकृत भार|संविदा मांग|अनुबंधित मांग|अधिकतम मांग", r"पावर फैक्टर|शक्ति गुणांक"]


def _plain(v):
    """JSON-safe form of a candidate value (dates and date pairs become ISO strings)."""
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v.isoformat() if isinstance(v, date) else v


def _norm_unit(u):
    if not u:
        return None
    k = u.upper().replace(" ", "")
    return {"KVA": "kVA", "KW": "kW", "HP": "HP", "MVA": "MVA", "KVAH": "kVAh", "KWH": "kWh", "KVA/HP": "kVA/HP", "KVA(MD)": "kVA", "KW(MD)": "kW"}.get(k, u)


def _dedupe(cands: list) -> list:
    """Same value found several ways = one candidate with an agreement bonus."""
    by: dict = {}
    for c in sorted(cands, key=lambda c: -c.score):
        key = round(c.value, 4) if isinstance(c.value, (int, float)) else c.value
        if key in by:
            by[key].extra["agree"] = by[key].extra.get("agree", 0) + 1
            by[key].extra.setdefault("also", []).append(f"{c.relation} p{c.page}")
        else:
            by[key] = c
    return sorted(by.values(), key=lambda c: -c.score)


def _select_energy_and_pf(ex: Extractor) -> dict:
    """Pick (kWh, kVAh, PF) jointly so that kWh / kVAh reproduces the printed PF where possible."""
    # a printed 0 means "not billed on this basis", not a reading to pair with the other energy figure
    K = [c for c in _dedupe(ex.cands["kwh"]) if c.value][:6] or _dedupe(ex.cands["kwh"])[:6]
    V = [c for c in _dedupe(ex.cands["kvah"]) if c.value][:6] or _dedupe(ex.cands["kvah"])[:6]
    pf_b = [c for c in _dedupe(ex.cands["pf_billed"]) if c.value is not None and 0 < c.value <= 1.0001][:4]
    pf_g = [c for c in _dedupe(ex.cands["pf"]) if c.value is not None and 0 < c.value <= 1.0001][:4]
    P = pf_b + pf_g
    out = {"kwh": None, "kvah": None, "pf_billed": None, "pf": None, "cross_checked": False, "ratio": None}
    best = None
    for k, v in itertools.product(K or [None], V or [None]):
        if k is not None and v is not None:
            if v.value <= 0 or k.value > v.value * 1.0005:
                continue                                    # kWh can never exceed kVAh
            ratio = k.value / v.value
            diffs = [(abs(ratio - p.value), p) for p in P]
            d, p = min(diffs, key=lambda t: t[0]) if diffs else (None, None)
            bonus = (1.0 - d / PF_TOLERANCE) * (0.6 + 0.4 * p.score) if d is not None and d <= PF_TOLERANCE else 0.0
            total = k.score + v.score + 0.9 * bonus
            cand = (total, k, v, p if d is not None and d <= PF_TOLERANCE else None, ratio)
        else:
            x = k or v
            cand = ((x.score if x else 0.0), k, v, None, None)
        if best is None or cand[0] > best[0]:
            best = cand
    if best is None:
        allK, allV = _dedupe(ex.cands["kwh"]), _dedupe(ex.cands["kvah"])
        best = (0.0, allK[0] if allK else None, allV[0] if allV else None, None, None)
    if best:
        _, k, v, p, ratio = best
        out.update(kwh=k, kvah=v, ratio=ratio, cross_checked=p is not None)
        if p is not None:
            out["pf_billed" if p in pf_b else "pf"] = p
    if out["pf_billed"] is None and pf_b:
        out["pf_billed"] = pf_b[0]
    if out["pf"] is None and pf_g:
        out["pf"] = pf_g[0]
    return out


def _pick(ex: Extractor, fid: str, prefer_unit=None):
    cands = _dedupe(ex.cands[fid])
    if not cands:
        return None
    if prefer_unit and len(cands) > 1:
        top = cands[0]
        for c in cands[1:4]:
            if _norm_unit(c.unit) == prefer_unit and c.score >= top.score - 0.1 and _norm_unit(top.unit) != prefer_unit:
                c.extra["alt_unit_of"] = top.value
                return c
    return cands[0]


def _field_record(c: Candidate, spec: F.FieldSpec, ex_cands: list, cross_checked=False, value_override=None) -> dict:
    score = c.score
    agree = c.extra.get("agree", 0)
    others = [o for o in _dedupe(ex_cands) if o is not c and isinstance(o.value, type(c.value)) and o.value != c.value]
    rival = next((o for o in others if o.score >= 0.8 * c.score), None)
    conf = score + min(0.06, 0.03 * agree) + (0.08 if cross_checked else 0.0) - (0.0 if (cross_checked or rival is None) else 0.15)
    conf = max(0.0, min(0.99, conf))
    value = c.value if value_override is None else value_override
    if isinstance(value, tuple):
        value = [v.isoformat() if isinstance(v, date) else v for v in value]
    elif isinstance(value, date):
        value = value.isoformat()
    status = "FOUND"
    if spec.kind == "number" and value == 0:
        status = "ZERO"
    rec = {
        "value": value, "raw": c.raw, "unit": _norm_unit(c.unit), "page": c.page, "bbox": [round(v, 1) for v in c.bbox],
        "label": c.label, "relation": c.relation, "method": f"{c.src}:{c.relation}", "ocr_conf": round(c.ocr_conf, 3),
        "confidence": round(conf, 3), "status": status, "cross_checked": cross_checked,
        "direction": c.extra.get("direction"),
        "alternatives": [{"value": _plain(o.value), "label": o.label, "page": o.page,
                          "relation": o.relation, "score": round(o.score, 3)} for o in others[:3]],
        "evidence": c.note or f"{c.relation}: '{c.label}' -> '{c.raw}'",
        "verification": "AUTO" if (not spec.critical or conf >= CRITICAL_CONFIDENCE) and rival is None or cross_checked else "NEEDS_REVIEW",
    }
    if spec.critical and conf < CRITICAL_CONFIDENCE and not cross_checked:
        rec["verification"] = "NEEDS_REVIEW"
    return rec


_MONTH_NAMES = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_BILL_MONTH = re.compile(r"(?:month\s*of|bill\s*month|for\s*the\s*month|billing\s*month)\s*[:\-]?\s*([A-Za-z]{3,9})[\s,.\-/]*(\d{2,4})", re.I)


def _find_billing_month(doc: Document):
    """('2026-09', raw, page) from 'for the month of SEP - 2026' style wording, else None."""
    for c in doc.all_cells:
        m = _BILL_MONTH.search(c.text)
        if m and m.group(1)[:3].lower() in _MONTH_NAMES:
            y = int(m.group(2))
            y += 2000 if y < 100 else 0
            return f"{y:04d}-{_MONTH_NAMES[m.group(1)[:3].lower()]:02d}", m.group(0), c.page
    return None


def _classify(doc: Document) -> dict:
    text = fix_devanagari_order(" ".join(c.text for c in doc.all_cells)).lower()
    hits = [k for k in _BILL_KEYWORDS if re.search(k, text)]
    discom = next(((code, state) for pat, code, state in F.DISCOMS if re.search(pat, text)), None)
    score = len(hits) + (2 if discom else 0)
    kind = "ELECTRICITY_BILL" if score >= 5 else "POSSIBLE_BILL" if score >= 3 else "NOT_ELECTRICITY_BILL"
    third_party = False
    if not discom and kind != "ELECTRICITY_BILL" and re.search(r"electricity\s*(charge|bill|expense)|power\s*(bill|charge)|\beb\s*(bill|charge)", text) \
            and re.search(r"\bunits?\b|\bkwh\b|\bqnt\b|\bqty\b", text):
        kind, third_party = "POSSIBLE_BILL", True                  # landlord / sub-meter invoice: electricity is charged but it is not a DISCOM bill
    return {"verdict": kind, "keyword_hits": len(hits), "discom": discom[0] if discom else None, "state": discom[1] if discom else None,
            "issuer_type": "DISCOM" if discom else "THIRD_PARTY_INVOICE" if third_party else "OTHER_ISSUER"}


def _unit_rate_amount(numbers):
    """All (a, b, amount) in a charge row where a x b = amount (b may be paise). Which of a/b are the *units* is not
    decided here: the caller keeps the one that equals the metered kWh or kVAh."""
    nums = [n for n in numbers if n is not None]
    if len(nums) < 3:
        return []
    amount = nums[-1]
    if amount <= 0:
        return []
    out = []
    for a, b in itertools.combinations(nums[:-1], 2):
        if a > 0 and b > 0 and (abs(a * b - amount) <= 0.02 * amount + 1 or abs(a * b / 100.0 - amount) <= 0.02 * amount + 1):
            out.append((a, b, amount))
    return out


_RIDER_CATEGORIES = {"fixed_charges", "demand_charges", "wheeling", "fuel_adjustment", "electricity_duty", "taxes_surcharges",
                     "pf_adjustment", "excess_demand_penalty", "late_payment", "arrears", "adjustments_credits"}


def _billing_basis(ex: Extractor, kwh, kvah, text: str) -> dict:
    """kWh / kVAh / Mixed / Unknown from the arithmetic of the energy charge, not from which readings are printed.

    A charge row whose units x rate = amount AND whose units equal the metered kWh or kVAh is the proof. Rows that are
    clearly demand / duty / tax riders are ignored; if the bill has rows named as energy charges only those count."""
    k = kwh.value if kwh else None
    v = kvah.value if kvah else None
    low = text.lower()
    hint_kvah = bool(re.search(r"kvah\s*(billing|basis|tariff|based)|billed on kvah|(rs\.?|ps\.?|paise)\s*/\s*kvah|per\s*kvah|kvah\s*rate|rate\s*per\s*kvah", low))
    hint_kwh = bool(re.search(r"(rs\.?|ps\.?|paise)\s*/\s*kwh|per\s*kwh|kwh\s*rate|rate\s*per\s*kwh", low))
    def close(a, b):
        return a is not None and b is not None and b > 0 and abs(a - b) <= ENERGY_TOLERANCE * b + 0.5

    rows = []
    for ln in ex.charge_lines + [{"label": "(unlabelled row)", "numbers": u["numbers"], "page": u["page"], "category": None}
                                 for u in ex.unlabeled_rows]:
        label = ln["label"].lower()
        if "total" in label and "energy" not in label:
            continue
        if parse_dates(label) or any(w in label for w in ("difference", "reading", "multiplying", "constant", "consumption", "history")):
            continue                                        # meter-reading rows and date rows are not charge rows
        for a, b, amount in _unit_rate_amount(ln["numbers"]):
            # the metered reading is the "units"; the other factor is the rate
            if close(a, k) or close(a, v):
                units, rate = a, b
            elif close(b, k) or close(b, v):
                units, rate = b, a
            else:
                units, rate = max(a, b), min(a, b)
            rows.append({"units": units, "rate": rate, "amount": amount, "label": ln["label"], "page": ln["page"],
                         "category": ln["category"]})
            break
    ev = {"rows": rows[:8], "text_hint": {"kVAh": hint_kvah, "kWh": hint_kwh}}

    # 1) an explicit "Billed Units" figure that equals exactly one of the metered readings
    seen_kwh = seen_kvah = False
    for bu in _dedupe(ex.cands["billed_units"])[:4]:
        if bu.value and bu.score >= 0.6:
            seen_kvah |= bool(close(bu.value, v))
            seen_kwh |= bool(close(bu.value, k))
            ev["billed_units_label"] = {"value": bu.value, "label": bu.label}
    if seen_kvah != seen_kwh:
        return {"basis": "kVAh" if seen_kvah else "kWh", "confidence": 0.9,
                "evidence": ev | {"note": "explicit 'Billed Units' figure equals only one of the readings"}}
    if seen_kvah and seen_kwh and not rows:
        return {"basis": "Unknown", "confidence": 0.2,
                "evidence": ev | {"note": "billed units are printed for both kWh and kVAh; the bill does not say which one the charge uses"}}
    # 2) energy-charge rows whose own label names the unit ('Energy Charges (Rs/kVAh)')
    named = {"kVAh": 0, "kWh": 0}
    for ln in ex.charge_lines:
        if ln["category"] == "energy_charges":
            low_label = ln["label"].lower()
            named["kVAh"] += bool(re.search(r"kv\s*ah", low_label))
            named["kWh"] += bool(re.search(r"kw\s*h", low_label))
    ev["unit_named_energy_rows"] = named
    energy_rows = [r for r in rows if r["category"] == "energy_charges"]
    pool = energy_rows or [r for r in rows if r["category"] not in _RIDER_CATEGORIES]
    if pool:
        total = sum(r["units"] for r in pool)
        match_kvah = close(total, v) or any(close(r["units"], v) for r in pool)
        match_kwh = close(total, k) or any(close(r["units"], k) for r in pool)
        if match_kvah and not match_kwh:
            return {"basis": "kVAh", "confidence": 0.95, "evidence": ev}
        if match_kwh and not match_kvah:
            return {"basis": "kWh", "confidence": 0.95, "evidence": ev}
        if match_kwh and match_kvah:
            hint = "kVAh" if hint_kvah and not hint_kwh else "kWh" if hint_kwh and not hint_kvah else None
            return {"basis": hint or "Unknown", "confidence": 0.6 if hint else 0.2,
                    "evidence": ev | {"note": "kWh and kVAh are equal; basis cannot be told from units"}}
        if k and not v and any(0.9 <= r["units"] / k <= 1.1 for r in pool):
            return {"basis": "kWh", "confidence": 0.7, "evidence": ev | {"note": "billed units are within 10% of the only reading printed (kWh); "
                                                                              "difference looks like an adjustment (e.g. PF rebate)"}}
        if v and not k and any(0.9 <= r["units"] / v <= 1.1 for r in pool):
            return {"basis": "kVAh", "confidence": 0.7, "evidence": ev | {"note": "billed units are within 10% of the only reading printed (kVAh)"}}
        # units match neither reading and there is no single reading to compare with: do not guess
        return {"basis": "Unknown", "confidence": 0.2, "evidence": ev | {"note": "billed units match neither kWh nor kVAh"}}
    if named["kVAh"] != named["kWh"] and (named["kVAh"] or named["kWh"]):
        return {"basis": "kVAh" if named["kVAh"] else "kWh", "confidence": 0.75,
                "evidence": ev | {"note": "energy-charge rows are labelled with their unit"}}
    if hint_kvah != hint_kwh:
        return {"basis": "kVAh" if hint_kvah else "kWh", "confidence": 0.45,
                "evidence": ev | {"note": "from tariff wording only; energy-charge arithmetic not found"}}
    if k and not v:
        return {"basis": "kWh", "confidence": 0.5, "inferred": True,
                "evidence": ev | {"note": "inferred: only a kWh reading is printed (no kVAh); energy-charge arithmetic not found"}}
    return {"basis": "Unknown", "confidence": 0.0, "evidence": ev | {"note": "no energy-charge arithmetic found"}}


def _unreadable_script_warning(doc: Document):
    """The OCR model reads Latin text. Devanagari/other Indic print comes back as CJK noise: report it instead of ignoring it."""
    noisy = total = 0
    for p in doc.pages:
        if p.method != "ocr":
            continue
        for c in p.cells:
            total += 1
            if any("\u2e80" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef" for ch in c.text):
                noisy += 1
    if total and noisy / total >= 0.06:
        return (f"{noisy} of {total} OCR text boxes contain characters from a script the OCR model cannot read (typically Hindi or another "
                f"Indic language misread as Chinese); labels printed only in that script were not understood. Verify against the original.")
    return None


def analyze_document(doc: Document, critical_confidence: float = None, ai_provider=None) -> dict:
    global CRITICAL_CONFIDENCE
    if critical_confidence is not None:
        CRITICAL_CONFIDENCE = critical_confidence
    cls = _classify(doc)
    ex = Extractor(doc).run()
    res_fields: dict = {}
    flags: list = []

    sel = _select_energy_and_pf(ex)
    for fid in ("kwh", "kvah"):
        c = sel[fid]
        if c is not None:
            res_fields[fid] = _field_record(c, F.FIELDS[fid], ex.cands[fid], cross_checked=sel["cross_checked"])
    for fid in ("pf_billed", "pf"):
        c = sel[fid]
        if c is not None:
            res_fields[fid] = _field_record(c, F.FIELDS[fid], ex.cands[fid], cross_checked=sel["cross_checked"] and c is not None)

    for fid in ("reading_kwh_current", "reading_kwh_previous", "billed_units", "contract_demand", "sanctioned_load", "connected_load", "billing_demand", "multiplying_factor",
                "consumer_number", "bill_number", "bill_date", "due_date", "billing_period", "prev_reading_date",
                "present_reading_date", "meter_number", "consumer_name", "kvarh_lag", "kvarh_lead"):
        c = _pick(ex, fid)
        if c is not None:
            res_fields[fid] = _field_record(c, F.FIELDS[fid], ex.cands[fid])
    md = _pick(ex, "max_demand", prefer_unit="kVA")
    if md is not None:
        res_fields["max_demand"] = _field_record(md, F.FIELDS["max_demand"], ex.cands["max_demand"])

    if "billing_period" not in res_fields and "prev_reading_date" in res_fields and "present_reading_date" in res_fields:
        a, b = res_fields["prev_reading_date"], res_fields["present_reading_date"]
        if a["value"] and b["value"] and a["value"] < b["value"]:
            res_fields["billing_period"] = {
                "value": [a["value"], b["value"]], "raw": f"{a['raw']} .. {b['raw']}", "unit": None, "page": b["page"], "bbox": b["bbox"],
                "label": "derived from previous/present reading dates", "relation": "derived", "method": "derived:reading_dates",
                "ocr_conf": min(a["ocr_conf"], b["ocr_conf"]), "confidence": round(min(a["confidence"], b["confidence"]) * 0.9, 3),
                "status": "FOUND", "cross_checked": False, "alternatives": [], "evidence": "previous and present meter-reading dates",
                "verification": "NEEDS_REVIEW"}
    bp = res_fields.get("billing_period")
    if bp and isinstance(bp["value"], list) and len(bp["value"]) == 2:
        try:
            days = (date.fromisoformat(bp["value"][1]) - date.fromisoformat(bp["value"][0])).days
            res_fields["billing_days"] = {"value": days, "raw": f"{bp['value'][0]}..{bp['value'][1]}", "unit": "days", "page": bp["page"], "bbox": bp["bbox"],
                                          "label": "end - start of the billing period", "relation": "derived", "method": "calculated:period",
                                          "ocr_conf": bp.get("ocr_conf", 1.0), "confidence": bp["confidence"], "status": "CALCULATED", "cross_checked": False,
                                          "alternatives": [], "evidence": "billing period end date minus start date", "verification": bp["verification"]}
        except ValueError:
            pass
    bm = _find_billing_month(doc)
    if bm:
        res_fields["billing_month"] = {"value": bm[0], "raw": bm[1], "unit": None, "page": bm[2], "label": "bill month", "relation": "text",
                                       "method": "text:bill_month", "confidence": 0.8, "status": "FOUND", "verification": "AUTO",
                                       "evidence": f"'{bm[1]}'"}
        if "billing_period" not in res_fields:
            y, mo = int(bm[0][:4]), int(bm[0][5:])
            last = (date(y + (mo == 12), mo % 12 + 1, 1) - __import__("datetime").timedelta(days=1)).isoformat()
            res_fields["billing_period"] = {
                "value": [f"{bm[0]}-01", last], "raw": bm[1], "unit": None, "page": bm[2], "label": "derived from bill month",
                "relation": "derived", "method": "derived:billing_month", "confidence": 0.6, "status": "FOUND", "cross_checked": False,
                "alternatives": [], "evidence": f"whole calendar month of '{bm[1]}' (exact reading dates not printed)",
                "verification": "NEEDS_REVIEW"}
    tp = sorted(_dedupe(ex.total_payable), key=lambda c: (-round(c.score, 1), -(c.value or 0)))
    if tp:
        res_fields["total_payable"] = _field_record(tp[0], F.FieldSpec("total_payable", "number", critical=True), ex.total_payable)
    tb = _dedupe(ex.total_bill)
    if tb:
        res_fields["total_bill_amount"] = _field_record(tb[0], F.FieldSpec("total_bill_amount", "number"), ex.total_bill)

    for name, rec in charges.summarise(ex.charge_lines, (res_fields.get("total_payable") or {}).get("value")).items():
        res_fields.setdefault(name, rec)

    # derived, clearly labelled as calculated
    kw, kv = res_fields.get("kwh"), res_fields.get("kvah")
    if kw and kv and kv["value"]:
        res_fields["pf_calculated"] = {"value": round(kw["value"] / kv["value"], 4), "unit": None, "status": "CALCULATED",
                                       "method": "kWh / kVAh", "confidence": round(min(kw["confidence"], kv["confidence"]), 3),
                                       "evidence": f"{kw['value']} / {kv['value']}", "verification": "AUTO"}
    printed = res_fields.get("pf_billed") or res_fields.get("pf")
    if printed and "pf_calculated" in res_fields and printed["value"] is not None:
        diff = abs(res_fields["pf_calculated"]["value"] - printed["value"])
        res_fields["pf_calculated"]["difference_vs_bill"] = round(diff, 4)
        if diff > PF_TOLERANCE:
            flags.append(f"Printed PF {printed['value']} differs from kWh/kVAh {res_fields['pf_calculated']['value']} by {diff:.3f}")

    text = " ".join(c.text for c in doc.all_cells)
    basis = _billing_basis(ex, sel["kwh"], sel["kvah"], text)

    # Step E (optional, off unless an administrator configured an APPROVED provider): fill only what is still missing
    if ai_provider is not None or ai_assist.enabled():
        absent = [f for f in ("consumer_number", "bill_date", "billing_period", "kwh", "total_payable") if f not in res_fields]
        if "kvah" not in res_fields and "kwh" not in res_fields:
            absent.append("kvah")
        if absent:
            for fid, rec in ai_assist.assist(doc, absent, ai_provider).items():
                res_fields.setdefault(fid, rec)

    # not-found vs not-applicable vs zero is preserved, never defaulted
    missing = [f for f, s in F.FIELDS.items() if s.critical and f not in res_fields and f not in ("pf_billed", "pf", "kwh", "kvah")]
    if "pf_billed" not in res_fields and "pf" not in res_fields and "pf_calculated" not in res_fields:
        missing.append("pf")
    if "kwh" not in res_fields:
        missing.append("kwh")
    if "kvah" not in res_fields:
        missing.append("kvah")
    for fid, rec in res_fields.items():
        spec = F.FIELDS.get(fid)
        if spec and spec.id in ("pf", "pf_billed") and rec["value"] == 0:
            rec["status"] = "NOT_APPLICABLE"
            rec["verification"] = "NEEDS_REVIEW"
            rec["note"] = "PF printed as 0 — not physically possible; treated as 'not applicable' for this tariff/consumer class"
    distorted = [p.number for p in doc.pages if p.method == "ocr" and p.angle_spread >= 3.0]
    if distorted:
        msg = (f"Page(s) {distorted} look like a curved or skewed photograph (text-line angles vary by more than 3 degrees); labels and values "
               f"can be misaligned. Every value from those pages needs checking against the original.")
        doc.warnings.append(msg)
        flags.append(msg)
        for rec in res_fields.values():
            if rec.get("page") in distorted and rec.get("status") != "CALCULATED":
                rec["confidence"] = round(min(rec.get("confidence", 0.5), 0.8), 3)
                rec["verification"] = "NEEDS_REVIEW"
    warn = _unreadable_script_warning(doc)
    if warn:
        doc.warnings.append(warn)
        flags.append(warn)
    # third-party invoice: units x rate = amount on the electricity line gives the energy; nothing else is expected on such a document
    if cls["issuer_type"] == "THIRD_PARTY_INVOICE" and "kwh" not in res_fields:
        for pi in ex.pages:
            for row in pi.rows:
                line = " ".join(c.text for c in row)
                if not re.search(r"electric|power|energy|\beb\b", line, re.I):
                    continue
                nums = [n for c in row for n in parse_numbers(c.text)]
                found = _unit_rate_amount(nums)
                if not found:
                    continue
                a, b, amount = found[0]
                units, rate = (a, b) if a >= b else (b, a)
                bbox = [round(v, 1) for v in (row[0].x0, row[0].y0, row[-1].x1, row[-1].y1)]
                conf = min(c.conf for c in row)
                res_fields["kwh"] = {"value": units, "raw": str(units), "unit": "kWh", "page": row[0].page, "bbox": bbox, "label": line[:60],
                                     "relation": "derived", "method": "derived:units_x_rate", "ocr_conf": conf, "confidence": 0.6, "status": "FOUND",
                                     "cross_checked": True, "alternatives": [], "evidence": f"{units} units x {rate} = {amount}", "verification": "NEEDS_REVIEW"}
                res_fields.setdefault("total_payable", {"value": amount, "raw": str(amount), "unit": None, "page": row[0].page, "bbox": bbox,
                                                        "label": line[:60], "relation": "derived", "method": "derived:units_x_rate", "ocr_conf": conf,
                                                        "confidence": 0.6, "status": "FOUND", "cross_checked": False, "alternatives": [],
                                                        "evidence": "amount of the electricity line", "verification": "NEEDS_REVIEW"})
                break
            if "kwh" in res_fields:
                break
        flags.append("Third-party electricity invoice (sub-meter / landlord): not a DISCOM bill, so no power factor or demand data is available.")
    needs = sorted({f for f, r in res_fields.items() if r.get("verification") == "NEEDS_REVIEW"})
    return {
        "classification": cls,
        "document": {"kind": doc.kind, "pages": [{"n": p.number, "method": p.method, "cells": len(p.cells), "mean_conf": round(p.mean_conf, 3),
                                                  "rotation": p.rotation_applied, "note": p.ocr_note} for p in doc.pages],
                     "scripts": doc.scripts, "warnings": doc.warnings, "sha256": doc.sha256},
        "fields": res_fields,
        "missing_fields": sorted(set(missing)),
        "needs_review_fields": needs,
        "billing_basis": basis,
        "charge_lines": ex.charge_lines,
        "flags": flags,
        "candidate_counts": {k: len(v) for k, v in ex.cands.items() if v},
    }


def analyze_file(path, run_ocr: bool = True) -> dict:
    return analyze_document(load_document(path, run_ocr=run_ocr))
