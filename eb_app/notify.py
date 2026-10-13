"""
notify.py
=========
E-mail for alerts (Phase 5.7). Uses the SMTP account that RDC-OPS already has configured (read-only, see ops_settings) and the same
approach as RDC-OPS's email_helper: STARTTLS, login with the stored password, HTML + plain-text parts.

Anti-flood rules
  * One digest e-mail per run, at most MAX_PER_EMAIL alerts; the rest stay unsent and go out in the next run.
  * An alert is e-mailed once. If it is still unresolved and un-acknowledged it is repeated at most every `alert_renotify_hours`.
  * Acknowledged and resolved alerts are never e-mailed.
  * An unacknowledged CRITICAL alert older than `escalation_hours` is escalated ONCE to `escalation_to`.
  * Credentials are never part of a message or a log line.
"""

import html as _html
import json
import logging
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

from . import settings, storage
from .alerts import SEVERITY_RANK
from .ops_settings import SmtpSettings, load_smtp_settings

log = logging.getLogger(__name__)
MAX_PER_EMAIL = 40


def smtp_transport(smtp: SmtpSettings, msg: EmailMessage, recipients: list[str]) -> None:
    with smtplib.SMTP(smtp.host, smtp.port, timeout=30) as server:
        if smtp.use_tls:
            server.starttls()
        server.login(smtp.sender, smtp.password)
        server.send_message(msg, to_addrs=recipients)


def send_mail(smtp: SmtpSettings, to: list[str], cc: list[str], subject: str, html: str, text: str, transport=None, attachment=None) -> None:
    if not smtp.is_complete():
        raise RuntimeError("SMTP is not configured (host, sender and password are needed)")
    if not to:
        raise RuntimeError("no recipient")
    msg = EmailMessage()
    msg["From"] = smtp.sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    if attachment:
        name, data = attachment
        msg.add_attachment(data, maintype="application", subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=name)
    (transport or smtp_transport)(smtp, msg, to + cc)


# --------------------------------------------------------------------------- content
def _alert_context(conn, a, base_url) -> dict:
    d = json.loads(a["detail_json"] or "{}")
    ctx = {"plant": a["plant_code"] or "—", "board": d.get("discom") or "—", "month": d.get("billing_month") or "—", "bill_number": d.get("bill_number") or "—",
           "po": d.get("po_number") or "—", "pf": "—", "cd": "—", "md": "—", "penalty": "—", "link": base_url.rstrip("/") + "/alerts"}
    if a["bill_id"]:
        b = conn.execute("SELECT b.*, p.pf_used, p.pf_source FROM electricity_bills b LEFT JOIN electricity_pf_analysis p ON p.bill_id = b.id WHERE b.id = ?",
                         (a["bill_id"],)).fetchone()
        if b:
            u = lambda v, unit: "—" if v is None else f"{v:g} {unit or ''}".strip()
            ctx.update(board=b["discom_code"] or "—", month=b["billing_month"] or "—", bill_number=b["bill_number"] or "—", po=b["po_number"] or "—",
                       pf="—" if b["pf_billed"] is None and b["pf_used"] is None else f"{(b['pf_billed'] if b['pf_billed'] is not None else b['pf_used']):.2f}",
                       cd=u(b["contract_demand"], b["contract_demand_unit"]), md=u(b["max_demand"], b["max_demand_unit"]),
                       link=base_url.rstrip("/") + f"/bill/{b['id']}")
            bits = []
            if b["pf_penalty"]:
                bits.append(f"PF penalty Rs {b['pf_penalty']:,.2f}")
            if b["excess_demand_penalty"]:
                bits.append(f"Excess demand penalty Rs {b['excess_demand_penalty']:,.2f}")
            ctx["penalty"] = "; ".join(bits) or "none billed"
            ctx["unverified"] = b["quality"] != "VERIFIED"
    return ctx


def build_digest(conn, alerts, base_url, escalation=False):
    sev_color = {"CRITICAL": "#c0392b", "WARNING": "#d68910", "INFO": "#2471a3"}
    cards, lines = [], []
    for a in alerts:
        c = _alert_context(conn, a, base_url)
        e = _html.escape
        cards.append(
            f"<table cellpadding='6' cellspacing='0' style='border:1px solid #ccd;border-left:5px solid {sev_color[a['severity']]};margin:0 0 12px;width:100%;font-size:13px'>"
            f"<tr><td colspan='4'><b>{e(a['severity'])}</b> &middot; <b>{e(a['title'])}</b></td></tr>"
            f"<tr><td>Plant</td><td>{e(c['plant'])}</td><td>Electricity board</td><td>{e(c['board'])}</td></tr>"
            f"<tr><td>Billing month</td><td>{e(c['month'])}</td><td>Bill number</td><td>{e(c['bill_number'])}</td></tr>"
            f"<tr><td>PO number</td><td>{e(c['po'])}</td><td>Power factor</td><td>{e(c['pf'])}</td></tr>"
            f"<tr><td>Contract demand</td><td>{e(c['cd'])}</td><td>Maximum demand</td><td>{e(c['md'])}</td></tr>"
            f"<tr><td>Detected penalty</td><td colspan='3'>{e(c['penalty'])}</td></tr>"
            f"<tr><td>Problem</td><td colspan='3'>{e(a['message'] or '')}</td></tr>"
            f"<tr><td>Required action</td><td colspan='3'>{e(a['required_action'] or '')}</td></tr>"
            f"<tr><td colspan='4'><a href='{e(c['link'])}'>Open in RDC Electricity Intelligence</a></td></tr></table>")
        lines.append(f"[{a['severity']}] {a['title']}\n  Plant {c['plant']} | Board {c['board']} | Month {c['month']} | Bill {c['bill_number']} | PO {c['po']}\n"
                     f"  PF {c['pf']} | CD {c['cd']} | MD {c['md']} | Penalty {c['penalty']}\n  Problem: {a['message']}\n  Action: {a['required_action']}\n  {c['link']}\n")
    head = "ESCALATION - unacknowledged critical alerts" if escalation else "Electricity alerts"
    worst = max((SEVERITY_RANK[a["severity"]] for a in alerts), default=0)
    label = {2: "CRITICAL", 1: "WARNING", 0: "INFO"}[worst]
    subject = f"[RDC Electricity] {head}: {len(alerts)} alert(s), highest {label}"
    note = "<p style='font-size:12px;color:#666'>Values come from machine-read bills. Alerts marked unverified have not been checked by a person. " \
           "Estimated savings are never part of an alert.</p>"
    html = f"<div style='font-family:Segoe UI,Arial,sans-serif'><h3>{head}</h3>{''.join(cards)}{note}</div>"
    return subject, html, f"{head}\n\n" + "\n".join(lines)


# --------------------------------------------------------------------------- sending
def _log(conn, alert_ids, kind, recipients, subject, status, error=None):
    now = storage.now_iso()
    for aid in alert_ids or [None]:
        conn.execute("INSERT INTO electricity_notifications (alert_id, kind, recipients, subject, status, error, created_at) VALUES (?,?,?,?,?,?,?)",
                     (aid, kind, ", ".join(recipients), subject, status, error, now))


def send_pending(conn, smtp: SmtpSettings | None = None, transport=None, now: datetime | None = None) -> dict:
    """E-mail the alerts that are due. Never raises: a mail failure is recorded and retried in the next run."""
    now = now or datetime.now()
    cfg = settings.get_all(conn)
    result = {"sent": 0, "escalated": 0, "failed": 0, "skipped": None}
    if not cfg["alert_email_enabled"]:
        result["skipped"] = "alert e-mails are switched off in Settings"
        return result
    to, cc = settings.recipients(cfg["alert_email_to"]), settings.recipients(cfg["alert_email_cc"])
    smtp = smtp or load_smtp_settings()
    if not to:
        result["skipped"] = "no alert recipient is configured"
        return result
    if not smtp.is_complete():
        result["skipped"] = "SMTP is not configured"
        return result
    min_rank = SEVERITY_RANK[cfg["alert_email_min_severity"]]
    renotify = cfg["alert_renotify_hours"]
    cutoff = (now - timedelta(hours=renotify)).isoformat(sep=" ", timespec="seconds") if renotify else None
    rows = [a for a in conn.execute("SELECT * FROM electricity_alerts WHERE status = 'OPEN' ORDER BY first_seen").fetchall()
            if SEVERITY_RANK[a["severity"]] >= min_rank and (a["last_emailed_at"] is None or (cutoff and a["last_emailed_at"] <= cutoff))]
    rows.sort(key=lambda a: (-SEVERITY_RANK[a["severity"]], a["first_seen"]))
    rows = rows[:MAX_PER_EMAIL]
    if rows:
        subject, html, text = build_digest(conn, rows, cfg["app_base_url"])
        try:
            send_mail(smtp, to, cc, subject, html, text, transport)
            ts = now.isoformat(sep=" ", timespec="seconds")
            conn.executemany("UPDATE electricity_alerts SET last_emailed_at = ?, email_count = email_count + 1 WHERE id = ?", [(ts, a["id"]) for a in rows])
            _log(conn, [a["id"] for a in rows], "ALERT_DIGEST", to + cc, subject, "SENT")
            result["sent"] = len(rows)
        except Exception as exc:
            log.warning("Alert e-mail failed: %s", type(exc).__name__)
            _log(conn, [a["id"] for a in rows], "ALERT_DIGEST", to + cc, subject, "FAILED", f"{type(exc).__name__}: {str(exc)[:200]}")
            result["failed"] = len(rows)
    # escalation
    esc_to = settings.recipients(cfg["escalation_to"])
    if cfg["escalation_hours"] and esc_to:
        limit = (now - timedelta(hours=cfg["escalation_hours"])).isoformat(sep=" ", timespec="seconds")
        due = conn.execute("SELECT * FROM electricity_alerts WHERE status = 'OPEN' AND severity = 'CRITICAL' AND escalation_level = 0 AND first_seen <= ? "
                           "ORDER BY first_seen LIMIT ?", (limit, MAX_PER_EMAIL)).fetchall()
        if due:
            subject, html, text = build_digest(conn, due, cfg["app_base_url"], escalation=True)
            try:
                send_mail(smtp, esc_to, [], subject, html, text, transport)
                ts = now.isoformat(sep=" ", timespec="seconds")
                conn.executemany("UPDATE electricity_alerts SET escalation_level = 1, escalated_at = ? WHERE id = ?", [(ts, a["id"]) for a in due])
                _log(conn, [a["id"] for a in due], "ESCALATION", esc_to, subject, "SENT")
                result["escalated"] = len(due)
            except Exception as exc:
                _log(conn, [a["id"] for a in due], "ESCALATION", esc_to, subject, "FAILED", f"{type(exc).__name__}: {str(exc)[:200]}")
                result["failed"] += len(due)
    return result
