"""
alerts.py
=========
In-app alerts (Phase 5.7). Every alert has a dedupe key, so a condition that is still true on the next 2-hourly scan updates the
SAME open alert instead of creating a new one. When the condition disappears (a correction, a new reading) the alert is resolved
automatically with a note; a person can also acknowledge or resolve it.

Alert types
   1 EXTRACTION_FAILED          2 MISSING_PF              3 MISSING_ENERGY          4 PF_INCONSISTENT
   5 PF_BELOW_THRESHOLD         6 PF_PENALTY_ACTUAL       7 CONTRACT_DEMAND_EXCEEDED
   8 EXCESS_DEMAND_PENALTY_ACTUAL   9 DUPLICATE_BILL      10 DOWNLOAD_FAILURE       11 SCHEDULER_FAILURE
  12 MISSING_EXPECTED_BILL

Severity: CRITICAL / WARNING / INFO. A finding based on a bill that nobody has verified yet says so in its title; hypothetical
savings never raise an alert.
"""

import json
from datetime import date, datetime, timedelta

from . import settings, storage
from .analysis import engine

SEVERITY_RANK = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
BILL_TYPES = ("MISSING_PF", "MISSING_ENERGY", "PF_INCONSISTENT", "PF_BELOW_THRESHOLD", "PF_PENALTY_ACTUAL", "CONTRACT_DEMAND_EXCEEDED",
              "EXCESS_DEMAND_PENALTY_ACTUAL", "REVISED_BILL", "PENALTY_UNREADABLE", "COST_CONFLICT", "DEMAND_IMPLAUSIBLE")
EXT_TYPES = ("EXTRACTION_FAILED", "DUPLICATE_BILL")


# --------------------------------------------------------------------------- store
def raise_alert(conn, type_, severity, dedupe_key, title, message="", action="", bill_id=None, extraction_id=None, account_id=None,
                plant_code=None, detail=None, now=None) -> tuple[int, bool]:
    """Create the alert or refresh the open one with the same key. Returns (id, created)."""
    now = now or storage.now_iso()
    row = conn.execute("SELECT id, severity FROM electricity_alerts WHERE dedupe_key = ? AND status <> 'RESOLVED'", (dedupe_key,)).fetchone()
    detail_json = json.dumps(detail or {}, default=str)
    if row:
        conn.execute("UPDATE electricity_alerts SET last_seen = ?, occurrences = occurrences + 1, severity = ?, title = ?, message = ?, "
                     "required_action = ?, detail_json = ?, bill_id = COALESCE(?, bill_id), plant_code = COALESCE(?, plant_code) WHERE id = ?",
                     (now, severity, title, message, action, detail_json, bill_id, plant_code, row["id"]))
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO electricity_alerts (alert_type, severity, bill_id, extraction_id, account_id, plant_code, dedupe_key, title, message,"
        " required_action, detail_json, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (type_, severity, bill_id, extraction_id, account_id, plant_code, dedupe_key, title, message, action, detail_json, now, now))
    return cur.lastrowid, True


def resolve_where(conn, where_sql, params, note, user="system"):
    now = storage.now_iso()
    return conn.execute(
        f"UPDATE electricity_alerts SET status = 'RESOLVED', resolved_by = ?, resolved_at = ?, resolution_note = ? "
        f"WHERE status <> 'RESOLVED' AND {where_sql}", (user, now, note, *params)).rowcount


def acknowledge(conn, alert_id, user):
    return conn.execute("UPDATE electricity_alerts SET status = 'ACKNOWLEDGED', acknowledged_by = ?, acknowledged_at = ? "
                        "WHERE id = ? AND status = 'OPEN'", (user, storage.now_iso(), alert_id)).rowcount


def resolve(conn, alert_id, user, note):
    return resolve_where(conn, "id = ?", (alert_id,), note or "resolved by a person", user)


def _reconcile(conn, desired_keys, where_sql, params, types):
    """Auto-resolve open alerts of `types` that are no longer true."""
    marks = ",".join("?" * len(types))
    rows = conn.execute(f"SELECT id, dedupe_key FROM electricity_alerts WHERE status <> 'RESOLVED' AND alert_type IN ({marks}) AND {where_sql}",
                        (*types, *params)).fetchall()
    for r in rows:
        if r["dedupe_key"] not in desired_keys:
            resolve_where(conn, "id = ?", (r["id"],), "the condition is no longer present (bill re-read or corrected)")


# --------------------------------------------------------------------------- bill-level rules
def _label(b):
    return f"{b['plant_code'] or '?'} · {b['billing_month'] or 'period unknown'}"


def evaluate_bill(conn, bill_id, cfg=None):
    b = conn.execute("SELECT b.*, e.issuer_type, e.discom_code AS e_discom FROM electricity_bills b JOIN electricity_bill_extractions e "
                     "ON e.id = b.extraction_id WHERE b.id = ?", (bill_id,)).fetchone()
    if b is None:
        return []
    cfg = cfg or settings.get_all(conn)
    pf = conn.execute("SELECT * FROM electricity_pf_analysis WHERE bill_id = ?", (bill_id,)).fetchone()
    dem = conn.execute("SELECT * FROM electricity_demand_analysis WHERE bill_id = ?", (bill_id,)).fetchone()
    unverified = b["quality"] != "VERIFIED"
    tag = " (unverified machine reading)" if unverified else ""
    want = []
    ctx = {"bill_id": bill_id, "extraction_id": b["extraction_id"], "account_id": b["account_id"], "plant_code": b["plant_code"]}

    def add(type_, sev, title, msg, action, key_suffix="", **detail):
        want.append(dict(type_=type_, severity=sev, dedupe_key=f"{type_}:bill:{bill_id}{key_suffix}", title=title, message=msg, action=action,
                         detail={"billing_month": b["billing_month"], "bill_number": b["bill_number"], "po_number": b["po_number"],
                                 "discom": b["discom_code"], **detail}, **ctx))

    discom_bill = (b["issuer_type"] or "DISCOM") == "DISCOM"
    if discom_bill and pf is not None:
        if pf["validation_status"] == "NO_PF":
            add("MISSING_PF", "WARNING", f"No power factor found - {_label(b)}",
                "The bill prints no PF and kWh/kVAh are not both available, so PF cannot be calculated.",
                "Open the bill, enter the PF (and unit readings) or confirm that the bill has none.")
        if b["kwh"] is None and b["kvah"] is None:
            add("MISSING_ENERGY", "WARNING", f"No kWh or kVAh found - {_label(b)}", "Neither kWh nor kVAh could be read.", "Open the bill and enter the consumption.")
        elif (b["billing_basis"] == "kVAh" and b["kvah"] is None) or (b["billing_basis"] == "kWh" and b["kwh"] is None):
            need = "kVAh" if b["billing_basis"] == "kVAh" else "kWh"
            add("MISSING_ENERGY", "WARNING", f"{need} consumption missing - {_label(b)}",
                f"The bill is billed on {need} but the {need} figure was not extracted.", f"Open the bill and enter the {need} reading.")
        if pf["validation_status"] in ("MISMATCH", "INVALID_ENERGY"):
            add("PF_INCONSISTENT", "WARNING", f"Billed PF disagrees with kWh/kVAh - {_label(b)}",
                "; ".join(json.loads(pf["notes_json"] or "[]")[:2]) or "The PF printed on the bill and kWh/kVAh disagree.",
                "Check kWh, kVAh and PF against the original bill; correct the misread value.",
                pf_billed=pf["pf_billed"], pf_calculated=pf["pf_calculated"], kwh=b["kwh"], kvah=b["kvah"])
        if pf["performance_status"] in ("ATTENTION", "CRITICAL") and pf["pf_used"] is not None and pf["validation_status"] != "INVALID_ENERGY":
            crit = pf["performance_status"] == "CRITICAL"
            sev = "CRITICAL" if crit and not unverified else "WARNING" if crit or not unverified else "INFO"
            add("PF_BELOW_THRESHOLD", sev, f"PF {pf['pf_used']:.2f} is {'critically low' if crit else 'below target'}{tag} - {_label(b)}",
                f"Power factor {pf['pf_used']:.4f} ({pf['pf_source']}) is {pf['performance_status'].lower()} against the management bands "
                f"(good >= {cfg['pf_good']}, critical < {cfg['pf_attention']}). These are internal monitoring bands, not a tariff penalty threshold.",
                "Review the capacitor bank / APFC panel of this plant." + (" Verify the PF reading on the original bill first." if unverified else ""),
                pf=pf["pf_used"])
    if (b["pf_penalty"] or 0) > 1e-9:
        add("PF_PENALTY_ACTUAL", "CRITICAL", f"PF penalty of Rs {b['pf_penalty']:,.2f} billed{tag} - {_label(b)}",
            f"The electricity board billed a power-factor penalty of Rs {b['pf_penalty']:,.2f} on this bill (actual amount printed on the bill).",
            "Verify the amount on the original bill and find the cause of the low PF.", pf_penalty=b["pf_penalty"], pf=b["pf_billed"])
    if dem is not None:
        if dem["violation_status"] == "VIOLATION":
            add("CONTRACT_DEMAND_EXCEEDED", "CRITICAL", f"Contract demand exceeded beyond the permitted limit{tag} - {_label(b)}",
                f"Maximum demand {dem['max_demand']:g} {dem['max_demand_unit'] or ''} vs contract demand {dem['contract_demand']:g} {dem['contract_demand_unit'] or ''} "
                f"({dem['utilization_pct']}% utilisation; permitted tolerance {dem['tolerance_pct']}% under tariff rule #{dem['tariff_rule_id']}).",
                "Review the contract demand with the board or reduce the peak demand.", max_demand=dem["max_demand"], contract_demand=dem["contract_demand"])
        elif dem["violation_status"] in ("UNVERIFIED_NO_RULE", "UNVERIFIED_RULE_HAS_NO_TOLERANCE"):
            add("CONTRACT_DEMAND_EXCEEDED", "INFO", f"Recorded demand above contract demand (not a confirmed violation){tag} - {_label(b)}",
                f"Maximum demand {dem['max_demand']:g} {dem['max_demand_unit'] or ''} is above the contract demand {dem['contract_demand']:g} "
                f"{dem['contract_demand_unit'] or ''}. No approved tariff rule defines the permitted limit, so this is informational.",
                "Add the approved tariff rule for this DISCOM, or check the bill for an excess-demand charge.",
                max_demand=dem["max_demand"], contract_demand=dem["contract_demand"])
    if (b["excess_demand_penalty"] or 0) > 1e-9:
        add("EXCESS_DEMAND_PENALTY_ACTUAL", "CRITICAL", f"Excess-demand penalty of Rs {b['excess_demand_penalty']:,.2f} billed{tag} - {_label(b)}",
            f"The electricity board billed an excess-demand penalty of Rs {b['excess_demand_penalty']:,.2f} (actual amount on the bill).",
            "Verify the amount on the original bill and review the contract demand.", excess_demand_penalty=b["excess_demand_penalty"],
            max_demand=b["max_demand"], contract_demand=b["contract_demand"])
    if ",DEMAND_IMPLAUSIBLE," in (b["data_flags"] or ""):
        add("DEMAND_IMPLAUSIBLE", "WARNING", f"Maximum or contract demand looks misread - {_label(b)}",
            f"Maximum demand {b['max_demand']:g} against contract demand {b['contract_demand']:g} is more than {engine.DEMAND_IMPLAUSIBLE_RATIO:g} times the contract; "
            f"it is shown but left out of utilisation and violation checks.", "Open the bill and correct the maximum demand / contract demand (and their units).")
    if ",COST_CONFLICT," in (b["data_flags"] or ""):
        add("COST_CONFLICT", "WARNING", f"Bill amounts contradict each other - {_label(b)}",
            f"Total bill amount {b['total_bill_amount']} and payable {b['total_payable']} (or the bill's own charge lines) do not agree, so this bill is left out of "
            f"cost totals until a person checks it.", "Open the bill and correct the total bill amount / payable amount.")
    for col, what in (("pf_penalty_status", "power-factor penalty / incentive"), ("excess_demand_status", "excess-demand penalty")):
        if b[col] == "UNREADABLE":
            add("PENALTY_UNREADABLE", "WARNING", f"The {what} line is on the bill but its amount could not be read - {_label(b)}",
                f"A '{what}' line was found, but no number on it can be the amount (it was a PF value, a rate or the bill total). "
                f"The amount is therefore recorded as unreadable, not as zero.", "Open the bill and enter the amount (or 0 if none was charged).", key_suffix=f":{col}")
    if b["revision_count"] and b["revision_count"] > 1 and b["is_primary"]:
        add("REVISED_BILL", "INFO", f"{b['revision_count']} bills on file for the same account and month - {_label(b)}",
            "A revised or supplementary bill (or a second copy) exists; all are kept, and only the latest counts in totals.",
            "Confirm which bill is final.")

    keys = set()
    for w in want:
        raise_alert(conn, w.pop("type_"), w.pop("severity"), w["dedupe_key"], w.pop("title"), w.pop("message"), w.pop("action"),
                    bill_id=w["bill_id"], extraction_id=w["extraction_id"], account_id=w["account_id"], plant_code=w["plant_code"], detail=w["detail"])
        keys.add(w["dedupe_key"])
    _reconcile(conn, keys, "bill_id = ?", (bill_id,), BILL_TYPES)
    return sorted(keys)


# --------------------------------------------------------------------------- extraction-level rules
def evaluate_extraction(conn, extraction_id):
    e = conn.execute("SELECT * FROM electricity_bill_extractions WHERE id = ?", (extraction_id,)).fetchone()
    if e is None:
        return
    plant = None
    po = None
    if e["attachment_id"] is not None:
        r = conn.execute("SELECT p.plant_code, p.po_number FROM electricity_attachments a JOIN electricity_pos p ON p.po_header_id = a.po_header_id "
                         "WHERE a.attached_document_id = ?", (e["attachment_id"],)).fetchone()
        plant, po = (r["plant_code"], r["po_number"]) if r else (None, None)
    else:
        r = conn.execute("SELECT plant_code, po_number FROM electricity_uploads WHERE id = ?", (e["upload_id"],)).fetchone()
        plant, po = (r["plant_code"], r["po_number"]) if r else (None, None)
    keys = set()
    if not e["archived"]:
        if e["bill_status"] in ("FAILED", "UNSUPPORTED"):
            key = f"EXTRACTION_FAILED:ext:{extraction_id}"
            raise_alert(conn, "EXTRACTION_FAILED", "CRITICAL" if e["bill_status"] == "FAILED" else "WARNING", key,
                        f"Bill could not be read ({e['bill_status'].lower()}) - {plant or 'unknown plant'}",
                        (e["error"] or "The document could not be read.")[:300],
                        "Open the exception list: retry the extraction, or enter the values manually / upload a better copy.",
                        extraction_id=extraction_id, plant_code=plant, detail={"po_number": po, "status": e["bill_status"]})
            keys.add(key)
        elif e["bill_status"] == "DUPLICATE":
            key = f"DUPLICATE_BILL:ext:{extraction_id}"
            raise_alert(conn, "DUPLICATE_BILL", "INFO", key, f"Duplicate bill file - {plant or 'unknown plant'}",
                        f"This file is identical to extraction #{e['duplicate_of']} and is not counted again.", "No action needed unless the duplicate is unexpected.",
                        extraction_id=extraction_id, plant_code=plant, detail={"po_number": po, "duplicate_of": e["duplicate_of"]})
            keys.add(key)
    _reconcile(conn, keys, "extraction_id = ?", (extraction_id,), EXT_TYPES)


# --------------------------------------------------------------------------- system-level rules
def evaluate_download_failures(conn, max_retries=5):
    """One alert per attachment whose download keeps failing; resolved once it downloads."""
    keys = set()
    for a in conn.execute("SELECT a.attached_document_id, a.fetch_status, a.fetch_error, a.retry_count, p.plant_code, p.po_number "
                          "FROM electricity_attachments a JOIN electricity_pos p ON p.po_header_id = a.po_header_id "
                          "WHERE a.fetch_status IN ('FAILED','TOO_LARGE')").fetchall():
        key = f"DOWNLOAD_FAILURE:att:{a['attached_document_id']}"
        keys.add(key)
        raise_alert(conn, "DOWNLOAD_FAILURE", "WARNING" if (a["retry_count"] or 0) < max_retries and a["fetch_status"] == "FAILED" else "CRITICAL", key,
                    f"Oracle attachment could not be downloaded - PO {a['po_number']}",
                    (a["fetch_error"] or a["fetch_status"])[:300], "The scanner retries automatically; if it keeps failing check the attachment in Oracle.",
                    plant_code=a["plant_code"], detail={"attachment_id": a["attached_document_id"], "po_number": a["po_number"], "retries": a["retry_count"]})
    _reconcile(conn, keys, "1=1", (), ("DOWNLOAD_FAILURE",))


def evaluate_scheduler(conn, consecutive_failures, last_error, threshold=None):
    threshold = threshold or settings.get(conn, "scheduler_failure_alert_after")
    key = "SCHEDULER_FAILURE:system"
    if consecutive_failures >= threshold:
        raise_alert(conn, "SCHEDULER_FAILURE", "CRITICAL", key, f"Oracle scan has failed {consecutive_failures} times in a row",
                    (last_error or "unknown error")[:300], "Check the Oracle connection and the scheduler log on the Automation page.",
                    detail={"consecutive_failures": consecutive_failures, "last_error": last_error})
    else:
        resolve_where(conn, "dedupe_key = ?", (key,), "the scan succeeded")


def evaluate_missing_bills(conn, today=None):
    """Accounts with an expected billing schedule (set per account, or a global default) whose next bill is overdue."""
    today = today or date.today()
    default_days = settings.get(conn, "missing_bill_default_days")
    grace_default = settings.get(conn, "missing_bill_grace_days")
    keys = set()
    for a in conn.execute("SELECT * FROM electricity_consumer_accounts").fetchall():
        days = a["expected_every_days"] if a["schedule_enabled"] and a["expected_every_days"] else (default_days if default_days else None)
        if not days or (not a["schedule_enabled"] and not default_days):
            continue
        grace = a["grace_days"] if a["grace_days"] is not None else grace_default
        last = conn.execute("SELECT MAX(period_end) FROM electricity_bills WHERE account_id = ? AND is_primary = 1", (a["id"],)).fetchone()[0]
        if not last:
            continue
        due = date.fromisoformat(last) + timedelta(days=days + grace)
        if today > due:
            key = f"MISSING_EXPECTED_BILL:acct:{a['id']}:{last}"
            keys.add(key)
            raise_alert(conn, "MISSING_EXPECTED_BILL", "WARNING", key,
                        f"Expected electricity bill not received - {a['plant_code']} / {a['consumer_number']}",
                        f"The last bill for this account ended {last}; with a {days}-day schedule and {grace} days' grace the next bill was due by {due}.",
                        "Check whether the bill was raised and whether its PO and attachment exist in Oracle, or upload it manually.",
                        account_id=a["id"], plant_code=a["plant_code"], detail={"last_period_end": last, "expected_every_days": days, "grace_days": grace, "due_by": str(due)})
    _reconcile(conn, keys, "1=1", (), ("MISSING_EXPECTED_BILL",))
