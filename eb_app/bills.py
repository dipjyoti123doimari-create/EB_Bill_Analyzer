"""
bills.py
========
Turns an extraction (what the reader found, plus human corrections) into the canonical bill record the dashboards use:

    Plant -> Consumer account -> Billing period -> Bill -> PO -> Attachment

* The extraction and its per-field evidence stay untouched; electricity_bills is a derived snapshot that is rebuilt whenever an
  extraction is created or a field is corrected, so it can never disagree with the evidence.
* Several bills for one account and month (revised / supplementary) are ALL kept; the latest one is flagged is_primary so that
  totals are not double counted, and revision_count says how many exist.
* Duplicates, non-bills, unsupported and failed extractions get no bill row.
"""

import json
import re
from datetime import date, timedelta

from . import settings, storage
from .analysis import engine, tariff

BILL_STATUSES = ("VERIFIED", "NEEDS_REVIEW", "PARTIAL")
CHARGE_COLUMNS = {                      # bill column <- extraction field
    "energy_charges": "charge_energy_charges", "fixed_charges": "charge_fixed_charges", "demand_charges": "charge_demand_charges",
    "fuel_adjustment": "charge_fuel_adjustment", "electricity_duty": "charge_electricity_duty", "wheeling": "charge_wheeling",
    "taxes_surcharges": "charge_taxes_surcharges", "arrears": "charge_arrears", "adjustments_credits": "charge_adjustments_credits",
    "late_payment": "charge_late_payment",
}


def consumer_key(number) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(number or "").upper())


def _fields(conn, extraction_id) -> dict:
    return {r["field_name"]: r for r in conn.execute("SELECT * FROM electricity_bill_fields WHERE extraction_id = ?", (extraction_id,))}


def _num(f, name):
    r = f.get(name)
    if r is None or r["status"] not in ("FOUND", "ZERO", "CALCULATED") or r["value_num"] is None:
        return None
    return r["value_num"]


def _unit(f, name):
    r = f.get(name)
    return r["unit_normalized"] if r is not None and r["unit_normalized"] else None


def _text(f, name):
    r = f.get(name)
    if r is None or r["status"] not in ("FOUND", "ZERO", "CALCULATED"):
        return None
    return r["value_text"]


def _period(f):
    t = _text(f, "billing_period")
    if not t or " .. " not in t:
        return None, None
    a, b = [x.strip() for x in t.split(" .. ", 1)]
    try:
        sa, sb = date.fromisoformat(a), date.fromisoformat(b)
    except ValueError:
        return None, None
    return (a, b) if sa <= sb else (None, None)


def billing_month_of(start, end):
    """Month containing the middle of the period (a 15th-to-15th bill belongs to the month most of it falls in)."""
    if not start or not end:
        return None
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    return (s + timedelta(days=(e - s).days // 2)).strftime("%Y-%m")


def _charge(f, bill_name):
    """(value, status) for a charge category; absent => (None, 'NOT_FOUND') (never a silent zero)."""
    r = f.get(CHARGE_COLUMNS.get(bill_name, bill_name))
    if r is not None and r["status"] == "UNREADABLE":
        return None, "UNREADABLE"                       # the line is on the bill but its amount could not be read
    if r is None or r["value_num"] is None:
        return None, "NOT_FOUND"
    return r["value_num"], "ZERO" if r["status"] == "ZERO" or abs(r["value_num"]) < 1e-9 else "FOUND"


def consistency(b: dict):
    """(cost_amount, cost_source, flags). Cross-checks between figures of the SAME bill. A flagged bill stays visible, but its
    inconsistent figure is left out of company totals (the dashboard says how many bills were left out), because a total built
    on a misread number would be a fabricated number.
      PF_PAIR_IMPLAUSIBLE  kWh / kVAh is above 1 or below 0.30: one of the two readings is wrong
      COST_CONFLICT        the bill total and the payable amount differ by more than 10x, or the total is far from the sum of the
                           bill's own energy + demand + fixed charges
    """
    flags = []
    kwh, kvah = b.get("kwh"), b.get("kvah")
    if kwh and kvah and kwh > 0 and kvah > 0:
        r = kwh / kvah
        if r > 1.0005 or r < 0.30:
            flags.append("PF_PAIR_IMPLAUSIBLE")
    cd, md = b.get("contract_demand"), b.get("max_demand")
    if cd and md and cd > 0 and md > 0:
        cdb, f1, _ = engine.to_base(cd, b.get("contract_demand_unit"))
        mdb, f2, _ = engine.to_base(md, b.get("max_demand_unit"))
        ratio = (mdb / cdb) if (cdb and mdb and f1 == f2) else (md / cd if (not b.get("contract_demand_unit") or not b.get("max_demand_unit")) else None)
        if ratio is not None and ratio > engine.DEMAND_IMPLAUSIBLE_RATIO:
            flags.append("DEMAND_IMPLAUSIBLE")
    tba, tp = b.get("total_bill_amount"), b.get("total_payable")
    cost, src = None, None
    if tba and tba > 0:
        cost, src = tba, "BILL_TOTAL"
    elif tp and tp > 0:
        cost, src = tp, "PAYABLE"
    if tba and tp and tba > 0 and tp > 0 and max(tba, tp) / min(tba, tp) > 10:
        flags.append("COST_CONFLICT")
    comp = sum(v for v in (b.get("energy_charges"), b.get("demand_charges"), b.get("fixed_charges")) if v and v > 0)
    if cost and comp >= 1000 and (cost > 6 * comp or cost < 0.4 * comp):
        flags.append("COST_CONFLICT")
    return cost, src, sorted(set(flags))


def _pf_direction(conn, extraction_id):
    """LEAD / LAG as printed next to the PF; else, when both reactive readings are printed, whichever is larger (labelled as derived)."""
    row = conn.execute("SELECT result_json FROM electricity_bill_extractions WHERE id = ?", (extraction_id,)).fetchone()
    try:
        flds = json.loads(row[0] or "{}").get("fields", {})
    except (TypeError, ValueError):
        return None
    for k in ("pf_billed", "pf"):
        d = (flds.get(k) or {}).get("direction")
        if d:
            return str(d).upper()
    f = _fields(conn, extraction_id)
    lag, lead = _num(f, "kvarh_lag"), _num(f, "kvarh_lead")
    if lag is not None and lead is not None and (lag or lead):
        return ("LEAD" if lead > lag else "LAG") + " (derived from the kVARh lead / lag readings)"
    return None


def _context(conn, ext):
    if ext["attachment_id"] is not None:
        r = conn.execute(
            "SELECT p.po_header_id, p.po_number, p.plant_code, p.plant_name, p.consumer_hint, p.plant_org_id, a.attached_document_id AS att "
            "FROM electricity_attachments a JOIN electricity_pos p ON p.po_header_id = a.po_header_id WHERE a.attached_document_id = ?",
            (ext["attachment_id"],)).fetchone()
        if r is None:
            return {"source": "ORACLE_EBS"}
        return {"source": "ORACLE_EBS", "po_header_id": r["po_header_id"], "po_number": r["po_number"], "plant_code": r["plant_code"],
                "plant_name": r["plant_name"], "hint": r["consumer_hint"], "attachment_id": r["att"], "org_id": r["plant_org_id"]}
    u = conn.execute("SELECT * FROM electricity_uploads WHERE id = ?", (ext["upload_id"],)).fetchone()
    return {"source": "MANUAL_UPLOAD", "upload_id": ext["upload_id"], "po_number": u["po_number"], "plant_code": u["plant_code"],
            "plant_name": u["plant_name"], "hint": u["consumer_number"], "upload_month": u["billing_month"], "upload_discom": u["discom_code"]}


def _ensure_plant(conn, ctx, state):
    code = ctx.get("plant_code")
    if not code:
        return
    now = storage.now_iso()
    row = conn.execute("SELECT state FROM electricity_plants WHERE plant_code = ?", (code,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO electricity_plants (plant_code, plant_name, state, org_id, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
                     (code, ctx.get("plant_name"), state, ctx.get("org_id"), now, now))
    else:
        conn.execute("UPDATE electricity_plants SET last_seen = ?, plant_name = COALESCE(?, plant_name), state = COALESCE(state, ?) WHERE plant_code = ?",
                     (now, ctx.get("plant_name"), state, code))


def _ensure_account(conn, plant_code, number, discom, state, meter):
    key = consumer_key(number)
    if not plant_code or not key:
        return None
    now = storage.now_iso()
    row = conn.execute("SELECT id FROM electricity_consumer_accounts WHERE plant_code = ? AND consumer_key = ?", (plant_code, key)).fetchone()
    if row:
        conn.execute("UPDATE electricity_consumer_accounts SET last_seen = ?, discom_code = COALESCE(discom_code, ?), state = COALESCE(state, ?), "
                     "meter_number = COALESCE(?, meter_number) WHERE id = ?", (now, discom, state, meter, row["id"]))
        return row["id"]
    return conn.execute("INSERT INTO electricity_consumer_accounts (plant_code, consumer_key, consumer_number, discom_code, state, meter_number, first_seen, last_seen)"
                        " VALUES (?,?,?,?,?,?,?,?)", (plant_code, key, str(number), discom, state, meter, now, now)).lastrowid


def _regroup(conn, group_key):
    """Latest bill of an (account, month) is primary; all revisions stay on file."""
    if not group_key:
        return
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM electricity_bills WHERE group_key = ? ORDER BY COALESCE(bill_date, '') DESC, created_at DESC, id DESC", (group_key,))]
    for i, bid in enumerate(ids):
        conn.execute("UPDATE electricity_bills SET is_primary = ?, revision_count = ? WHERE id = ?", (1 if i == 0 else 0, len(ids), bid))


def refresh_bill(conn, extraction_id: int, evaluate_alerts: bool = True):
    """Rebuild the bill record, PF analysis and demand analysis of one extraction. Returns the bill id or None."""
    ext = conn.execute("SELECT * FROM electricity_bill_extractions WHERE id = ?", (extraction_id,)).fetchone()
    if ext is None:
        return None
    old = conn.execute("SELECT id, group_key FROM electricity_bills WHERE extraction_id = ?", (extraction_id,)).fetchone()
    if ext["archived"] or ext["bill_status"] not in BILL_STATUSES:
        from . import alerts
        if old:
            alerts.resolve_where(conn, "bill_id = ?", (old["id"],), "the bill was superseded or is no longer counted (re-read, duplicate or reclassified)")
            conn.execute("DELETE FROM electricity_bills WHERE id = ?", (old["id"],))
            _regroup(conn, old["group_key"])
        if ext["archived"]:
            alerts.resolve_where(conn, "extraction_id = ?", (extraction_id,), "the extraction was archived after a re-read")
        elif evaluate_alerts:
            alerts.evaluate_extraction(conn, extraction_id)
        return None

    f = _fields(conn, extraction_id)
    ctx = _context(conn, ext)
    state = ext["state"]
    consumer = _text(f, "consumer_number") or ctx.get("hint")
    start, end = _period(f)
    month = billing_month_of(start, end) or (ctx.get("upload_month") if ctx.get("source") == "MANUAL_UPLOAD" else None)
    plant = ctx.get("plant_code")
    _ensure_plant(conn, ctx, state)
    discom = ext["discom_code"] or ctx.get("upload_discom")
    account_id = _ensure_account(conn, plant, consumer, discom, state, _text(f, "meter_number"))
    acct = conn.execute("SELECT consumer_category, voltage_category FROM electricity_consumer_accounts WHERE id = ?", (account_id,)).fetchone() if account_id else None

    pf_billed = _num(f, "pf_billed")
    if pf_billed is None:
        pf_billed = _num(f, "pf")
    charges = {c: _charge(f, c) for c in CHARGE_COLUMNS}
    pen, pen_st = _charge(f, "pf_penalty")
    inc, inc_st = _charge(f, "pf_incentive")
    xd, xd_st = _charge(f, "charge_excess_demand_penalty")
    now = storage.now_iso()
    row = {
        "extraction_id": extraction_id, "source": ctx["source"], "plant_code": plant, "account_id": account_id,
        "po_header_id": ctx.get("po_header_id"), "po_number": ctx.get("po_number"), "attachment_id": ext["attachment_id"], "upload_id": ext["upload_id"],
        "bill_status": ext["bill_status"], "quality": "VERIFIED" if ext["bill_status"] == "VERIFIED" else "UNVERIFIED",
        "consumer_number": consumer, "meter_number": _text(f, "meter_number"), "discom_code": discom, "state": state,
        "bill_number": _text(f, "bill_number"), "bill_date": _text(f, "bill_date"), "due_date": _text(f, "due_date"),
        "period_start": start, "period_end": end, "billing_month": month,
        "billing_days": int(_num(f, "billing_days")) if _num(f, "billing_days") is not None else None,
        "billing_basis": ext["billing_basis"] or "Unknown",
        "kwh": _num(f, "kwh"), "kvah": _num(f, "kvah"), "pf_billed": pf_billed, "pf_calculated": _num(f, "pf_calculated"),
        "sanctioned_load": _num(f, "sanctioned_load"), "sanctioned_load_unit": _unit(f, "sanctioned_load"),
        "contract_demand": _num(f, "contract_demand"), "contract_demand_unit": _unit(f, "contract_demand"),
        "max_demand": _num(f, "max_demand"), "max_demand_unit": _unit(f, "max_demand"),
        "billing_demand": _num(f, "billing_demand"), "billing_demand_unit": _unit(f, "billing_demand"),
        "total_bill_amount": _num(f, "total_bill_amount"), "total_payable": _num(f, "total_payable"),
        "pf_penalty": pen, "pf_penalty_status": pen_st, "pf_incentive": inc, "pf_incentive_status": inc_st,
        "excess_demand_penalty": xd, "excess_demand_status": xd_st,
        "group_key": f"{account_id}|{month}" if account_id and month else None,
    }
    for col, (val, _st) in charges.items():
        row[col] = val
    row["cost_amount"], row["cost_source"], flags = consistency(row)
    row["data_flags"] = "," + ",".join(flags) + "," if flags else ","
    if old:
        sets = ", ".join(f"{c} = :{c}" for c in row if c != "extraction_id")
        conn.execute(f"UPDATE electricity_bills SET {sets}, updated_at = :now WHERE id = :id", {**row, "now": now, "id": old["id"]})
        bill_id = old["id"]
        if old["group_key"] != row["group_key"]:
            _regroup(conn, old["group_key"])
    else:
        cols = list(row)
        bill_id = conn.execute(f"INSERT INTO electricity_bills ({', '.join(cols)}, created_at, updated_at) "
                               f"VALUES ({', '.join(':' + c for c in cols)}, :now, :now)", {**row, "now": now}).lastrowid
    _regroup(conn, row["group_key"])

    cfg = settings.get_all(conn)
    facts = {**row, "pf_direction": _pf_direction(conn, extraction_id),
             "consumer_category": acct["consumer_category"] if acct else None, "voltage_category": acct["voltage_category"] if acct else None}
    pf, dem = engine.analyze_bill(facts, tariff.load_approved(conn), cfg)
    _write_analysis(conn, bill_id, pf, dem)
    if evaluate_alerts:
        from . import alerts
        alerts.evaluate_bill(conn, bill_id)
        alerts.evaluate_extraction(conn, extraction_id)
    return bill_id


def _write_analysis(conn, bill_id, pf, dem):
    now = storage.now_iso()
    pf = dict(pf)
    pf["notes_json"] = json.dumps(pf.pop("notes", []))
    dem = dict(dem)
    dem["notes_json"] = json.dumps(dem.pop("notes", []))
    conn.execute("DELETE FROM electricity_pf_analysis WHERE bill_id = ?", (bill_id,))
    conn.execute(f"INSERT INTO electricity_pf_analysis (bill_id, {', '.join(pf)}, computed_at) VALUES (?, {', '.join('?' * len(pf))}, ?)",
                 (bill_id, *pf.values(), now))
    conn.execute("DELETE FROM electricity_demand_analysis WHERE bill_id = ?", (bill_id,))
    conn.execute(f"INSERT INTO electricity_demand_analysis (bill_id, {', '.join(dem)}, computed_at) VALUES (?, {', '.join('?' * len(dem))}, ?)",
                 (bill_id, *dem.values(), now))


def refresh_all(conn, evaluate_alerts: bool = False) -> dict:
    """Rebuild every bill (after a tariff rule or setting changed, or to backfill earlier extractions)."""
    ids = [r[0] for r in conn.execute("SELECT id FROM electricity_bill_extractions WHERE archived = 0 ORDER BY id")]
    made = 0
    for i in ids:
        if refresh_bill(conn, i, evaluate_alerts=evaluate_alerts):
            made += 1
    return {"extractions": len(ids), "bills": made}


def reanalyze_all(conn) -> int:
    """Re-run only the PF / demand analysis for every bill (after a tariff rule is approved or a setting changes)."""
    cfg = settings.get_all(conn)
    rules = tariff.load_approved(conn)
    n = 0
    for b in conn.execute("SELECT b.*, a.consumer_category, a.voltage_category FROM electricity_bills b "
                          "LEFT JOIN electricity_consumer_accounts a ON a.id = b.account_id").fetchall():
        facts = dict(b)
        facts["pf_direction"] = _pf_direction(conn, b["extraction_id"])
        pf, dem = engine.analyze_bill(facts, rules, cfg)
        _write_analysis(conn, b["id"], pf, dem)
        n += 1
    return n
