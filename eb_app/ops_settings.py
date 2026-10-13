"""
ops_settings.py
===============
Reuses the Oracle EBS R12 connection settings that RDC-OPS already stores, so
there is exactly one place where the connection is configured.

* The RDC-OPS database is opened strictly read-only (SQLite mode=ro).
* We never import RDC-OPS code: its modules use cwd-relative paths and would
  create/modify files in the wrong folder.
* EB_ORACLE_HOST / PORT / SERVICE / USER / PASSWORD / INSTANTCLIENT environment
  variables override the stored values (for a different deployment host).
"""

import os
import sqlite3
from dataclasses import dataclass, field

from . import config


@dataclass(frozen=True)
class OracleSettings:
    host: str
    port: str
    service: str
    user: str
    password: str = field(repr=False)          # never shown in repr/logs
    instantclient: str = ""

    @property
    def dsn(self) -> str:
        return f"{self.host}:{self.port}/{self.service}"

    def is_complete(self) -> bool:
        return bool(self.host and self.port and self.service and self.user and self.password)

    def public_dict(self) -> dict:
        """Safe-to-display view (password reported only as set/unset)."""
        return {"host": self.host, "port": self.port, "service": self.service, "user": self.user,
                "password": "<set>" if self.password else "<empty>", "instantclient": self.instantclient}


_KEYS = {
    "host": ("oracle_host", "EB_ORACLE_HOST", ""),            # no built-in address: it comes from the RDC-OPS settings or EB_ORACLE_HOST
    "port": ("oracle_port", "EB_ORACLE_PORT", "1528"),
    "service": ("oracle_service", "EB_ORACLE_SERVICE", "RDCAZPRD"),
    "user": ("oracle_user", "EB_ORACLE_USER", ""),
    "password": ("oracle_password", "EB_ORACLE_PASSWORD", ""),
    "instantclient": ("oracle_instantclient_dir", "EB_ORACLE_INSTANTCLIENT", ""),
}


def _read_rdc_ops_settings() -> dict:
    ops_dir = config.locate_rdc_ops_dir()
    if ops_dir is None:
        return {}
    uri = f"file:{(ops_dir / 'data' / 'app.db').as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            return dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    sender: str
    password: str = field(repr=False)
    use_tls: bool = True

    def is_complete(self) -> bool:
        return bool(self.host and self.sender and self.password)

    def public_dict(self) -> dict:
        return {"host": self.host, "port": self.port, "sender": self.sender, "password": "<set>" if self.password else "<empty>",
                "use_tls": self.use_tls}


def load_smtp_settings() -> SmtpSettings:
    """The mail account RDC-OPS already uses (read-only). EB_SMTP_HOST / PORT / SENDER / PASSWORD / TLS override it; like RDC-OPS the
    SMTP_PASSWORD environment variable beats the stored password."""
    s = _read_rdc_ops_settings()
    try:
        port = int(os.environ.get("EB_SMTP_PORT") or s.get("smtp_port") or 587)
    except ValueError:
        port = 587
    tls_raw = os.environ.get("EB_SMTP_TLS") or s.get("smtp_use_tls") or "true"
    return SmtpSettings(
        host=(os.environ.get("EB_SMTP_HOST") or s.get("smtp_host") or "").strip(),
        port=port,
        sender=(os.environ.get("EB_SMTP_SENDER") or s.get("smtp_sender") or "").strip(),
        password=os.environ.get("EB_SMTP_PASSWORD") or os.environ.get("SMTP_PASSWORD") or s.get("smtp_password") or "",
        use_tls=str(tls_raw).strip().lower() in ("1", "true", "yes", "on"))


def load_oracle_settings() -> OracleSettings:
    stored = _read_rdc_ops_settings()
    values = {}
    for attr, (db_key, env_key, default) in _KEYS.items():
        v = os.environ.get(env_key) or stored.get(db_key) or default
        values[attr] = str(v).strip() if attr != "password" else str(v)
    return OracleSettings(**values)
