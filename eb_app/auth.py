"""
auth.py
=======
Login for RDC Electricity Intelligence — uses the SAME users as RDC-OPS (one account, one password), read-only.

* The RDC-OPS database is opened with SQLite mode=ro: this app can never create, change or lock a user there. Password hashes are
  checked here and never leave this module. (EB_AUTH_DB overrides the file, which is how the tests use their own users.)
* Roles are RDC-OPS roles, mapped to what this application allows:

      SUPER_ADMIN                  everything: settings, tariff rules, scan now, retry, corrections, all plants
      HO_VIEWER / FINANCE_VIEWER   view + export every plant (no changes)
      REGIONAL_USER / PLANT_USER   view + export + upload bills for their assigned plants only (and correct their own uploads)
      UEP_ADMIN                    no access (administers a different RDC-OPS module)

* Failed logins are counted per user + address; after MAX_FAILURES in WINDOW_MINUTES the account is locked out for that address.
* Sessions: HttpOnly, SameSite=Lax cookie with its own name (so it never collides with the RDC-OPS cookie on the same host), 20-minute
  idle timeout, per-session CSRF token required on every state-changing request.
"""

import hmac
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import abort, g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import config
from .config import locate_rdc_ops_dir

SUPER_ADMIN = "SUPER_ADMIN"
VIEW_ALL = {"SUPER_ADMIN", "HO_VIEWER", "FINANCE_VIEWER"}
RESTRICTED = {"REGIONAL_USER", "PLANT_USER"}
ALLOWED_ROLES = VIEW_ALL | RESTRICTED
UPLOAD_ROLES = {"SUPER_ADMIN", "REGIONAL_USER", "PLANT_USER"}
IDLE_MINUTES = 20
MAX_FAILURES = 5
WINDOW_MINUTES = 10

_fail_lock = threading.Lock()
_failures: dict = {}
_DUMMY_HASH = generate_password_hash("not-a-real-password")      # checked when the user does not exist, so timing does not reveal it


def ops_db_path():
    env = os.environ.get("EB_AUTH_DB")
    if env:
        return env
    d = locate_rdc_ops_dir()
    return str(d / "data" / "app.db") if d else None


def _ops_conn():
    p = ops_db_path()
    if not p:
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _throttled(key) -> bool:
    now = time.time()
    with _fail_lock:
        hits = [t for t in _failures.get(key, []) if now - t < WINDOW_MINUTES * 60]
        _failures[key] = hits
        return len(hits) >= MAX_FAILURES


def _note_failure(key) -> None:
    with _fail_lock:
        _failures.setdefault(key, []).append(time.time())


def reset_throttle() -> None:
    with _fail_lock:
        _failures.clear()


def authenticate(username: str, password: str, ip: str = ""):
    """Returns ({'id','username','full_name','role','email'}, None) or (None, reason). The reason is safe to show."""
    username = (username or "").strip()
    key = (username.lower(), ip)
    if _throttled(key):
        return None, "Too many failed attempts. Try again in a few minutes."
    conn = _ops_conn()
    if conn is None:
        return None, "The RDC-OPS user database was not found, so nobody can log in."
    try:
        row = conn.execute("SELECT id, username, full_name, email, password_hash, role, is_active FROM users WHERE lower(username) = lower(?)", (username,)).fetchone()
    except sqlite3.Error:
        return None, "The RDC-OPS user database could not be read."
    finally:
        conn.close()
    # same message and similar work for unknown user / wrong password / disabled user (no account enumeration)
    hash_ = row["password_hash"] if row else _DUMMY_HASH
    ok = check_password_hash(hash_, password or "") and bool(row) and bool(row["is_active"])
    if not ok:
        _note_failure(key)
        return None, "Invalid username or password."
    if row["role"] not in ALLOWED_ROLES:
        return None, "Your role has no access to Electricity Intelligence."
    return {"id": row["id"], "username": row["username"], "full_name": row["full_name"], "role": row["role"], "email": row["email"]}, None


def get_user_by_id(uid):
    conn = _ops_conn()
    if conn is None:
        return None
    try:
        r = conn.execute("SELECT id, username, full_name, email, role, is_active FROM users WHERE id = ?", (uid,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not r or not r["is_active"] or r["role"] not in ALLOWED_ROLES:
        return None
    return dict(r)


def allowed_plants(user):
    """None = every plant. Otherwise the list of plant codes (upper case) the user may see."""
    if user["role"] in VIEW_ALL:
        return None
    conn = _ops_conn()
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT plant_code, plant_name FROM user_plant_access WHERE user_id = ?", (user["id"],)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return sorted({(r["plant_code"] or "").strip().upper() for r in rows if (r["plant_code"] or "").strip()})


def can_see_plant(user, plant_code) -> bool:
    ap = allowed_plants(user)
    return ap is None or (plant_code or "").upper() in ap


def can_upload(user) -> bool:
    return user["role"] in UPLOAD_ROLES


def is_admin(user) -> bool:
    return user["role"] == SUPER_ADMIN


# --------------------------------------------------------------------------- Flask glue
def secret_key() -> str:
    env = os.environ.get("EB_SECRET_KEY")
    if env:
        return env
    p = config.DATA_DIR / "secret.key"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(secrets.token_hex(32))
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    return p.read_text().strip()


def init_app(app) -> None:
    app.secret_key = secret_key()
    app.config.update(SESSION_COOKIE_NAME="eb_session", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=os.environ.get("EB_HTTPS") == "1", MAX_CONTENT_LENGTH=config.MAX_ATTACHMENT_BYTES + 1024 * 1024)

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp


def current_user():
    if "uid" not in session:
        return None
    last = session.get("last")
    if last:
        try:
            if datetime.utcnow() - datetime.fromisoformat(last) > timedelta(minutes=IDLE_MINUTES):
                session.clear()
                return None
        except ValueError:
            session.clear()
            return None
    user = get_user_by_id(session["uid"])
    if user is None:
        session.clear()
        return None
    session["last"] = datetime.utcnow().isoformat()
    return user


def login_session(user) -> None:
    session.clear()
    session["uid"] = user["id"]
    session["last"] = datetime.utcnow().isoformat()
    session["csrf"] = secrets.token_hex(16)
    session.permanent = False


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def _check_csrf() -> bool:
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    return bool(sent) and hmac.compare_digest(sent, session.get("csrf", ""))


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        user = current_user()
        if user is None:
            if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
                return jsonify(error="login required"), 401
            return redirect(url_for("page_login", next=request.full_path if request.query_string else request.path))
        g.user = user
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not _check_csrf():
            abort(400, "Missing or invalid security token. Reload the page and try again.")
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not is_admin(g.user):
            abort(403)
        return fn(*a, **kw)
    return login_required(wrapper)


def upload_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not can_upload(g.user):
            abort(403)
        return fn(*a, **kw)
    return login_required(wrapper)
