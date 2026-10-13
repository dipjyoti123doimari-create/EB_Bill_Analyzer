"""
jobs.py
=======
Persistent background-job queue (SQLite) used for work that must not block a web request or the scanner: reading a manually uploaded
bill, re-reading a bill, reading a freshly downloaded attachment.

* enqueue() is idempotent: while a job for (kind, ref) is queued or running, a second enqueue returns the same job.
* claim_next() is an atomic UPDATE, so any number of worker threads / processes can run without ever taking the same job twice.
* A failed job is retried with exponential back-off up to max_attempts, then marked FAILED and logged in the processing-error table.
* A job whose worker died (restart, crash) is put back in the queue by recover().
* OCR is CPU-heavy: one reading at a time per process (OCR_GATE), so the website stays responsive.
"""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta

from . import audit, storage

log = logging.getLogger(__name__)

OCR_GATE = threading.Lock()
STALE_AFTER_MINUTES = 45            # a RUNNING job older than this belongs to a dead worker (a bill takes minutes, not an hour)


def _ts(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat(sep=" ")


def enqueue(conn, kind: str, ref_id: int, max_attempts: int = 3) -> int:
    try:
        return conn.execute("INSERT INTO electricity_jobs (kind, ref_id, max_attempts, created_at) VALUES (?,?,?,?)",
                            (kind, ref_id, max_attempts, storage.now_iso())).lastrowid
    except sqlite3.IntegrityError:
        return conn.execute("SELECT id FROM electricity_jobs WHERE kind = ? AND ref_id = ? AND status IN ('QUEUED','RUNNING')", (kind, ref_id)).fetchone()[0]


def claim_next(conn, worker: str, now: datetime | None = None):
    now = now or datetime.now()
    for _ in range(5):
        row = conn.execute("SELECT id FROM electricity_jobs WHERE status = 'QUEUED' AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                           "ORDER BY id LIMIT 1", (_ts(now),)).fetchone()
        if row is None:
            return None
        n = conn.execute("UPDATE electricity_jobs SET status = 'RUNNING', claimed_by = ?, claimed_at = ?, attempts = attempts + 1 "
                         "WHERE id = ? AND status = 'QUEUED'", (worker, _ts(now), row["id"])).rowcount
        if n == 1:
            return conn.execute("SELECT * FROM electricity_jobs WHERE id = ?", (row["id"],)).fetchone()
    return None


def recover(conn, now: datetime | None = None) -> dict:
    """After a restart: RUNNING jobs of a dead worker go back to QUEUED, uploads stuck in PROCESSING are re-queued."""
    now = now or datetime.now()
    limit = _ts(now - timedelta(minutes=STALE_AFTER_MINUTES))
    jobs = conn.execute("UPDATE electricity_jobs SET status = 'QUEUED', claimed_by = NULL, error = 'worker stopped; job re-queued' "
                        "WHERE status = 'RUNNING' AND claimed_at <= ?", (limit,)).rowcount
    uploads = conn.execute("UPDATE electricity_uploads SET status = 'QUEUED' WHERE status = 'PROCESSING' AND id IN "
                           "(SELECT ref_id FROM electricity_jobs WHERE kind = 'PROCESS_UPLOAD' AND status = 'QUEUED')").rowcount
    return {"requeued_jobs": jobs, "requeued_uploads": uploads}


def run_job(job, db_path=None, run_ocr=True) -> None:
    from .reader import store
    kind, ref = job["kind"], job["ref_id"]
    if kind == "PROCESS_ATTACHMENT":
        store.process_attachment(ref, db_path=db_path, run_ocr=run_ocr)
    elif kind == "PROCESS_UPLOAD":
        store.process_upload(ref, db_path=db_path, run_ocr=run_ocr)
    elif kind == "RETRY_EXTRACTION":
        store.retry_extraction(ref, user="retry-queue", db_path=db_path, run_ocr=run_ocr)
    else:
        raise ValueError(f"unknown job kind {kind}")


def work_once(worker: str, db_path=None, run_ocr=True, now: datetime | None = None) -> bool:
    """Claim and run one job. Returns False when the queue is empty."""
    now = now or datetime.now()
    with storage.session(db_path) as conn:
        job = claim_next(conn, worker, now)
    if job is None:
        return False
    err = None
    try:
        with OCR_GATE:
            run_job(job, db_path=db_path, run_ocr=run_ocr)
    except Exception as exc:                      # never let one bad file stop the queue
        err = f"{type(exc).__name__}: {exc}"
        log.warning("Job %s (%s #%s) failed: %s", job["id"], job["kind"], job["ref_id"], err)
    with storage.session(db_path) as conn:
        if err is None:
            conn.execute("UPDATE electricity_jobs SET status = 'DONE', finished_at = ?, error = NULL WHERE id = ?", (storage.now_iso(), job["id"]))
        elif job["attempts"] < job["max_attempts"]:
            delay = timedelta(seconds=30 * 2 ** job["attempts"])
            conn.execute("UPDATE electricity_jobs SET status = 'QUEUED', error = ?, claimed_by = NULL, next_attempt_at = ? WHERE id = ?",
                         (err[:400], _ts(datetime.now() + delay), job["id"]))
        else:
            conn.execute("UPDATE electricity_jobs SET status = 'FAILED', finished_at = ?, error = ? WHERE id = ?", (storage.now_iso(), err[:400], job["id"]))
            audit.record_error(conn, "JOB", job["kind"], job["ref_id"], err)
            if job["kind"] == "PROCESS_UPLOAD":
                conn.execute("UPDATE electricity_uploads SET status = 'FAILED', error = ? WHERE id = ?", (err[:400], job["ref_id"]))
    return True


def drain(worker: str, db_path=None, run_ocr=True, max_jobs: int = 1000) -> int:
    n = 0
    while n < max_jobs and work_once(worker, db_path, run_ocr):
        n += 1
    return n


class Worker(threading.Thread):
    """Daemon thread that keeps draining the queue (started by the web app and by `python -m eb_app.worker`)."""

    def __init__(self, name="eb-job-worker", db_path=None, poll_seconds=3.0, run_ocr=True):
        super().__init__(name=name, daemon=True)
        self.db_path, self.poll, self.run_ocr = db_path, poll_seconds, run_ocr
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                busy = work_once(self.name, self.db_path, self.run_ocr)
            except Exception:
                log.exception("job worker loop error")
                busy = False
            if not busy:
                self._halt.wait(self.poll)

    def stop(self):
        self._halt.set()
