"""
scheduler.py
============
Automatic Oracle scanning (Phase 4) — independent of any browser.

How it stays correct
  * A backend thread (APScheduler) wakes every 60 s and asks ONE question: "is a scan due?" The answer lives in the database
    (state 'sched.next_due'), not in memory, so a restart simply resumes: the next scan is still due when it was due.
  * The interval is `scan_interval_minutes` (default 120 = 2 hours). A successful cycle schedules the next one 2 hours after it
    STARTED; a failed cycle backs off exponentially (5, 10, 20, 40 ... minutes, never longer than the interval).
  * Overlap is impossible: a cycle first takes the database lock 'scan-cycle' (expiring + heart-beaten), so the web app, a
    standalone worker and a "Scan Now" click can all exist at once and only one scans.
  * The first cycle of each day (at/after `reconcile_hour`) is a FULL reconciliation scan; every other cycle is incremental
    (attachments created or changed since the last success, with a 24 h overlap), so an old PO that receives a bill months later
    is still found.
  * A cycle = scan Oracle -> download new/changed/retry-due attachments -> read them (OCR budget per cycle) -> evaluate alerts ->
    send alert e-mail. Every step is recorded in electricity_scan_runs; errors in electricity_processing_errors.
"""

import logging
import os
import socket
import threading
import time
from datetime import datetime, timedelta

from . import alerts, audit, discovery, jobs, notify, settings, storage

log = logging.getLogger(__name__)

LOCK_NAME = "scan-cycle"
LOCK_TTL_SECONDS = 30 * 60
MAX_BACKOFF_MINUTES = None          # None = capped by the scan interval


def _ts(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat(sep=" ")


def owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


# --------------------------------------------------------------------------- locking
def pid_alive(pid: int) -> bool:
    """Is a process with this id running on this machine?"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)                       # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return k32.GetLastError() == 5                            # access denied => it exists
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259                         # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def owner_is_dead(owner: str) -> bool:
    """A lock owner is 'host:pid:thread'. If it belongs to this machine and that process is gone, the lock is a leftover of a crash."""
    parts = (owner or "").split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[0] != socket.gethostname():
        return False
    pid = int(parts[1])
    return pid != os.getpid() and not pid_alive(pid)


def acquire(conn, owner: str, now: datetime, ttl: int = LOCK_TTL_SECONDS, name: str = LOCK_NAME) -> bool:
    """Take the lock if it is free, expired, held by a process that no longer exists, or already ours. Atomic."""
    held = conn.execute("SELECT owner FROM electricity_job_locks WHERE name = ?", (name,)).fetchone()
    if held and held["owner"] != owner and owner_is_dead(held["owner"]):
        conn.execute("DELETE FROM electricity_job_locks WHERE name = ? AND owner = ?", (name, held["owner"]))
    n = conn.execute(
        "INSERT INTO electricity_job_locks (name, owner, acquired_at, heartbeat_at, expires_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, acquired_at = excluded.acquired_at, heartbeat_at = excluded.heartbeat_at, "
        "expires_at = excluded.expires_at WHERE electricity_job_locks.expires_at < ? OR electricity_job_locks.owner = ?",
        (name, owner, _ts(now), _ts(now), _ts(now + timedelta(seconds=ttl)), _ts(now), owner)).rowcount
    return n == 1


def heartbeat(db_path, owner: str, ttl: int = LOCK_TTL_SECONDS, name: str = LOCK_NAME) -> None:
    now = datetime.now()
    with storage.session(db_path) as conn:
        conn.execute("UPDATE electricity_job_locks SET heartbeat_at = ?, expires_at = ? WHERE name = ? AND owner = ?",
                     (_ts(now), _ts(now + timedelta(seconds=ttl)), name, owner))


def release(db_path, owner: str, name: str = LOCK_NAME) -> None:
    with storage.session(db_path) as conn:
        conn.execute("DELETE FROM electricity_job_locks WHERE name = ? AND owner = ?", (name, owner))


# --------------------------------------------------------------------------- scheduling state
def get_schedule(conn) -> dict:
    g = lambda k, d=None: storage.get_state(conn, "sched." + k, d)
    return {"next_due": g("next_due"), "last_success_at": g("last_success_at"), "last_cycle_at": g("last_cycle_at"),
            "consecutive_failures": int(g("consecutive_failures", "0")), "last_error": g("last_error", ""), "last_tick_at": g("last_tick_at"),
            "last_full_date": g("last_full_date")}


def _set(conn, **kv):
    for k, v in kv.items():
        storage.set_state(conn, "sched." + k, "" if v is None else str(v))


def is_due(conn, now: datetime) -> bool:
    cfg_enabled = settings.get(conn, "scheduler_enabled")
    if not cfg_enabled:
        return False
    nd = storage.get_state(conn, "sched.next_due")
    return not nd or now >= datetime.fromisoformat(nd)


def decide_mode(conn, now: datetime) -> str:
    sched = get_schedule(conn)
    if not storage.get_state(conn, "last_success_started_at"):
        return "full"
    hour = settings.get(conn, "reconcile_hour")
    if sched["last_full_date"] != now.date().isoformat() and now.hour >= hour:
        return "full"
    return "incremental"


def _after_cycle(conn, started: datetime, finished: datetime, ok: bool, error: str, full_done: bool) -> None:
    interval = settings.get(conn, "scan_interval_minutes")
    fails = get_schedule(conn)["consecutive_failures"]
    if ok:
        fails = 0
        nxt = max(started + timedelta(minutes=interval), finished + timedelta(seconds=60))
        _set(conn, last_success_at=_ts(finished), consecutive_failures=0, last_error="")
        if full_done:
            _set(conn, last_full_date=started.date().isoformat())
    else:
        fails += 1
        delay = min(interval, 5 * 2 ** (fails - 1)) if MAX_BACKOFF_MINUTES is None else min(MAX_BACKOFF_MINUTES, 5 * 2 ** (fails - 1))
        nxt = finished + timedelta(minutes=delay)
        _set(conn, consecutive_failures=fails, last_error=error[:300])
    _set(conn, last_cycle_at=_ts(finished), next_due=_ts(nxt))
    alerts.evaluate_scheduler(conn, fails, error if not ok else "")


# --------------------------------------------------------------------------- one cycle
def run_cycle(source, trigger: str = "scheduler", db_path=None, now: datetime | None = None, force_mode: str | None = None,
              run_ocr: bool = True, budget_minutes: float | None = None, send_mail: bool = True, mail_transport=None, smtp=None,
              real_clock: bool = False) -> dict:
    """Scan -> download -> read -> alert. Returns a summary; never raises for Oracle/reader problems."""
    from .reader import store
    injected = now is not None and not real_clock   # tests pass a fixed clock; production uses the real one (tick() passes real_clock=True)
    now = now or datetime.now()
    owner = owner_id()
    with storage.session(db_path) as conn:
        if not acquire(conn, owner, now):
            return {"status": "SKIPPED", "reason": "another scan cycle is already running"}
        mode = force_mode or decide_mode(conn, now)
        cfg = settings.get_all(conn)
    started = now
    out = {"status": "SUCCESS", "mode": mode, "trigger": trigger, "downloaded": 0, "download_failed": 0, "processed": 0, "process_failed": 0,
           "alert_mail": None}
    try:
        stats = discovery.run_scan(source, mode=mode, trigger=trigger, db_path=db_path, now=now)
        out.update(run_id=stats["run_id"], pos_inspected=stats["pos_inspected"], new_pos=stats["new_pos"],
                   new_attachments=stats["new_attachments"], changed_attachments=stats["changed_attachments"])
        if stats["status"] == "FAILED":
            out.update(status="FAILED", error=stats["message"])
            with storage.session(db_path) as conn:
                audit.record_error(conn, "SCAN", "scan_run", stats["run_id"], stats["message"])
            return out
        def save_progress():                         # counters are written as work happens, so a crash still shows how far the cycle got
            with storage.session(db_path) as c2:
                c2.execute("UPDATE electricity_scan_runs SET downloaded = ?, download_failed = ?, processed = ?, process_failed = ? WHERE id = ?",
                           (out["downloaded"], out["download_failed"], out["processed"], out["process_failed"], stats["run_id"]))

        # ---- download everything that is new, changed, or due for a retry
        remaining = cfg["download_batch"]
        while remaining > 0:
            heartbeat(db_path, owner)
            d = discovery.download_pending(source, limit=min(remaining, 50), db_path=db_path, now=now)
            out["downloaded"] += d["downloaded"]
            out["download_failed"] += d["failed"]
            remaining -= max(d["attempted"], 0)
            save_progress()
            if d["attempted"] == 0:
                break
        # ---- read bills until the OCR budget of this cycle is spent
        budget = (budget_minutes if budget_minutes is not None else cfg["process_budget_minutes"]) * 60
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            ids = store.pending_attachment_ids(1, db_path)                 # one at a time: the budget is checked after every bill
            outdated = [] if ids else store.outdated_extraction_ids(1, db_path)
            if not ids and not outdated:
                break
            for i in ids:
                heartbeat(db_path, owner)
                with jobs.OCR_GATE:
                    r = store.process_attachment(i, db_path=db_path, run_ocr=run_ocr)
                out["processed"] += 1
                out["process_failed"] += 1 if r["bill_status"] == "FAILED" else 0
                save_progress()
            for e in outdated:
                heartbeat(db_path, owner)
                with jobs.OCR_GATE:
                    r = store.reread_outdated(e, db_path=db_path, run_ocr=run_ocr)
                out["processed"] += 1
                out["process_failed"] += 1 if r["bill_status"] == "FAILED" else 0
                save_progress()
            if time.monotonic() >= deadline:
                out["note"] = "reading budget used up; the remaining bills are read in the next cycle"
        # ---- alerts and e-mail
        with storage.session(db_path) as conn:
            alerts.evaluate_download_failures(conn, discovery.MAX_DOWNLOAD_RETRIES)
            alerts.evaluate_missing_bills(conn, now.date())
            conn.execute("UPDATE electricity_scan_runs SET downloaded = ?, download_failed = ?, processed = ?, process_failed = ? WHERE id = ?",
                         (out["downloaded"], out["download_failed"], out["processed"], out["process_failed"], stats["run_id"]))
            if send_mail:
                out["alert_mail"] = notify.send_pending(conn, smtp=smtp, transport=mail_transport, now=now)
    except Exception as exc:                       # the scheduler itself must survive anything
        log.exception("Scan cycle crashed")
        out.update(status="FAILED", error=f"{type(exc).__name__}: {exc}")
        with storage.session(db_path) as conn:
            audit.record_error(conn, "SCAN", "cycle", None, out["error"])
    finally:
        finished = now if injected else datetime.now()
        try:
            with storage.session(db_path) as conn:
                _after_cycle(conn, started, finished, out["status"] == "SUCCESS", out.get("error", ""), full_done=(mode == "full" and out["status"] == "SUCCESS"))
        finally:
            release(db_path, owner)
    return out


def tick(source, db_path=None, now: datetime | None = None, **kw):
    """Called every minute. Records a heartbeat, then runs a cycle if one is due."""
    real_clock = now is None
    now = now or datetime.now()
    with storage.session(db_path) as conn:
        _set(conn, last_tick_at=_ts(now))
        due = is_due(conn, now)
    if not due:
        return None
    return run_cycle(source, trigger="scheduler", db_path=db_path, now=now, real_clock=real_clock, **kw)


def recover(db_path=None) -> dict:
    """Run once at start-up: free a lock and a scan-run row left behind by a crash, re-queue interrupted jobs."""
    now = datetime.now()
    with storage.session(db_path) as conn:
        stale = conn.execute("DELETE FROM electricity_job_locks WHERE expires_at < ?", (_ts(now),)).rowcount
        for lk in conn.execute("SELECT name, owner FROM electricity_job_locks").fetchall():
            if owner_is_dead(lk["owner"]):                                 # the process that held it crashed: free it at once, do not wait for the expiry
                stale += conn.execute("DELETE FROM electricity_job_locks WHERE name = ? AND owner = ?", (lk["name"], lk["owner"])).rowcount
        runs = conn.execute("UPDATE electricity_scan_runs SET status = 'FAILED', finished_at = ?, message = 'interrupted (application stopped); resumed after restart' "
                            "WHERE status = 'RUNNING' AND NOT EXISTS (SELECT 1 FROM electricity_job_locks WHERE name = ?)", (_ts(now), LOCK_NAME)).rowcount
        res = jobs.recover(conn, now)
    return {"stale_locks": stale, "interrupted_runs": runs, **res}


class Scheduler:
    """Owns the APScheduler thread and the job-queue worker. Safe to create in the web app and in a stand-alone worker process."""

    def __init__(self, source, db_path=None, tick_seconds: int = 60, run_ocr: bool = True, with_worker: bool = True):
        self.source, self.db_path, self.tick_seconds, self.run_ocr = source, db_path, tick_seconds, run_ocr
        self.with_worker = with_worker
        self._sched = None
        self.worker = None
        self._busy = threading.Lock()

    def _tick(self):
        if not self._busy.acquire(blocking=False):
            return                                   # the previous tick is still running a long cycle
        try:
            tick(self.source, self.db_path, run_ocr=self.run_ocr)
        except Exception:
            log.exception("scheduler tick failed")
        finally:
            self._busy.release()

    def start(self) -> dict:
        from apscheduler.schedulers.background import BackgroundScheduler
        info = recover(self.db_path)
        self._sched = BackgroundScheduler(daemon=True)
        self._sched.add_job(self._tick, "interval", seconds=self.tick_seconds, id="eb-tick", max_instances=1, coalesce=True,
                            next_run_time=datetime.now() + timedelta(seconds=3))
        self._sched.start()
        if self.with_worker:
            self.worker = jobs.Worker(db_path=self.db_path, run_ocr=self.run_ocr)
            self.worker.start()
        log.warning("Scheduler started (tick every %ss); recovery: %s", self.tick_seconds, info)
        return info

    def stop(self):
        if self._sched is not None:
            self._sched.shutdown(wait=False)
            self._sched = None
        if self.worker is not None:
            self.worker.stop()

    @property
    def running(self) -> bool:
        return self._sched is not None and self._sched.running

    def scan_now(self, trigger: str = "manual") -> dict:
        """Start a cycle in the background (the web request returns at once)."""
        with storage.session(self.db_path) as conn:
            row = conn.execute("SELECT owner FROM electricity_job_locks WHERE name = ? AND expires_at >= ?", (LOCK_NAME, _ts(datetime.now()))).fetchone()
        if row:
            return {"started": False, "reason": "a scan cycle is already running"}
        threading.Thread(target=lambda: run_cycle(self.source, trigger=trigger, db_path=self.db_path, run_ocr=self.run_ocr, force_mode="incremental"),
                         name="eb-scan-now", daemon=True).start()
        return {"started": True}
