"""
settings.py
===========
Administrator-configurable settings (stored in electricity_state under the 'cfg.' prefix).

Every value has a documented default; nothing here is a tariff rule. The PF status bands are the company's INTERNAL monitoring
thresholds (Phase 3, section 3.2), not electricity-board penalty thresholds.
"""

from . import storage

# name: (default, type, description)
DEFAULTS = {
    # --- power factor monitoring (internal management thresholds)
    "pf_excellent":   (0.99, float, "PF at or above this is Excellent"),
    "pf_good":        (0.95, float, "PF at or above this (and below Excellent) is Good"),
    "pf_attention":   (0.90, float, "PF at or above this (and below Good) is Attention; below it is Critical"),
    "target_pf":      (0.99, float, "Target PF used for the theoretical kVAh saving"),
    "pf_tolerance":   (0.02, float, "Billed PF vs kWh/kVAh PF may differ by this much before a review alert is raised"),
    "critical_confidence": (0.95, float, "Critical fields below this confidence need a person to verify them"),
    # --- alerts / email
    "alert_email_enabled":   (False, bool, "Send alert e-mails"),
    "alert_email_to":        ("", str, "Comma-separated recipients for alert e-mails"),
    "alert_email_cc":        ("", str, "Comma-separated CC recipients"),
    "alert_email_min_severity": ("CRITICAL", str, "Lowest severity that is e-mailed (CRITICAL / WARNING / INFO); every alert is always visible in the app"),
    "alert_renotify_hours":  (72, int, "An alert that is still unresolved is re-sent at most this often (0 = never repeat)"),
    "escalation_hours":      (48, int, "An unacknowledged CRITICAL alert is escalated after this many hours (0 = off)"),
    "escalation_to":         ("", str, "Escalation recipients (comma-separated)"),
    "app_base_url":          ("http://127.0.0.1:2002", str, "Base URL used for links in e-mails"),
    "scheduler_failure_alert_after": (3, int, "Raise a scheduler alert after this many consecutive failed scans"),
    "missing_bill_default_days":  (0, int, "Default expected billing interval in days (0 = no default; set per consumer account)"),
    "missing_bill_grace_days":    (10, int, "Grace period after the expected date before a bill is called missing"),
    # --- scheduler
    "scan_interval_minutes": (120, int, "Oracle scan interval (the business requirement is 2 hours)"),
    "reconcile_hour":        (2, int, "Hour of day (0-23) for the daily full reconciliation scan"),
    "download_batch":        (200, int, "Attachments downloaded per cycle"),
    "process_budget_minutes": (90, int, "Maximum minutes spent reading bills in one cycle"),
    "backfill_from":         ("", str, "Only download attachments of POs created on/after this date (YYYY-MM-DD); empty = everything"),
    "scheduler_enabled":     (True, bool, "Automatic scanning on/off"),
}


def _cast(kind, raw):
    if kind is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return kind(raw)


def get(conn, name):
    default, kind, _ = DEFAULTS[name]
    raw = storage.get_state(conn, "cfg." + name)
    if raw is None:
        # critical_confidence was stored without a prefix by Phase 2
        raw = storage.get_state(conn, name) if name == "critical_confidence" else None
    if raw is None:
        return default
    try:
        return _cast(kind, raw)
    except (TypeError, ValueError):
        return default


def get_all(conn) -> dict:
    return {k: get(conn, k) for k in DEFAULTS}


def validate(name, value):
    """Return the cleaned value or raise ValueError."""
    if name not in DEFAULTS:
        raise ValueError(f"unknown setting {name}")
    default, kind, _ = DEFAULTS[name]
    try:
        v = _cast(kind, value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: invalid value {value!r}") from None
    if kind is float and name.startswith(("pf_", "target_pf", "critical")) and not 0 < v <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    if name == "pf_tolerance" and not 0 < v < 0.5:
        raise ValueError("pf_tolerance must be between 0 and 0.5")
    if name in ("scan_interval_minutes",) and v < 1:
        raise ValueError("scan_interval_minutes must be at least 1")
    if name == "reconcile_hour" and not 0 <= v <= 23:
        raise ValueError("reconcile_hour must be 0-23")
    if name == "alert_email_min_severity" and v.upper() not in ("CRITICAL", "WARNING", "INFO"):
        raise ValueError("severity must be CRITICAL, WARNING or INFO")
    if name == "backfill_from" and v:
        from datetime import date
        date.fromisoformat(v)
    if name in ("alert_email_to", "alert_email_cc", "escalation_to"):
        import re
        for a in [x.strip() for x in v.split(",") if x.strip()]:
            if not re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", a):
                raise ValueError(f"'{a}' is not a valid e-mail address")
    if name in ("pf_excellent", "pf_good", "pf_attention"):
        pass                                           # ordering is checked in set_many
    return v.upper() if name == "alert_email_min_severity" else v


def set_many(conn, values: dict) -> dict:
    cleaned = {k: validate(k, v) for k, v in values.items()}
    merged = {**get_all(conn), **cleaned}
    if not (merged["pf_attention"] < merged["pf_good"] < merged["pf_excellent"] <= 1):
        raise ValueError("PF bands must satisfy attention < good < excellent <= 1")
    for k, v in cleaned.items():
        storage.set_state(conn, "cfg." + k, str(v))
    return cleaned


def recipients(text: str) -> list[str]:
    return [a.strip() for a in (text or "").replace(";", ",").split(",") if a.strip()]
