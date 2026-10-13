"""
engine.py
=========
Power-factor, demand and cost analysis of ONE bill (Phase 3). Pure functions: a dict of bill facts in, a dict of results out.

Rules this module enforces
  * ACTUAL amounts (penalty / incentive / excess-demand penalty printed on the bill) and ESTIMATES (theoretical kVAh saving) are
    separate columns with separate status values; nothing here ever adds one to the other.
  * The billed PF is never overwritten; the PF recomputed from kWh / kVAh sits beside it with a validation status.
  * An independent expected penalty is computed ONLY from an APPROVED, effective-dated tariff rule. Without one the status is
    UNAVAILABLE_NO_RULE and no number is produced.
  * kW is never compared with kVA (no justified conversion without the power factor of the peak); HP <-> kW uses the physical
    definition 1 HP = 0.7457 kW. A demand figure labelled kWh / kVAh is an energy figure and is rejected.
  * Missing / zero / not applicable stay distinct: a value that is absent is None with an explanatory status, never 0.
"""

import math

from . import tariff

HP_TO_KW = 0.7457            # 1 mechanical horsepower = 745.7 W (physical definition)
PF_EPS = 1.0005              # kWh may exceed kVAh by rounding only
PF_MIN_PLAUSIBLE = 0.30      # a monthly kWh/kVAh below this is not a real power factor of a working consumer: a reading was misread
DEMAND_IMPLAUSIBLE_RATIO = 3.0   # recorded demand above 3x the contract demand is treated as a misread, not as a violation (a real excess is far smaller)

_UNITS = {                    # unit -> (family, factor to the family base unit)
    "kva": ("apparent", 1.0), "mva": ("apparent", 1000.0), "va": ("apparent", 0.001),
    "kw": ("real", 1.0), "mw": ("real", 1000.0), "w": ("real", 0.001),
    "hp": ("real", HP_TO_KW), "bhp": ("real", HP_TO_KW),
}
_ENERGY_UNITS = {"kvah", "kwh", "mvah", "mwh", "vah", "wh"}


# --------------------------------------------------------------------------- power factor
def calc_pf(kwh, kvah):
    """kWh / kVAh, or (None, reason). Only for a physically possible pair taken from the same bill."""
    if kwh is None or kvah is None:
        return None, "kWh and kVAh are both needed"
    if kvah <= 0 or kwh <= 0:
        return None, "kWh and kVAh must be positive"
    if kwh > kvah * PF_EPS:
        return None, "kWh is larger than kVAh, which is physically impossible: one of the readings is wrong"
    if kwh < kvah * PF_MIN_PLAUSIBLE:
        return None, (f"kWh / kVAh = {kwh / kvah:.3f}, below {PF_MIN_PLAUSIBLE}: implausible for a working plant, so one of the readings is "
                      f"probably wrong")
    return round(min(1.0, kwh / kvah), 4), None


def validate_pf(billed, calc, tol, calc_reason=None):
    """(difference, status)."""
    if billed is None and calc is None:
        return None, "NO_PF"
    if billed is None:
        return None, "NOT_COMPARABLE_NO_BILLED_PF"
    if calc is None:
        return None, "INVALID_ENERGY" if calc_reason and ("impossible" in calc_reason or "implausible" in calc_reason) else "NOT_COMPARABLE_NO_KWH_KVAH"
    diff = round(billed - calc, 4)
    return diff, "MATCH" if abs(diff) <= tol + 1e-9 else "MISMATCH"


def classify_pf(pf, cfg):
    if pf is None:
        return "UNKNOWN"
    if pf >= cfg["pf_excellent"] - 1e-9:
        return "EXCELLENT"
    if pf >= cfg["pf_good"] - 1e-9:
        return "GOOD"
    if pf >= cfg["pf_attention"] - 1e-9:
        return "ATTENTION"
    return "CRITICAL"


def _base_amount(base, bill):
    parts = {"energy_charges": ["energy_charges"], "demand_charges": ["demand_charges"],
             "energy_and_demand_charges": ["energy_charges", "demand_charges"],
             "fixed_energy_demand_charges": ["fixed_charges", "energy_charges", "demand_charges"]}[base]
    vals = [bill.get(p) for p in parts]
    if any(v is None for v in vals):
        return None, [p for p, v in zip(parts, vals) if v is None]
    return sum(vals), []


def _steps(gap, step, rounding):
    x = gap / step
    return math.floor(x + 1e-9) if rounding == "floor" else math.ceil(x - 1e-9) if rounding == "ceil" else x


def expected_pf_amount(rule, pf, bill, kind):
    """(amount | None, status, note). kind: 'penalty' (PF below threshold) or 'incentive' (PF above)."""
    thr = rule["threshold"]
    gap = (thr - pf) if kind == "penalty" else (pf - thr)
    if gap <= 1e-9:
        return 0.0, "COMPUTED", f"PF {pf:.4f} is {'at or above' if kind == 'penalty' else 'at or below'} the rule threshold {thr}"
    base, missing = _base_amount(rule["base"], bill)
    if base is None:
        return None, "UNAVAILABLE_BASE_MISSING", f"the bill's {', '.join(missing)} was not extracted"
    pct = _steps(gap, rule["step"], rule["rounding"]) * rule["pct_per_step"]
    if rule.get("max_pct") is not None:
        pct = min(pct, rule["max_pct"])
    return round(base * pct / 100.0, 2), "COMPUTED", f"{pct:.4g}% of {rule['base']} ({base:,.2f}) for PF {pf:.4f} vs threshold {thr}"


def analyze_pf(bill, rule, rule_status, rule_note, cfg):
    notes = []
    kwh, kvah = bill.get("kwh"), bill.get("kvah")
    billed = bill.get("pf_billed")
    calc, why = calc_pf(kwh, kvah)
    diff, validation = validate_pf(billed, calc, cfg["pf_tolerance"], why)
    if why and (kwh is not None or kvah is not None) and validation not in ("NO_PF",):
        notes.append(f"calculated PF unavailable: {why}")
    basis = bill.get("billing_basis") or "Unknown"

    if billed is not None and (validation != "MISMATCH"):
        pf_used, source = billed, "BILLED"
    elif billed is not None:
        pf_used, source = billed, "BILLED_CONFLICTS_WITH_KWH_KVAH"
    elif calc is not None:
        pf_used, source = calc, "CALCULATED_FROM_KWH_KVAH"
        notes.append("the bill prints no PF; the PF shown is kWh / kVAh")
    else:
        pf_used, source = None, None
    out = {
        "billing_basis": basis, "pf_billed": billed, "pf_calculated": calc, "pf_difference": diff, "validation_status": validation,
        "pf_used": pf_used, "pf_source": source, "performance_status": classify_pf(pf_used, cfg), "direction": bill.get("pf_direction"),
        "actual_pf_penalty": bill.get("pf_penalty"), "actual_pf_incentive": bill.get("pf_incentive"),
        "penalty_status": bill.get("pf_penalty_status") or ("NOT_FOUND" if bill.get("pf_penalty") is None else "FOUND"),
        "expected_pf_penalty": None, "expected_status": "UNAVAILABLE_NO_RULE", "expected_vs_bill": None, "tariff_rule_id": None,
        "current_kvah": None, "target_pf": cfg["target_pf"], "target_kvah": None, "kvah_reduction": None, "marginal_rate": None,
        "rate_source": None, "estimated_savings": None, "savings_status": "UNAVAILABLE",
    }
    if bill.get("pf_direction"):
        notes.append(f"PF direction on the bill: {bill['pf_direction']}")
    if validation == "MISMATCH":
        notes.append(f"billed PF {billed} differs from kWh/kVAh PF {calc} by {abs(diff):.3f} (tolerance {cfg['pf_tolerance']})")
    if validation == "INVALID_ENERGY":
        notes.append("kWh and kVAh are inconsistent with each other (see above): they need review before the PF can be validated")

    # ---- independent expected penalty / incentive (approved rule only)
    if rule is None:
        out["expected_status"] = {"NONE": "UNAVAILABLE_NO_RULE", "AMBIGUOUS": "UNAVAILABLE_AMBIGUOUS_RULE", "SPANS_CHANGE": "UNAVAILABLE_TARIFF_CHANGE",
                                  "NO_PERIOD": "UNAVAILABLE_NO_PERIOD", "CATEGORY_UNKNOWN": "UNAVAILABLE_CATEGORY_UNKNOWN"}.get(rule_status, "UNAVAILABLE_NO_RULE")
        notes.append(rule_note)
    else:
        out["tariff_rule_id"] = rule["id"]
        if rule.get("pf_penalty_rule") is None and rule.get("pf_incentive_rule") is None:
            out["expected_status"] = "UNAVAILABLE_RULE_HAS_NO_PF_PART"
        elif pf_used is None:
            out["expected_status"] = "UNAVAILABLE_NO_PF"
        else:
            pf_for_rule = billed if billed is not None else calc
            if rule.get("pf_penalty_rule"):
                amt, st, note = expected_pf_amount(rule["pf_penalty_rule"], pf_for_rule, bill, "penalty")
                out["expected_pf_penalty"], out["expected_status"] = amt, st
                notes.append(note)
                actual = bill.get("pf_penalty")
                if amt is not None and actual is not None:
                    out["expected_vs_bill"] = round(actual - amt, 2)
                    if abs(actual - amt) > max(1.0, 0.005 * max(abs(amt), abs(actual))):
                        out["expected_status"] = "MISMATCH_WITH_BILL"
                    else:
                        out["expected_status"] = "MATCHES_BILL"
                elif amt is not None:
                    out["expected_status"] = "COMPUTED_BILL_LINE_NOT_FOUND"
            if rule.get("pf_incentive_rule") and not rule.get("pf_penalty_rule"):
                amt, st, note = expected_pf_amount(rule["pf_incentive_rule"], pf_for_rule, bill, "incentive")
                notes.append("expected incentive " + (f"{amt:,.2f}: " if amt is not None else "unavailable: ") + note)
                out["expected_status"] = "INCENTIVE_" + st

    # ---- theoretical kVAh saving (never an actual penalty)
    _savings(out, bill, rule, cfg, notes)
    out["notes"] = notes
    return out


def _savings(out, bill, rule, cfg, notes):
    basis = out["billing_basis"]
    kwh = bill.get("kwh")
    if basis == "kWh":
        out["savings_status"] = "NOT_APPLICABLE_KWH_BASIS"
        return
    if basis != "kVAh":
        out["savings_status"] = "UNAVAILABLE_BASIS_" + basis.upper()
        notes.append(f"billing basis is {basis}: a kVAh saving cannot be estimated")
        return
    if kwh is None or kwh <= 0:
        out["savings_status"] = "UNAVAILABLE_NO_KWH"
        return
    if out["validation_status"] in ("MISMATCH", "INVALID_ENERGY"):
        out["savings_status"] = "UNAVAILABLE_PF_CONFLICT"
        notes.append("saving not estimated: the PF on the bill and kWh/kVAh disagree; review the bill first")
        return
    pf = out["pf_calculated"] if out["pf_calculated"] is not None else out["pf_billed"]
    if pf is None or not 0 < pf <= 1:
        out["savings_status"] = "UNAVAILABLE_NO_PF"
        return
    target = cfg["target_pf"]
    out["current_kvah"] = round(kwh / pf, 2)
    out["target_kvah"] = round(kwh / target, 2)
    if pf >= target - 1e-9:
        out["kvah_reduction"] = 0.0
        out["estimated_savings"] = 0.0
        out["savings_status"] = "AT_OR_ABOVE_TARGET"
        return
    out["kvah_reduction"] = round(out["current_kvah"] - out["target_kvah"], 2)
    rate = None
    if rule and rule.get("tariff_rates") and rule["tariff_rates"].get("energy_rate_per_kvah"):
        rate, out["rate_source"] = rule["tariff_rates"]["energy_rate_per_kvah"], "TARIFF_RULE"
    elif bill.get("energy_charges") and bill.get("kvah"):
        rate, out["rate_source"] = bill["energy_charges"] / bill["kvah"], "BILL_AVERAGE_ENERGY_RATE"
        notes.append("rate is the bill's average energy charge per kVAh (not a verified marginal rate); the saving is only indicative")
    if rate is None or rate <= 0:
        out["savings_status"] = "UNAVAILABLE_NO_RATE"
        return
    out["marginal_rate"] = round(rate, 4)
    out["estimated_savings"] = round(out["kvah_reduction"] * rate, 2)
    out["savings_status"] = "ESTIMATE" if bill.get("quality") == "VERIFIED" else "ESTIMATE_UNVERIFIED_INPUTS"
    notes.append("THEORETICAL saving if PF were raised to the target; it is not a billed amount")


# --------------------------------------------------------------------------- demand
def to_base(value, unit):
    """(base_value, family, note) or (None, None, reason)."""
    if value is None:
        return None, None, "no value"
    u = (unit or "").strip().lower()
    if not u:
        return None, None, "unit not recorded"
    if u in _ENERGY_UNITS:
        return None, None, f"'{unit}' is an energy unit, not a demand unit"
    if u not in _UNITS:
        return None, None, f"unknown demand unit '{unit}'"
    fam, f = _UNITS[u]
    return value * f, fam, ("converted from HP at 1 HP = 0.7457 kW" if u in ("hp", "bhp") else None)


def compare_demand(cd, cd_unit, md, md_unit):
    """(status, utilisation %, excess in CD units, note)."""
    if cd is None or md is None:
        return "MISSING_VALUE", None, None, "contract demand or maximum demand was not extracted"
    cdb, fam1, n1 = to_base(cd, cd_unit)
    mdb, fam2, n2 = to_base(md, md_unit)
    if cdb is None or mdb is None:
        return "UNIT_UNKNOWN", None, None, f"cannot compare: contract demand {n1 or 'ok'}; maximum demand {n2 or 'ok'}"
    if fam1 != fam2:
        return "UNITS_INCOMPATIBLE", None, None, (f"contract demand is in {cd_unit} and maximum demand in {md_unit}: kW and kVA cannot be compared "
                                                 f"without a justified conversion (the PF at the peak is not known)")
    if cdb <= 0:
        return "NO_CONTRACT_DEMAND", None, None, "contract demand is zero"
    util = round(mdb / cdb * 100.0, 2)
    excess = max(0.0, (mdb - cdb)) / (cdb / cd) if cd else 0.0        # expressed in the contract-demand unit
    return "COMPARABLE", util, round(excess, 3), n1 or n2


def analyze_demand(bill, rule, rule_status, rule_note, cfg):
    notes = []
    cd, cdu, md, mdu = bill.get("contract_demand"), bill.get("contract_demand_unit"), bill.get("max_demand"), bill.get("max_demand_unit")
    status, util, excess, note = compare_demand(cd, cdu, md, mdu)
    if status == "COMPARABLE" and util is not None and util > DEMAND_IMPLAUSIBLE_RATIO * 100:
        status, note = "IMPLAUSIBLE_VALUE", (f"maximum demand {md:g} is {util:.0f}% of the contract demand {cd:g}; a real excess is never this large, so one of the two "
                                             f"figures was probably misread. It was not used for utilisation or violation checks.")
        util = excess = None
    if note:
        notes.append(note)
    out = {
        "contract_demand": cd, "contract_demand_unit": cdu, "max_demand": md, "max_demand_unit": mdu, "billing_demand": bill.get("billing_demand"),
        "comparison_status": status, "utilization_pct": util, "excess_demand": excess,
        "exceeds_contract": (1 if excess and excess > 1e-9 else 0) if status == "COMPARABLE" else None,
        "violation_status": "NOT_ASSESSABLE", "tolerance_pct": None, "tariff_rule_id": None,
        "actual_excess_penalty": bill.get("excess_demand_penalty"),
        "penalty_status": bill.get("excess_demand_status") or ("NOT_FOUND" if bill.get("excess_demand_penalty") is None else "FOUND"),
        "expected_excess_penalty": None, "expected_status": "UNAVAILABLE_NO_RULE", "expected_vs_bill": None,
    }
    penalty = bill.get("excess_demand_penalty")
    if status == "COMPARABLE":
        if rule is None:
            out["violation_status"] = "UNVERIFIED_NO_RULE" if out["exceeds_contract"] else "WITHIN_CONTRACT"
            if out["exceeds_contract"]:
                notes.append("recorded demand is above the contract demand, but without an approved tariff rule it cannot be called a "
                             "violation or a penalty case")
            notes.append(rule_note)
            out["expected_status"] = {"NONE": "UNAVAILABLE_NO_RULE", "AMBIGUOUS": "UNAVAILABLE_AMBIGUOUS_RULE", "SPANS_CHANGE": "UNAVAILABLE_TARIFF_CHANGE",
                                      "NO_PERIOD": "UNAVAILABLE_NO_PERIOD", "CATEGORY_UNKNOWN": "UNAVAILABLE_CATEGORY_UNKNOWN"}.get(rule_status, "UNAVAILABLE_NO_RULE")
        else:
            out["tariff_rule_id"] = rule["id"]
            tol = rule.get("contract_demand_tolerance_pct")
            if tol is None:
                out["violation_status"] = "UNVERIFIED_RULE_HAS_NO_TOLERANCE" if out["exceeds_contract"] else "WITHIN_CONTRACT"
                out["expected_status"] = "UNAVAILABLE_RULE_HAS_NO_EXCESS_DEMAND_PART"
            else:
                out["tolerance_pct"] = tol
                limit_ratio = 1 + tol / 100.0
                over = util is not None and util > 100.0 * limit_ratio + 1e-9
                out["violation_status"] = "VIOLATION" if over else "WITHIN_PERMITTED_LIMIT"
                er = rule.get("excess_demand_rule")
                if er is None:
                    out["expected_status"] = "UNAVAILABLE_RULE_HAS_NO_EXCESS_DEMAND_PART"
                else:
                    unit_ok = er["unit"].lower() == "kva" and to_base(1, cdu)[1] == "apparent" or er["unit"].lower() == "kw" and to_base(1, cdu)[1] == "real"
                    if not unit_ok:
                        out["expected_status"] = "UNAVAILABLE_UNIT_MISMATCH"
                    elif not over:
                        out["expected_excess_penalty"], out["expected_status"] = 0.0, "COMPUTED"
                    else:
                        cdb = to_base(cd, cdu)[0]
                        mdb = to_base(md, mdu)[0]
                        ref = cdb * limit_ratio if er["excess_basis"] == "over_tolerance_limit" else cdb
                        out["expected_excess_penalty"] = round((mdb - ref) * er["rate_per_unit"] * er["multiplier"], 2)
                        out["expected_status"] = "COMPUTED"
                        notes.append(f"expected = ({mdb:g} - {ref:g}) x {er['rate_per_unit']} x {er['multiplier']} ({er['excess_basis']})")
                    if out["expected_excess_penalty"] is not None and penalty is not None:
                        out["expected_vs_bill"] = round(penalty - out["expected_excess_penalty"], 2)
                        out["expected_status"] = "MATCHES_BILL" if abs(out["expected_vs_bill"]) <= max(1.0, 0.005 * abs(penalty)) else "MISMATCH_WITH_BILL"
    else:
        out["violation_status"] = "NOT_ASSESSABLE"
    # the DISCOM itself billing an excess-demand penalty is an actual finding, independent of our rule matching
    if penalty is not None and penalty > 1e-9:
        out["violation_status"] = "VIOLATION_BILLED"
        notes.append("the electricity board has billed an excess-demand penalty on this bill")
    out["notes"] = notes
    return out


def analyze_bill(bill, rules, cfg):
    """Both analyses for one bill. rules: approved rules from tariff.load_approved()."""
    rule, st, note = tariff.find_rule(rules, bill.get("state"), bill.get("discom_code"), bill.get("consumer_category"),
                                      bill.get("voltage_category"), bill.get("period_start"), bill.get("period_end"))
    return analyze_pf(bill, rule, st, note, cfg), analyze_demand(bill, rule, st, note, cfg)
