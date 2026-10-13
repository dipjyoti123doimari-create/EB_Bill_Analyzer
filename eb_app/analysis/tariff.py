"""
tariff.py
=========
Effective-dated tariff-rule registry.

NO rule is shipped with the application. Rules are entered by an administrator from a tariff order / regulatory document
(`source_reference` is mandatory) and only rules with approval_status = APPROVED are ever used. When no approved rule applies,
the analysis shows the charges printed on the bill and marks every independent calculation "unavailable".

A rule is matched to a bill by state / DISCOM / consumer category / voltage category and by the BILLING PERIOD (never by today's
date): a bill whose period is not entirely covered by one approved rule gets no independent calculation.
"""

import json
from datetime import date

from .. import storage

BASES = ("energy_charges", "demand_charges", "energy_and_demand_charges", "fixed_energy_demand_charges")
ROUNDING = ("floor", "ceil", "exact")
EXCESS_BASES = ("over_contract_demand", "over_tolerance_limit")
DEMAND_UNITS = ("kVA", "kW")

JSON_FIELDS = ("pf_penalty_rule_json", "pf_incentive_rule_json", "excess_demand_rule_json", "demand_charge_rule_json", "tariff_rates_json")


class RuleError(ValueError):
    pass


def _num(d, key, lo=None, hi=None, required=True):
    if key not in d or d[key] in (None, ""):
        if required:
            raise RuleError(f"'{key}' is required")
        return None
    try:
        v = float(d[key])
    except (TypeError, ValueError):
        raise RuleError(f"'{key}' must be a number") from None
    if lo is not None and v < lo:
        raise RuleError(f"'{key}' must be >= {lo}")
    if hi is not None and v > hi:
        raise RuleError(f"'{key}' must be <= {hi}")
    return v


def validate_pf_rule(d: dict, kind: str) -> dict:
    """pf penalty / incentive: pct_per_step % of `base` for every `step` of PF below (penalty) or above (incentive) `threshold`."""
    out = {"threshold": _num(d, "threshold", 0.01, 1.0), "step": _num(d, "step", 0.0001, 0.5), "pct_per_step": _num(d, "pct_per_step", 0)}
    if d.get("rounding") not in ROUNDING:
        raise RuleError(f"'rounding' must be one of {ROUNDING}")
    if d.get("base") not in BASES:
        raise RuleError(f"'base' must be one of {BASES}")
    out["rounding"], out["base"] = d["rounding"], d["base"]
    mx = _num(d, "max_pct", 0, required=False)
    if mx is not None:
        out["max_pct"] = mx
    return out


def validate_excess_rule(d: dict) -> dict:
    if d.get("excess_basis") not in EXCESS_BASES:
        raise RuleError(f"'excess_basis' must be one of {EXCESS_BASES}")
    if d.get("unit") not in DEMAND_UNITS:
        raise RuleError(f"'unit' must be one of {DEMAND_UNITS}")
    return {"excess_basis": d["excess_basis"], "unit": d["unit"],
            "rate_per_unit": _num(d, "rate_per_unit", 0), "multiplier": _num(d, "multiplier", 0)}


def validate_rates(d: dict) -> dict:
    out = {}
    for k in ("energy_rate_per_kvah", "energy_rate_per_kwh"):
        v = _num(d, k, 0, required=False)
        if v is not None:
            out[k] = v
    if not out:
        raise RuleError("tariff_rates needs energy_rate_per_kvah and/or energy_rate_per_kwh")
    return out


def validate_demand_charge(d: dict) -> dict:
    return {"rate_per_unit": _num(d, "rate_per_unit", 0), "unit": d.get("unit") if d.get("unit") in DEMAND_UNITS else _raise("'unit' must be kVA or kW")}


def _raise(msg):
    raise RuleError(msg)


def _parse_date(text, name, required=True):
    if not text:
        if required:
            raise RuleError(f"{name} is required")
        return None
    try:
        return date.fromisoformat(str(text)[:10]).isoformat()
    except ValueError:
        raise RuleError(f"{name} must be a date YYYY-MM-DD") from None


def create_rule(conn, data: dict, user: str) -> int:
    """Insert a DRAFT rule after validating every part. Drafts are never used by the analysis."""
    if not user:
        raise RuleError("a rule needs the name of its author")
    state = (data.get("state") or "").strip() or None
    discom = (data.get("discom_code") or "").strip() or None
    if not state and not discom:
        raise RuleError("a rule must name a state or a DISCOM (a global rule would be an unsupported assumption)")
    eff_from = _parse_date(data.get("effective_from"), "effective_from")
    eff_to = _parse_date(data.get("effective_to"), "effective_to", required=False)
    if eff_to and eff_to < eff_from:
        raise RuleError("effective_to is before effective_from")
    parts = {}
    if data.get("pf_penalty_rule"):
        parts["pf_penalty_rule_json"] = json.dumps(validate_pf_rule(data["pf_penalty_rule"], "penalty"))
    if data.get("pf_incentive_rule"):
        parts["pf_incentive_rule_json"] = json.dumps(validate_pf_rule(data["pf_incentive_rule"], "incentive"))
    if data.get("excess_demand_rule"):
        parts["excess_demand_rule_json"] = json.dumps(validate_excess_rule(data["excess_demand_rule"]))
    if data.get("tariff_rates"):
        parts["tariff_rates_json"] = json.dumps(validate_rates(data["tariff_rates"]))
    if data.get("demand_charge_rule"):
        parts["demand_charge_rule_json"] = json.dumps(validate_demand_charge(data["demand_charge_rule"]))
    tol = data.get("contract_demand_tolerance_pct")
    tol = _num({"t": tol}, "t", 0, 100, required=False) if tol not in (None, "") else None
    basis = data.get("billing_basis_rule") or None
    if basis not in (None, "kWh", "kVAh", "Mixed"):
        raise RuleError("billing_basis_rule must be kWh, kVAh or Mixed")
    if not parts and tol is None and not basis:
        raise RuleError("a rule must define at least one of: PF penalty, PF incentive, tolerance, excess-demand rule, tariff rates, billing basis")
    pf_thr = None
    if "pf_penalty_rule_json" in parts:
        pf_thr = json.loads(parts["pf_penalty_rule_json"])["threshold"]
    cur = conn.execute(
        "INSERT INTO electricity_tariff_rules (state, discom_code, consumer_category, voltage_category, effective_from, effective_to,"
        " billing_basis_rule, pf_penalty_threshold, contract_demand_tolerance_pct, source_reference, created_by, created_at, note,"
        " pf_penalty_rule_json, pf_incentive_rule_json, excess_demand_rule_json, demand_charge_rule_json, tariff_rates_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (state, discom, (data.get("consumer_category") or "").strip() or None, (data.get("voltage_category") or "").strip() or None,
         eff_from, eff_to, basis, pf_thr, tol, (data.get("source_reference") or "").strip() or None, user, storage.now_iso(),
         data.get("note"), parts.get("pf_penalty_rule_json"), parts.get("pf_incentive_rule_json"), parts.get("excess_demand_rule_json"),
         parts.get("demand_charge_rule_json"), parts.get("tariff_rates_json")))
    return cur.lastrowid


def set_approval(conn, rule_id: int, status: str, user: str) -> None:
    if status not in ("APPROVED", "REJECTED", "DRAFT"):
        raise RuleError("invalid status")
    row = conn.execute("SELECT * FROM electricity_tariff_rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise RuleError("unknown rule")
    if status == "APPROVED" and not (row["source_reference"] or "").strip():
        raise RuleError("a rule cannot be approved without a source document / reference")
    conn.execute("UPDATE electricity_tariff_rules SET approval_status = ?, approved_by = ?, approved_at = ? WHERE id = ?",
                 (status, user if status != "DRAFT" else None, storage.now_iso() if status != "DRAFT" else None, rule_id))


def load_approved(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM electricity_tariff_rules WHERE approval_status = 'APPROVED'"):
        d = dict(r)
        for k in JSON_FIELDS:
            d[k[:-5]] = json.loads(d[k]) if d.get(k) else None
        out.append(d)
    return out


def _eq(a, b):
    return (a or "").strip().lower() == (b or "").strip().lower()


def find_rule(rules: list[dict], state, discom, category, voltage, period_start, period_end):
    """(rule | None, status, note). status: FOUND / NONE / AMBIGUOUS / SPANS_CHANGE / NO_PERIOD / CATEGORY_UNKNOWN"""
    if not period_start or not period_end:
        return None, "NO_PERIOD", "the billing period is unknown, so no effective-dated rule can be chosen"
    cands, category_blocked = [], False
    for r in rules:
        if r["state"] and not _eq(r["state"], state):
            continue
        if r["discom_code"] and not _eq(r["discom_code"], discom):
            continue
        if r["effective_from"] > period_end or (r["effective_to"] and r["effective_to"] < period_start):
            continue
        score = (1 if r["state"] else 0) + (1 if r["discom_code"] else 0)
        if r["consumer_category"]:
            if not category:
                category_blocked = True
                continue
            if not _eq(r["consumer_category"], category):
                continue
            score += 1
        if r["voltage_category"]:
            if not voltage:
                category_blocked = True
                continue
            if not _eq(r["voltage_category"], voltage):
                continue
            score += 1
        cands.append((score, r))
    if not cands:
        if category_blocked:
            return None, "CATEGORY_UNKNOWN", "an approved rule exists but the consumer's category / voltage level is not recorded"
        return None, "NONE", "no approved tariff rule for this state/DISCOM and billing period"
    full = [(s, r) for s, r in cands if r["effective_from"] <= period_start and (not r["effective_to"] or r["effective_to"] >= period_end)]
    if not full:
        return None, "SPANS_CHANGE", "the billing period spans a tariff change; the bill's own charges are shown, no independent calculation"
    best = max(s for s, _ in full)
    top = [r for s, r in full if s == best]
    if len(top) > 1:
        return None, "AMBIGUOUS", f"{len(top)} approved rules match equally (ids {', '.join(str(r['id']) for r in top)})"
    return top[0], "FOUND", f"tariff rule #{top[0]['id']} ({top[0]['source_reference']})"
