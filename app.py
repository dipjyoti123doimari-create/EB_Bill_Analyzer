"""
RDC Electricity Intelligence — web application.

Run:   python app.py        -> http://127.0.0.1:2002   (EB_HOST / EB_PORT change the address)
Same look and feel as RDC-OPS (copied stylesheet, sidebar, topbar, KPI cards, multi-selects, themes) and the same logins
(read-only, see eb_app/auth.py). The 2-hourly Oracle scan and the bill-reading queue run in background threads of this
process (EB_SCHEDULER=0 turns them off, e.g. when `python -m eb_app.worker` is used instead).
"""

import io
import json
import logging
import os
import socket
from datetime import datetime
from urllib.parse import urlencode

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for)

from eb_app import (alerts, analytics, audit, auth, bills, config, identification, jobs, notify, reports, scheduler, settings, storage, uploads, viewer)
from eb_app.analysis import tariff
from eb_app.ops_settings import load_oracle_settings, load_smtp_settings
from eb_app.reader import store
from eb_app.reader.fields import FIELDS

log = logging.getLogger("eb.web")
_THEMES = {"Pre-dawn", "Sunrise", "Daytime", "Dusk", "Sunset", "Night"}
BILL_STATUS_BADGE = {"VERIFIED": "badge-green", "NEEDS_REVIEW": "badge-amber", "PARTIAL": "badge-amber", "FAILED": "badge-red", "UNSUPPORTED": "badge-red",
                     "NOT_A_BILL": "badge-muted", "DUPLICATE": "badge-muted"}
PF_BADGE = {"EXCELLENT": "badge-green", "GOOD": "badge-green", "ATTENTION": "badge-amber", "CRITICAL": "badge-red", "UNKNOWN": "badge-muted"}
SEV_BADGE = {"CRITICAL": "badge-red", "WARNING": "badge-amber", "INFO": "badge-purple"}

# editable fields on the bill page: (field name, label, kind, unit choices)
CORRECTABLE = [
    ("consumer_number", "Consumer number", "text", None), ("meter_number", "Meter number", "text", None), ("bill_number", "Bill number", "text", None),
    ("bill_date", "Bill date", "date", None), ("due_date", "Due date", "date", None), ("billing_period", "Billing period (two dates)", "daterange", None),
    ("kwh", "kWh consumption", "number", None), ("kvah", "kVAh consumption", "number", None), ("pf_billed", "Power factor as per bill", "number", None),
    ("sanctioned_load", "Sanctioned load", "number", ("kW", "kVA", "HP")), ("contract_demand", "Contract demand", "number", ("kVA", "kW", "HP", "MVA")),
    ("max_demand", "Maximum recorded demand", "number", ("kVA", "kW", "HP", "MVA")), ("billing_demand", "Billing demand", "number", ("kVA", "kW", "HP", "MVA")),
    ("charge_energy_charges", "Energy charges", "number", None), ("charge_fixed_charges", "Fixed charges", "number", None),
    ("charge_demand_charges", "Demand charges", "number", None), ("charge_fuel_adjustment", "Fuel adjustment charges", "number", None),
    ("charge_electricity_duty", "Electricity duty", "number", None), ("charge_wheeling", "Wheeling charges", "number", None),
    ("charge_taxes_surcharges", "Taxes & surcharges", "number", None), ("charge_arrears", "Arrears / previous balance", "number", None),
    ("charge_adjustments_credits", "Adjustments / credits", "number", None), ("charge_late_payment", "Late payment charges", "number", None),
    ("pf_penalty", "PF penalty (amount on the bill)", "number", None), ("pf_incentive", "PF incentive (amount on the bill)", "number", None),
    ("charge_excess_demand_penalty", "Excess-demand penalty (amount on the bill)", "number", None),
    ("total_bill_amount", "Total bill amount", "number", None), ("total_payable", "Total payable", "number", None),
]
CORRECTABLE_NAMES = {c[0] for c in CORRECTABLE}


def _inr(v, digits=2):
    if v is None or v == "":
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    neg = v < 0
    s = f"{abs(v):.{digits}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if neg else "") + whole + (("." + frac) if digits else "")


def create_app(db_path=None, start_background: bool | None = None, source=None, testing: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["EB_DB"] = db_path
    app.config["TESTING"] = testing
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    storage.init_db(db_path)
    auth.init_app(app)
    sched = None
    mode = os.environ.get("EB_SCHEDULER", "1")             # 1 = scanner + reading queue, jobs = reading queue only (uploads / retries), 0 = neither
    if start_background is None:
        start_background = mode != "0" and not testing
    if start_background and mode == "jobs":
        jobs.Worker(db_path=db_path).start()
    elif start_background:
        from eb_app.oracle_source import OracleSource
        sched = scheduler.Scheduler(source or OracleSource(), db_path=db_path)
        sched.start()
    app.extensions["eb_scheduler"] = sched

    def S():
        return storage.session(app.config["EB_DB"])

    # ------------------------------------------------------------------ template plumbing
    @app.template_filter("inr")
    def f_inr(v, digits=2):
        return _inr(v, digits)

    @app.template_filter("num")
    def f_num(v, digits=0):
        if v is None:
            return "—"
        return _inr(v, digits)

    @app.template_filter("pf")
    def f_pf(v):
        return "—" if v is None else f"{float(v):.3f}"

    @app.template_filter("pct")
    def f_pct(v):
        return "—" if v is None else f"{float(v):.1f}%"

    @app.template_filter("fromjson")
    def f_fromjson(v):
        try:
            return json.loads(v) if v else []
        except ValueError:
            return []

    @app.context_processor
    def _inject():
        with S() as conn:
            theme = storage.get_state(conn, "bg_theme", "Night")
            ctx = {
                "bg_theme": theme if theme in _THEMES else "Night",
                "bg_auto": storage.get_state(conn, "bg_auto", "true") == "true",
                "bg_animate": storage.get_state(conn, "bg_animate", "true") == "true",
            }
            user = getattr(g, "user", None)
            ctx["user"] = user
            ctx["csrf_token"] = auth.csrf_token() if "uid" in session else ""
            ctx["is_admin"] = bool(user and auth.is_admin(user))
            ctx["can_upload"] = bool(user and auth.can_upload(user))
            ctx["open_alerts"] = 0
            ctx["BILL_BADGE"], ctx["PF_BADGE"], ctx["SEV_BADGE"] = BILL_STATUS_BADGE, PF_BADGE, SEV_BADGE
            if user:
                ap = auth.allowed_plants(user)
                sql = "SELECT COUNT(*) FROM electricity_alerts WHERE status = 'OPEN'"
                p = ()
                if ap is not None:
                    sql += f" AND plant_code IN ({','.join('?' * len(ap))})" if ap else " AND 1 = 0"
                    p = tuple(ap)
                ctx["open_alerts"] = conn.execute(sql, p).fetchone()[0]
        return ctx

    @app.template_global()
    def qs(**over):
        """Current query string with some parameters replaced (None removes one)."""
        pairs = [(k, v) for k in request.args for v in request.args.getlist(k) if k not in over]
        for k, v in over.items():
            if v is None:
                continue
            for item in (v if isinstance(v, (list, tuple)) else [v]):
                pairs.append((k, item))
        return urlencode(pairs)

    def scope():
        """Plants the logged-in user may see (None = all)."""
        return auth.allowed_plants(g.user)

    def filters_from_request(conn):
        return analytics.Filters.from_args(request.args, allowed_plants=scope(), conn=conn)

    def log_audit(action, entity_type=None, entity_id=None, details=None):
        with S() as c:
            audit.log(c, g.user["username"], action, entity_type, entity_id, details, role=g.user["role"], ip=request.remote_addr)

    @app.errorhandler(403)
    def _403(e):
        return render_template("message.html", title="Not allowed", message="Your role does not allow this action or this plant.", active_page=""), 403

    @app.errorhandler(404)
    def _404(e):
        return render_template("message.html", title="Not found", message="This page or record does not exist.", active_page=""), 404

    @app.errorhandler(400)
    def _400(e):
        return render_template("message.html", title="Request refused", message=getattr(e, "description", "Bad request"), active_page=""), 400

    @app.errorhandler(413)
    def _413(e):
        return render_template("message.html", title="File too large", message=f"The file is larger than the {config.MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit.", active_page=""), 413

    # ------------------------------------------------------------------ login
    @app.route("/login", methods=["GET", "POST"])
    def page_login():
        if request.method == "POST":
            u, why = auth.authenticate(request.form.get("username", ""), request.form.get("password", ""), request.remote_addr or "")
            with S() as c:
                audit.log(c, request.form.get("username", "")[:60], "LOGIN_OK" if u else "LOGIN_FAILED", "user", u["id"] if u else None,
                          {"reason": why} if why else None, role=u["role"] if u else None, ip=request.remote_addr)
            if u:
                auth.login_session(u)
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("page_dashboard"))
            return render_template("login.html", error=why, bg_theme="Night", bg_auto=False, bg_animate=False), 401
        return render_template("login.html", error=None, bg_theme="Night", bg_auto=False, bg_animate=False)

    @app.route("/logout", methods=["POST"])
    @auth.login_required
    def page_logout():
        log_audit("LOGOUT", "user", g.user["id"])
        session.clear()
        return redirect(url_for("page_login"))

    # ------------------------------------------------------------------ dashboard
    @app.route("/")
    @auth.login_required
    def page_dashboard():
        with S() as conn:
            f = filters_from_request(conn)
            period = request.args.get("period") or ("last6" if not (f.month_from or f.month_to) else "")
            if period in ("current", "previous", "last3", "last6", "last12") and not request.args.get("month_from"):
                f.month_from, f.month_to, plabel = analytics.resolve_period(conn, period)
            else:
                plabel = f"{f.month_from or 'start'} to {f.month_to or 'latest'}" if (f.month_from or f.month_to) else "All periods"
            data = analytics.management(conn, f)
            opts = analytics.filter_options(conn, scope())
            recent = [dict(r) for r in conn.execute(
                "SELECT * FROM electricity_alerts WHERE status = 'OPEN' ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END, last_seen DESC LIMIT 8")]
            ap = scope()
            if ap is not None:
                recent = [a for a in recent if (a["plant_code"] or "").upper() in ap]
        return render_template("dashboard.html", active_page="dashboard", d=data, k=data["kpis"], f=f, opts=opts, period=period, plabel=plabel, recent=recent)

    @app.route("/api/dashboard")
    @auth.login_required
    def api_dashboard():
        with S() as conn:
            f = filters_from_request(conn)
            m = analytics.management(conn, f)
        return jsonify(monthly=m["monthly"], states=m["states"], boards=m["boards"], kpis=m["kpis"])

    # ------------------------------------------------------------------ plant-wise monitoring
    @app.route("/monitor")
    @auth.login_required
    def page_monitor():
        with S() as conn:
            f = filters_from_request(conn)
            t = analytics.bill_table(conn, f, sort=request.args.get("sort", "period"), direction=request.args.get("dir", "desc"),
                                     page=int(request.args.get("page", 1) or 1), per_page=int(request.args.get("per", 50) or 50))
            opts = analytics.filter_options(conn, scope())
            k = analytics.kpis(conn, f)
        return render_template("monitor.html", active_page="monitor", t=t, f=f, opts=opts, k=k, sort=request.args.get("sort", "period"),
                               direction=request.args.get("dir", "desc"))

    @app.route("/monitor/export")
    @auth.login_required
    def export_monitor():
        return _send_report("monitor")

    @app.route("/plant/<code>")
    @auth.login_required
    def page_plant(code):
        code = code.upper()
        if not auth.can_see_plant(g.user, code):
            abort(403)
        with S() as conn:
            f = filters_from_request(conn)
            d = analytics.plant_detail(conn, code, f)
            cfg = settings.get_all(conn)
            if not d["info"] and not d["plant_name"]:
                abort(404)
            alerts_ = [dict(r) for r in conn.execute("SELECT * FROM electricity_alerts WHERE plant_code = ? AND status <> 'RESOLVED' ORDER BY last_seen DESC LIMIT 15", (code,))]
        return render_template("plant.html", active_page="monitor", code=code, d=d, f=f, cfg=cfg, alerts=alerts_)

    # ------------------------------------------------------------------ bill viewer
    def _extraction_or_404(conn, ext_id):
        e = conn.execute("SELECT e.*, COALESCE(p.plant_code, u.plant_code) AS plant_code FROM electricity_bill_extractions e "
                         "LEFT JOIN electricity_attachments a ON a.attached_document_id = e.attachment_id "
                         "LEFT JOIN electricity_pos p ON p.po_header_id = a.po_header_id LEFT JOIN electricity_uploads u ON u.id = e.upload_id WHERE e.id = ?", (ext_id,)).fetchone()
        if e is None:
            abort(404)
        if not auth.can_see_plant(g.user, e["plant_code"] or ""):
            abort(403)
        return e

    def _can_correct(e, conn):
        if auth.is_admin(g.user):
            return True
        if e["upload_id"] is not None:                    # a plant user may fix the bills they uploaded themselves
            u = conn.execute("SELECT uploaded_by FROM electricity_uploads WHERE id = ?", (e["upload_id"],)).fetchone()
            return bool(u and u["uploaded_by"] == g.user["username"])
        return False

    @app.route("/bill/<int:bill_id>")
    @auth.login_required
    def page_bill_redirect(bill_id):
        with S() as conn:
            r = conn.execute("SELECT extraction_id FROM electricity_bills WHERE id = ?", (bill_id,)).fetchone()
        if r is None:
            abort(404)
        return redirect(url_for("page_extraction", ext_id=r["extraction_id"]))

    @app.route("/e/<int:ext_id>")
    @auth.login_required
    def page_extraction(ext_id):
        with S() as conn:
            e = _extraction_or_404(conn, ext_id)
            fields_ = {r["field_name"]: dict(r) for r in conn.execute("SELECT * FROM electricity_bill_fields WHERE extraction_id = ?", (ext_id,))}
            bill = conn.execute("SELECT * FROM electricity_bills WHERE extraction_id = ?", (ext_id,)).fetchone()
            pf = dem = None
            if bill:
                pf = conn.execute("SELECT * FROM electricity_pf_analysis WHERE bill_id = ?", (bill["id"],)).fetchone()
                dem = conn.execute("SELECT * FROM electricity_demand_analysis WHERE bill_id = ?", (bill["id"],)).fetchone()
            charge_lines = [dict(r) for r in conn.execute("SELECT * FROM electricity_charge_lines WHERE extraction_id = ? ORDER BY page, id", (ext_id,))]
            corrections = [dict(r) for r in conn.execute("SELECT * FROM electricity_manual_corrections WHERE extraction_id = ? ORDER BY id DESC", (ext_id,))]
            audits = [dict(r) for r in conn.execute("SELECT * FROM electricity_audit_logs WHERE entity_type = 'extraction' AND entity_id = ? ORDER BY id DESC LIMIT 50", (str(ext_id),))]
            others = [dict(r) for r in conn.execute(
                "SELECT e2.id, e2.bill_status, e2.created_at, e2.archived, e2.reader_version FROM electricity_bill_extractions e2 WHERE "
                "(e2.attachment_id IS NOT NULL AND e2.attachment_id = ?) OR (e2.upload_id IS NOT NULL AND e2.upload_id = ?) ORDER BY e2.id DESC",
                (e["attachment_id"], e["upload_id"]))]
            po = None
            if e["attachment_id"]:
                po = conn.execute("SELECT p.* FROM electricity_pos p JOIN electricity_attachments a ON a.po_header_id = p.po_header_id WHERE a.attached_document_id = ?",
                                  (e["attachment_id"],)).fetchone()
            same_period = []
            if bill and bill["group_key"]:
                same_period = [dict(r) for r in conn.execute("SELECT id, extraction_id, bill_number, bill_date, is_primary, bill_status FROM electricity_bills WHERE group_key = ? ORDER BY id", (bill["group_key"],))]
            result = json.loads(e["result_json"] or "{}")
            can_correct = _can_correct(e, conn)
            orig = viewer.original_path(conn, ext_id)
        pages = e["pages"] or 0
        return render_template("bill.html", active_page="review" if e["bill_status"] not in ("VERIFIED",) else "monitor", e=dict(e), fields=fields_, bill=dict(bill) if bill else None,
                               pf=dict(pf) if pf else None, dem=dict(dem) if dem else None, charge_lines=charge_lines, corrections=corrections, audits=audits,
                               others=others, po=dict(po) if po else None, same_period=same_period, flags=json.loads(e["flags_json"] or "[]"),
                               warnings=json.loads(e["warnings_json"] or "[]"), missing=json.loads(e["missing_json"] or "[]"), basis_ev=(result.get("billing_basis") or {}),
                               po_check=json.loads(e["po_check_json"] or "{}"), CORRECTABLE=CORRECTABLE, can_correct=can_correct, has_original=bool(orig), pages=pages,
                               FIELDS=FIELDS, doc_name=orig[1] if orig else None)

    @app.route("/e/<int:ext_id>/file")
    @auth.login_required
    def file_original(ext_id):
        with S() as conn:
            _extraction_or_404(conn, ext_id)
            o = viewer.original_path(conn, ext_id)
        if not o:
            abort(404)
        path, name = o
        data, mime = viewer.original_bytes(path)
        resp = send_file(io.BytesIO(data), mimetype=mime, as_attachment=False, download_name=os.path.basename(name or path.name))
        if mime != "application/pdf":                      # (browsers refuse to show a PDF in a CSP-sandboxed frame; PDFs rely on nosniff + the viewer's own sandbox)
            resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
        return resp

    @app.route("/e/<int:ext_id>/page/<int:n>.png")
    @auth.login_required
    def page_image(ext_id, n):
        field = request.args.get("field", "")
        with S() as conn:
            _extraction_or_404(conn, ext_id)
            o = viewer.original_path(conn, ext_id)
            fr = conn.execute("SELECT page, bbox_json FROM electricity_bill_fields WHERE extraction_id = ? AND field_name = ?", (ext_id, field)).fetchone() if field else None
        if not o:
            abort(404)
        bbox = json.loads(fr["bbox_json"]) if fr and fr["bbox_json"] and fr["page"] == n else None
        try:
            png, note = viewer.render(o[0], n, bbox)
        except (ValueError, OSError):
            abort(404)
        resp = send_file(io.BytesIO(png), mimetype="image/png")
        if note:
            resp.headers["X-Evidence-Note"] = note
        return resp

    @app.route("/e/<int:ext_id>/correct", methods=["POST"])
    @auth.login_required
    def act_correct(ext_id):
        with S() as conn:
            e = _extraction_or_404(conn, ext_id)
            if not _can_correct(e, conn):
                abort(403)
        field = request.form.get("field", "")
        if field not in CORRECTABLE_NAMES:
            return jsonify(error="That field cannot be corrected here."), 400
        value, unit, reason = request.form.get("value", ""), request.form.get("unit") or None, (request.form.get("reason") or "")[:300]
        try:
            r = store.apply_correction(ext_id, field, value, g.user["username"], unit=unit, reason=reason, db_path=app.config["EB_DB"])
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True, **{k: (v if not isinstance(v, (list, tuple)) else list(v)) for k, v in r.items()})

    @app.route("/e/<int:ext_id>/verify", methods=["POST"])
    @auth.login_required
    def act_verify(ext_id):
        with S() as conn:
            e = _extraction_or_404(conn, ext_id)
            if not _can_correct(e, conn):
                abort(403)
        fields_ = request.form.getlist("field")
        for fld in fields_:
            store.mark_verified(ext_id, fld, g.user["username"], db_path=app.config["EB_DB"])
        return jsonify(ok=True, verified=len(fields_))

    @app.route("/e/<int:ext_id>/basis", methods=["POST"])
    @auth.login_required
    def act_basis(ext_id):
        with S() as conn:
            e = _extraction_or_404(conn, ext_id)
            if not _can_correct(e, conn):
                abort(403)
        try:
            store.set_billing_basis(ext_id, request.form.get("basis", ""), g.user["username"], request.form.get("reason"), db_path=app.config["EB_DB"])
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True)

    @app.route("/e/<int:ext_id>/retry", methods=["POST"])
    @auth.login_required
    def act_retry(ext_id):
        with S() as conn:
            e = _extraction_or_404(conn, ext_id)
            if not _can_correct(e, conn):
                abort(403)
            jobs.enqueue(conn, "RETRY_EXTRACTION", ext_id)
            audit.log(conn, g.user["username"], "QUEUE_RETRY", "extraction", ext_id, role=g.user["role"], ip=request.remote_addr)
        return jsonify(ok=True, message="Queued: the bill will be read again in the background; the old reading is kept in the history.")

    @app.route("/e/<int:ext_id>/resolve", methods=["POST"])
    @auth.login_required
    def act_resolve(ext_id):
        """'Mark resolved' for an exception that needs no more work (e.g. not a bill, accepted as is). Never turns a bad bill into a verified one."""
        with S() as conn:
            e = _extraction_or_404(conn, ext_id)
            if not auth.is_admin(g.user):
                abort(403)
            note = (request.form.get("note") or "").strip()
            if not note:
                return jsonify(error="Write a short note saying why this is resolved."), 400
            if e["bill_status"] in ("NEEDS_REVIEW", "PARTIAL"):
                return jsonify(error="A bill that still needs review is resolved by verifying or correcting its fields, not by closing it."), 400
            conn.execute("UPDATE electricity_bill_extractions SET resolved_by = ?, resolved_at = ?, resolution_note = ? WHERE id = ?",
                         (g.user["username"], storage.now_iso(), note[:400], ext_id))
            alerts.resolve_where(conn, "extraction_id = ?", (ext_id,), note[:200], g.user["username"])
            audit.log(conn, g.user["username"], "RESOLVE_EXCEPTION", "extraction", ext_id, {"note": note}, role=g.user["role"], ip=request.remote_addr)
        return jsonify(ok=True)

    # ------------------------------------------------------------------ exception / review dashboard
    @app.route("/review")
    @auth.login_required
    def page_review():
        status = request.args.get("status", "ACTIVE")
        page = max(1, int(request.args.get("page", 1) or 1))
        per = 40
        ap = scope()
        where, params = ["e.archived = 0"], []
        if status == "ACTIVE":
            where.append("e.bill_status IN ('NEEDS_REVIEW','PARTIAL','FAILED','UNSUPPORTED') AND e.resolved_at IS NULL")
        elif status == "ALL":
            pass
        else:
            where.append("e.bill_status = ?")
            params.append(status)
        if ap is not None:
            where.append(f"COALESCE(p.plant_code, u.plant_code) IN ({','.join('?' * len(ap))})" if ap else "1 = 0")
            params += list(ap)
        q = (request.args.get("q") or "").strip()
        if q:
            where.append("(COALESCE(p.plant_code, u.plant_code) LIKE ? OR COALESCE(p.po_number, u.po_number) LIKE ? OR e.discom_code LIKE ?)")
            params += [f"%{q}%"] * 3
        base = ("FROM electricity_bill_extractions e LEFT JOIN electricity_attachments a ON a.attached_document_id = e.attachment_id "
                "LEFT JOIN electricity_pos p ON p.po_header_id = a.po_header_id LEFT JOIN electricity_uploads u ON u.id = e.upload_id WHERE " + " AND ".join(where))
        with S() as conn:
            total = conn.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
            rows = [dict(r) for r in conn.execute(
                "SELECT e.id, e.bill_status, e.discom_code, e.state, e.doc_kind, e.pages, e.ocr_pages, e.created_at, e.missing_json, e.flags_json, e.warnings_json, e.error, "
                "e.resolved_at, COALESCE(p.plant_code, u.plant_code) AS plant, COALESCE(p.po_number, u.po_number) AS po, "
                "CASE WHEN e.attachment_id IS NOT NULL THEN 'ORACLE_EBS' ELSE 'MANUAL_UPLOAD' END AS source, "
                "(SELECT GROUP_CONCAT(field_name, ', ') FROM electricity_bill_fields f WHERE f.extraction_id = e.id AND f.verification_status = 'NEEDS_REVIEW') AS low_conf "
                + base + " ORDER BY CASE e.bill_status WHEN 'FAILED' THEN 0 WHEN 'UNSUPPORTED' THEN 1 WHEN 'PARTIAL' THEN 2 WHEN 'NEEDS_REVIEW' THEN 3 ELSE 4 END, e.id DESC LIMIT ? OFFSET ?",
                params + [per, (page - 1) * per])]
            sql_counts = ("SELECT e.bill_status, COUNT(*) FROM electricity_bill_extractions e LEFT JOIN electricity_attachments a ON a.attached_document_id = e.attachment_id "
                          "LEFT JOIN electricity_pos p ON p.po_header_id = a.po_header_id LEFT JOIN electricity_uploads u ON u.id = e.upload_id WHERE e.archived = 0 ")
            cp = []
            if ap is not None:
                sql_counts += f"AND COALESCE(p.plant_code, u.plant_code) IN ({','.join('?' * len(ap))}) " if ap else "AND 1 = 0 "
                cp = list(ap)
            counts = dict(conn.execute(sql_counts + "GROUP BY e.bill_status", cp).fetchall())
        return render_template("review.html", active_page="review", rows=rows, total=total, status=status, counts=counts, page=page, pages=max(1, -(-total // per)), q=q)

    # ------------------------------------------------------------------ alerts
    @app.route("/alerts")
    @auth.login_required
    def page_alerts():
        status = request.args.get("status", "OPEN")
        sev = request.args.get("severity", "")
        typ = request.args.get("type", "")
        where, params = [], []
        if status != "ALL":
            where.append("status = ?")
            params.append(status)
        if sev:
            where.append("severity = ?")
            params.append(sev)
        if typ:
            where.append("alert_type = ?")
            params.append(typ)
        ap = scope()
        if ap is not None:
            where.append(f"(plant_code IN ({','.join('?' * len(ap))}))" if ap else "1 = 0")
            params += list(ap)
        with S() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM electricity_alerts " + ("WHERE " + " AND ".join(where) if where else "") +
                " ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END, last_seen DESC LIMIT 300", params)]
            types = [r[0] for r in conn.execute("SELECT DISTINCT alert_type FROM electricity_alerts ORDER BY 1")]
            mail = [dict(r) for r in conn.execute("SELECT * FROM electricity_notifications ORDER BY id DESC LIMIT 12")] if auth.is_admin(g.user) else []
            cfg = settings.get_all(conn)
        return render_template("alerts.html", active_page="alerts", rows=rows, status=status, sev=sev, typ=typ, types=types, mail=mail, cfg=cfg)

    def _alert_ok(conn, alert_id):
        a = conn.execute("SELECT plant_code FROM electricity_alerts WHERE id = ?", (alert_id,)).fetchone()
        if a is None:
            abort(404)
        if not auth.can_see_plant(g.user, a["plant_code"] or "") and (a["plant_code"] or ""):
            abort(403)
        if a["plant_code"] is None and scope() is not None:
            abort(403)

    @app.route("/alerts/<int:alert_id>/ack", methods=["POST"])
    @auth.login_required
    def act_alert_ack(alert_id):
        with S() as conn:
            _alert_ok(conn, alert_id)
            alerts.acknowledge(conn, alert_id, g.user["username"])
            audit.log(conn, g.user["username"], "ALERT_ACK", "alert", alert_id, role=g.user["role"], ip=request.remote_addr)
        return jsonify(ok=True)

    @app.route("/alerts/<int:alert_id>/resolve", methods=["POST"])
    @auth.login_required
    def act_alert_resolve(alert_id):
        with S() as conn:
            _alert_ok(conn, alert_id)
            if not auth.is_admin(g.user):
                abort(403)
            alerts.resolve(conn, alert_id, g.user["username"], (request.form.get("note") or "resolved by a person")[:200])
            audit.log(conn, g.user["username"], "ALERT_RESOLVE", "alert", alert_id, {"note": request.form.get("note")}, role=g.user["role"], ip=request.remote_addr)
        return jsonify(ok=True)

    # ------------------------------------------------------------------ upload
    @app.route("/upload", methods=["GET", "POST"])
    @auth.upload_required
    def page_upload():
        with S() as conn:
            plants_ = uploads.plants(conn)
            ap = scope()
            if ap is not None:
                plants_ = [p for p in plants_ if (p["plant_code"] or "").upper() in ap]
            recent = [dict(r) for r in conn.execute(
                "SELECT u.*, e.bill_status FROM electricity_uploads u LEFT JOIN electricity_bill_extractions e ON e.id = u.extraction_id "
                + ("WHERE u.uploaded_by = ? " if not auth.is_admin(g.user) else "") + "ORDER BY u.id DESC LIMIT 15",
                (g.user["username"],) if not auth.is_admin(g.user) else ())]
        if request.method == "POST":
            f = request.files.get("file")
            plant = request.form.get("plant_code", "").strip()
            if plant and not auth.can_see_plant(g.user, plant):
                abort(403)
            if not plant:
                flash("Choose the plant this bill belongs to.", "error")
            elif f is None or not f.filename:
                flash("Choose a file to upload.", "error")
            else:
                try:
                    with S() as conn:
                        r = uploads.save_upload(conn, f.read(config.MAX_ATTACHMENT_BYTES + 1), f.filename, plant, g.user["username"],
                                                request.form.get("consumer_number", ""), request.form.get("discom_code", ""), request.form.get("po_number", ""),
                                                request.form.get("billing_month", ""))
                    if r["duplicate"]:
                        flash("This exact file was uploaded before; showing the earlier result.", "info")
                    return redirect(url_for("page_upload_status", upload_id=r["upload_id"]))
                except uploads.UploadError as exc:
                    flash(str(exc), "error")
        return render_template("upload.html", active_page="upload", plants=plants_, discoms=uploads.discom_options(), recent=recent, form=request.form)

    def _upload_row(conn, upload_id):
        u = conn.execute("SELECT * FROM electricity_uploads WHERE id = ?", (upload_id,)).fetchone()
        if u is None:
            abort(404)
        if not auth.can_see_plant(g.user, u["plant_code"] or ""):
            abort(403)
        return u

    @app.route("/upload/<int:upload_id>")
    @auth.upload_required
    def page_upload_status(upload_id):
        with S() as conn:
            u = _upload_row(conn, upload_id)
            if u["extraction_id"] and u["status"] == "PROCESSED":
                return redirect(url_for("page_extraction", ext_id=u["extraction_id"]))
        return render_template("upload_status.html", active_page="upload", u=dict(u))

    @app.route("/api/upload/<int:upload_id>")
    @auth.login_required
    def api_upload(upload_id):
        with S() as conn:
            u = _upload_row(conn, upload_id)
        return jsonify(status=u["status"], extraction_id=u["extraction_id"], error=u["error"], link_note=u["link_note"])

    # ------------------------------------------------------------------ reports
    def _send_report(kind):
        with S() as conn:
            f = filters_from_request(conn)
            try:
                name, data = reports.build(conn, kind, f)
            except ValueError:
                abort(404)
            audit.log(conn, g.user["username"], "EXPORT", "report", kind, {"query": request.query_string.decode()[:300]}, role=g.user["role"], ip=request.remote_addr)
        return send_file(io.BytesIO(data), as_attachment=True, download_name=name,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.route("/reports")
    @auth.login_required
    def page_reports():
        with S() as conn:
            opts = analytics.filter_options(conn, scope())
            f = filters_from_request(conn)
        return render_template("reports.html", active_page="reports", kinds=reports.ALL_KINDS, opts=opts, f=f)

    @app.route("/reports/<kind>.xlsx")
    @auth.login_required
    def download_report(kind):
        return _send_report(kind)

    # ------------------------------------------------------------------ admin: automation
    def _automation_context():
        with S() as conn:
            sch = scheduler.get_schedule(conn)
            cfg = settings.get_all(conn)
            runs = [dict(r) for r in conn.execute("SELECT * FROM electricity_scan_runs ORDER BY id DESC LIMIT 15")]
            lock = conn.execute("SELECT * FROM electricity_job_locks WHERE name = ?", (scheduler.LOCK_NAME,)).fetchone()
            one = lambda sql: conn.execute(sql).fetchone()[0]
            att = dict(conn.execute("SELECT fetch_status, COUNT(*) FROM electricity_attachments GROUP BY fetch_status").fetchall())
            ext = dict(conn.execute("SELECT bill_status, COUNT(*) FROM electricity_bill_extractions WHERE archived = 0 GROUP BY bill_status").fetchall())
            jobs_ = dict(conn.execute("SELECT status, COUNT(*) FROM electricity_jobs GROUP BY status").fetchall())
            errors = [dict(r) for r in conn.execute("SELECT * FROM electricity_processing_errors ORDER BY id DESC LIMIT 25")]
            last_ok = conn.execute("SELECT * FROM electricity_scan_runs WHERE status = 'SUCCESS' ORDER BY id DESC LIMIT 1").fetchone()
            totals = {"pos": one("SELECT COUNT(*) FROM electricity_pos WHERE eligible = 1"), "new_attachments_24h": one(
                "SELECT COALESCE(SUM(new_attachments), 0) FROM electricity_scan_runs WHERE started_at >= datetime('now', 'localtime', '-1 day')"),
                "pending_reviews": ext.get("NEEDS_REVIEW", 0) + ext.get("PARTIAL", 0), "failed_bills": ext.get("FAILED", 0) + ext.get("UNSUPPORTED", 0),
                "processed_ok": ext.get("VERIFIED", 0) + ext.get("NEEDS_REVIEW", 0) + ext.get("PARTIAL", 0), "pending_files": att.get("PENDING", 0),
                "failed_downloads": att.get("FAILED", 0) + att.get("TOO_LARGE", 0), "retry_queue": jobs_.get("QUEUED", 0) + jobs_.get("RUNNING", 0)}
        s = app.extensions.get("eb_scheduler")
        health = "OK"
        tick = sch["last_tick_at"]
        if cfg["scheduler_enabled"] is False:
            health = "PAUSED"
        elif tick is None or (datetime.now() - datetime.fromisoformat(tick)).total_seconds() > 300:
            health = "NOT RUNNING"
        elif sch["consecutive_failures"] >= cfg["scheduler_failure_alert_after"]:
            health = "FAILING"
        elif sch["consecutive_failures"]:
            health = "RETRYING"
        return dict(sch=sch, cfg=cfg, runs=runs, lock=dict(lock) if lock else None, att=att, ext=ext, jobs=jobs_, errors=errors, totals=totals, health=health,
                    last_ok=dict(last_ok) if last_ok else None, in_process=bool(s and s.running), smtp=load_smtp_settings().public_dict())

    @app.route("/automation")
    @auth.admin_required
    def page_automation():
        return render_template("automation.html", active_page="automation", **_automation_context())

    @app.route("/api/automation")
    @auth.admin_required
    def api_automation():
        c = _automation_context()
        return jsonify(health=c["health"], totals=c["totals"], schedule=c["sch"], running=c["lock"] is not None)

    @app.route("/automation/scan-now", methods=["POST"])
    @auth.admin_required
    def act_scan_now():
        s = app.extensions.get("eb_scheduler")
        log_audit("SCAN_NOW")
        if s is None:
            # no in-process scheduler (e.g. a separate worker owns it): make the next worker tick run immediately
            with S() as conn:
                storage.set_state(conn, "sched.next_due", "")
            return jsonify(ok=True, message="The background worker will start a scan within a minute.")
        r = s.scan_now()
        return jsonify(ok=r["started"], message="Scan started." if r["started"] else r["reason"])

    @app.route("/automation/retry-failed", methods=["POST"])
    @auth.admin_required
    def act_retry_failed():
        with S() as conn:
            dl = conn.execute("UPDATE electricity_attachments SET retry_count = 0, next_retry_at = NULL WHERE fetch_status IN ('FAILED','TOO_LARGE')").rowcount
            conn.execute("UPDATE electricity_attachments SET fetch_status = 'PENDING' WHERE fetch_status = 'FAILED'")
            ids = [r[0] for r in conn.execute("SELECT id FROM electricity_bill_extractions WHERE archived = 0 AND bill_status = 'FAILED'")]
            for i in ids:
                jobs.enqueue(conn, "RETRY_EXTRACTION", i)
            fj = conn.execute("UPDATE electricity_jobs SET status = 'QUEUED', attempts = 0, next_attempt_at = NULL WHERE status = 'FAILED'").rowcount
            audit.log(conn, g.user["username"], "RETRY_FAILED", None, None, {"downloads": dl, "extractions": len(ids), "jobs": fj}, role=g.user["role"], ip=request.remote_addr)
        return jsonify(ok=True, message=f"Re-queued {dl} failed download(s), {len(ids)} failed bill(s) and {fj} failed job(s).")

    @app.route("/automation/resolve-error/<int:err_id>", methods=["POST"])
    @auth.admin_required
    def act_resolve_error(err_id):
        with S() as conn:
            conn.execute("UPDATE electricity_processing_errors SET resolved_at = ?, resolved_by = ? WHERE id = ?", (storage.now_iso(), g.user["username"], err_id))
        return jsonify(ok=True)

    @app.route("/automation/test-mail", methods=["POST"])
    @auth.admin_required
    def act_test_mail():
        with S() as conn:
            cfg = settings.get_all(conn)
            to = settings.recipients(cfg["alert_email_to"])
            if not to:
                return jsonify(error="Set the alert recipients in Settings first."), 400
            smtp = load_smtp_settings()
            if not smtp.is_complete():
                return jsonify(error="SMTP is not configured (the RDC-OPS mail settings are empty)."), 400
            try:
                notify.send_mail(smtp, to, settings.recipients(cfg["alert_email_cc"]), "[RDC Electricity] Test e-mail",
                                 "<p>This is a test e-mail from RDC Electricity Intelligence. Alert e-mails will come from this address.</p>",
                                 "This is a test e-mail from RDC Electricity Intelligence.")
                notify._log(conn, [None], "TEST", to, "[RDC Electricity] Test e-mail", "SENT")
            except Exception as exc:
                notify._log(conn, [None], "TEST", to, "[RDC Electricity] Test e-mail", "FAILED", f"{type(exc).__name__}")
                return jsonify(error=f"The e-mail could not be sent ({type(exc).__name__})."), 502
            audit.log(conn, g.user["username"], "TEST_MAIL", None, None, {"to": to}, role=g.user["role"], ip=request.remote_addr)
        return jsonify(ok=True, message="Test e-mail sent to " + ", ".join(to))

    # ------------------------------------------------------------------ admin: tariff rules, settings, rules, accounts, audit
    @app.route("/tariffs", methods=["GET", "POST"])
    @auth.admin_required
    def page_tariffs():
        if request.method == "POST":
            data = {k: request.form.get(k) for k in ("state", "discom_code", "consumer_category", "voltage_category", "effective_from", "effective_to",
                                                     "billing_basis_rule", "contract_demand_tolerance_pct", "source_reference", "note")}
            try:
                for key, field_ in (("pf_penalty_rule", "pf_penalty_rule_json"), ("pf_incentive_rule", "pf_incentive_rule_json"),
                                    ("excess_demand_rule", "excess_demand_rule_json"), ("tariff_rates", "tariff_rates_json"), ("demand_charge_rule", "demand_charge_rule_json")):
                    raw = (request.form.get(field_) or "").strip()
                    if raw:
                        try:
                            data[key] = json.loads(raw)
                        except ValueError:
                            raise tariff.RuleError(f"{field_.replace('_json', '').replace('_', ' ')}: not valid JSON") from None
                with S() as conn:
                    rid = tariff.create_rule(conn, data, g.user["username"])
                    audit.log(conn, g.user["username"], "TARIFF_RULE_CREATE", "tariff_rule", rid, {k: v for k, v in data.items() if v}, role=g.user["role"], ip=request.remote_addr)
                flash(f"Draft rule #{rid} saved. It is not used until it is approved.", "success")
            except tariff.RuleError as exc:
                flash(str(exc), "error")
            return redirect(url_for("page_tariffs"))
        with S() as conn:
            rules_ = [dict(r) for r in conn.execute("SELECT * FROM electricity_tariff_rules ORDER BY effective_from DESC, id DESC")]
        return render_template("tariffs.html", active_page="tariffs", rules=rules_)

    @app.route("/tariffs/<int:rule_id>/<action>", methods=["POST"])
    @auth.admin_required
    def act_tariff(rule_id, action):
        status = {"approve": "APPROVED", "reject": "REJECTED", "draft": "DRAFT"}.get(action)
        if not status:
            abort(404)
        try:
            with S() as conn:
                tariff.set_approval(conn, rule_id, status, g.user["username"])
                n = bills.reanalyze_all(conn)
                audit.log(conn, g.user["username"], f"TARIFF_RULE_{status}", "tariff_rule", rule_id, {"bills_re_analysed": n}, role=g.user["role"], ip=request.remote_addr)
        except tariff.RuleError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True, message=f"Rule #{rule_id} is now {status}; {n} bills were re-analysed.")

    @app.route("/settings", methods=["GET", "POST"])
    @auth.admin_required
    def page_settings():
        if request.method == "POST":
            vals = {k: request.form.get(k, "") for k in settings.DEFAULTS if k in request.form or settings.DEFAULTS[k][1] is bool}
            for k, (_d, kind, _t) in settings.DEFAULTS.items():
                if kind is bool:
                    vals[k] = "true" if request.form.get(k) == "on" else "false"
            try:
                with S() as conn:
                    before = settings.get_all(conn)
                    cleaned = settings.set_many(conn, vals)
                    changed = {k: [before[k], v] for k, v in cleaned.items() if before[k] != v}
                    audit.log(conn, g.user["username"], "SETTINGS_CHANGE", "settings", None, changed, role=g.user["role"], ip=request.remote_addr)
                    if any(k.startswith(("pf_", "target_pf")) for k in changed):
                        bills.reanalyze_all(conn)
                flash("Settings saved.", "success")
            except ValueError as exc:
                flash(str(exc), "error")
            return redirect(url_for("page_settings"))
        with S() as conn:
            cfg = settings.get_all(conn)
        return render_template("settings.html", active_page="settings", cfg=cfg, defs=settings.DEFAULTS, smtp=load_smtp_settings().public_dict())

    @app.route("/rules", methods=["GET", "POST"])
    @auth.admin_required
    def page_rules():
        if request.method == "POST":
            try:
                with S() as conn:
                    if request.form.get("op") == "add":
                        identification.add_rule(conn, request.form.get("rule_type", ""), request.form.get("value", ""), request.form.get("note") or None)
                    elif request.form.get("op") in ("on", "off"):
                        identification.set_active(conn, int(request.form.get("rule_id", 0)), request.form.get("op") == "on")
                    res = identification.reapply(conn)
                    audit.log(conn, g.user["username"], "ID_RULE_CHANGE", "id_rule", request.form.get("rule_id"), {"op": request.form.get("op"), **res}, role=g.user["role"], ip=request.remote_addr)
                flash(f"Rules saved and re-applied to the stored POs ({res['now_eligible']} now eligible, {res['now_excluded']} now excluded).", "success")
            except (ValueError, KeyError) as exc:
                flash(str(exc), "error")
            return redirect(url_for("page_rules"))
        with S() as conn:
            rules_ = [dict(r) for r in conn.execute("SELECT * FROM electricity_id_rules ORDER BY rule_type, value")]
            plants_ = [dict(r) for r in conn.execute(
                "SELECT plant_code, MIN(plant_name) AS plant_name, COUNT(*) AS pos, COUNT(DISTINCT supplier_name) AS suppliers, MAX(po_created) AS latest "
                "FROM electricity_pos WHERE eligible = 1 GROUP BY plant_code ORDER BY plant_code")]
        return render_template("rules.html", active_page="rules", rules=rules_, plants=plants_, types=identification.RULE_TYPES)

    @app.route("/discovery")
    @auth.admin_required
    def page_discovery():
        with S() as conn:
            one = lambda sql: conn.execute(sql).fetchone()[0] or 0
            files = dict(conn.execute("SELECT fetch_status, COUNT(*) FROM electricity_attachments GROUP BY fetch_status").fetchall())
            stats = {"pos_total": one("SELECT COUNT(*) FROM electricity_pos"), "pos_eligible": one("SELECT COUNT(*) FROM electricity_pos WHERE eligible = 1"),
                     "plants": one("SELECT COUNT(DISTINCT plant_code) FROM electricity_pos WHERE eligible = 1"), "web_links": files.get("WEB_LINK", 0),
                     "files_downloaded": files.get("DOWNLOADED", 0), "files_pending": files.get("PENDING", 0) + files.get("FAILED", 0),
                     "files_total": sum(v for k, v in files.items() if k != "WEB_LINK")}
            runs = [dict(r) for r in conn.execute("SELECT * FROM electricity_scan_runs ORDER BY id DESC LIMIT 8")]
            pos = [dict(r) for r in conn.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM electricity_attachments a WHERE a.po_header_id = p.po_header_id AND a.fetch_status <> 'WEB_LINK') AS files, "
                "(SELECT COUNT(*) FROM electricity_attachments a WHERE a.po_header_id = p.po_header_id AND a.fetch_status = 'WEB_LINK') AS web_links "
                "FROM electricity_pos p ORDER BY p.po_created DESC LIMIT 25")]
        return render_template("discovery.html", active_page="discovery", s=stats, runs=runs, pos=pos)

    @app.route("/accounts/<int:account_id>", methods=["POST"])
    @auth.admin_required
    def act_account(account_id):
        every = request.form.get("expected_every_days", "").strip()
        grace = request.form.get("grace_days", "").strip()
        try:
            every_i = int(every) if every else None
            grace_i = int(grace) if grace else None
        except ValueError:
            return jsonify(error="Days must be whole numbers."), 400
        if every_i is not None and not 1 <= every_i <= 400:
            return jsonify(error="Expected interval must be between 1 and 400 days."), 400
        with S() as conn:
            conn.execute("UPDATE electricity_consumer_accounts SET consumer_category = ?, voltage_category = ?, schedule_enabled = ?, expected_every_days = ?, grace_days = ? WHERE id = ?",
                         ((request.form.get("consumer_category") or "").strip()[:60] or None, (request.form.get("voltage_category") or "").strip()[:40] or None,
                          1 if request.form.get("schedule_enabled") == "on" and every_i else 0, every_i, grace_i, account_id))
            audit.log(conn, g.user["username"], "ACCOUNT_SETTINGS", "account", account_id, dict(request.form), role=g.user["role"], ip=request.remote_addr)
            bills.reanalyze_all(conn)
        return jsonify(ok=True)

    @app.route("/audit")
    @auth.admin_required
    def page_audit():
        page = max(1, int(request.args.get("page", 1) or 1))
        with S() as conn:
            total = conn.execute("SELECT COUNT(*) FROM electricity_audit_logs").fetchone()[0]
            rows = [dict(r) for r in conn.execute("SELECT * FROM electricity_audit_logs ORDER BY id DESC LIMIT 100 OFFSET ?", ((page - 1) * 100,))]
        return render_template("audit.html", active_page="audit", rows=rows, page=page, pages=max(1, -(-total // 100)), total=total)

    # ------------------------------------------------------------------ endpoints used by the shared RDC-OPS JavaScript
    @app.route("/api/oracle-status")
    @auth.login_required
    def api_oracle_status():
        s = load_oracle_settings()
        where = f" at {s.host}:{s.port}" if auth.is_admin(g.user) else ""          # the server address is shown to administrators only
        if not s.is_complete():
            return jsonify(state="unconfigured", label="Oracle connection is not configured")
        try:
            with socket.create_connection((s.host, int(s.port)), timeout=2):
                return jsonify(state="connected", label=f"Oracle reachable{where}")
        except OSError:
            return jsonify(state="unreachable", label=f"Oracle not reachable{where}")

    @app.route("/api/bg-settings", methods=["POST"])
    @auth.login_required
    def api_bg_settings():
        data = request.get_json(silent=True) or {}
        with S() as conn:
            if "manual_theme" in data and data["manual_theme"] in _THEMES:
                storage.set_state(conn, "bg_theme", data["manual_theme"])
            if "auto_theme" in data:
                storage.set_state(conn, "bg_auto", "true" if data["auto_theme"] else "false")
            if "animate" in data:
                storage.set_state(conn, "bg_animate", "true" if data["animate"] else "false")
        return jsonify(ok=True)

    @app.route("/api/progress")
    def api_progress():
        return jsonify(pct=0, msg="")

    @app.route("/favicon.ico")
    def favicon():
        return ("", 204)

    @app.route("/healthz")
    def healthz():
        """Liveness for a watchdog: the web app answers and the database opens. No data, no login."""
        try:
            with S() as conn:
                conn.execute("SELECT 1").fetchone()
            return jsonify(ok=True)
        except Exception:
            return jsonify(ok=False), 503

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host=os.environ.get("EB_HOST", "127.0.0.1"), port=int(os.environ.get("EB_PORT", "2002")), debug=False, use_reloader=False, threaded=True)
