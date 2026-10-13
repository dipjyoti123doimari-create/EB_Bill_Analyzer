"""Phase 4 tests: job queue, scheduler (cycles, back-off, restart, locking, retries), discovery of new / old / revised / duplicate attachments,
manual upload and its validation, duplicate linking.  Oracle is replaced by FakeSource; documents are real (synthetic) PDFs read by the real reader.
"""
import io
import pathlib
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import helpers
import synth
from test_phase1 import FakeSource, TempDbCase
from eb_app import alerts, config, discovery, jobs, notify, scheduler, settings, storage, uploads
from eb_app.oracle_source import OracleError
from eb_app.ops_settings import SmtpSettings
from eb_app.reader import store


def pdf_bytes(**kw):
    d = tempfile.mkdtemp()
    p = synth.make_bill(pathlib.Path(d) / "b.pdf", **kw)
    return p.read_bytes()


class Env(TempDbCase):
    """TempDbCase + a FakeSource whose attachments are real bill PDFs."""

    def setUp(self):
        super().setUp()
        self.src = FakeSource()
        self.src.blobs = {900: (pdf_bytes(bill_no="B-1001"), "bill.pdf", "application/pdf")}
        self.now = datetime(2026, 10, 13, 9, 0, 0)

    def cycle(self, now=None, **kw):
        kw.setdefault("run_ocr", False)
        kw.setdefault("send_mail", False)
        return scheduler.run_cycle(self.src, db_path=self.db, now=now or self.now, **kw)

    def state(self, key):
        with storage.session(self.db) as c:
            return storage.get_state(c, key)

    def bills(self):
        return self.rows("SELECT * FROM electricity_bills ORDER BY id")


# ------------------------------------------------------------------------------------------- locks
class LockTests(Env):
    def test_only_one_owner_holds_the_lock_until_it_expires(self):
        with storage.session(self.db) as c:
            self.assertTrue(scheduler.acquire(c, "A", self.now))
            self.assertFalse(scheduler.acquire(c, "B", self.now + timedelta(minutes=5)))
            self.assertTrue(scheduler.acquire(c, "A", self.now + timedelta(minutes=5)))            # the owner may renew
            self.assertTrue(scheduler.acquire(c, "B", self.now + timedelta(minutes=5 + 31)))        # expired => takeover after a crash

    def test_a_lock_left_by_a_crashed_process_is_taken_over_at_once_not_after_30_minutes(self):
        import socket
        dead = f"{socket.gethostname()}:999999:1"                                         # no such process on this machine
        self.assertFalse(scheduler.pid_alive(999999))
        self.assertTrue(scheduler.pid_alive(__import__("os").getpid()))
        with storage.session(self.db) as c:
            self.assertTrue(scheduler.acquire(c, dead, self.now))
            self.assertTrue(scheduler.acquire(c, "me", self.now + timedelta(seconds=5)))      # the dead owner's lock does not block the restart
        with storage.session(self.db) as c:
            c.execute("DELETE FROM electricity_job_locks")
            live_other_host = "SOME-OTHER-PC:4242:1"
            scheduler.acquire(c, live_other_host, self.now)
            self.assertFalse(scheduler.acquire(c, "me", self.now + timedelta(seconds=5)))     # a lock of another machine is respected until it expires
        with storage.session(self.db) as c:
            c.execute("DELETE FROM electricity_job_locks")
            scheduler.acquire(c, dead, self.now)
        info = scheduler.recover(self.db)                                                   # start-up recovery frees it too
        self.assertEqual(info["stale_locks"], 1)
        self.assertEqual(self.rows("SELECT name FROM electricity_job_locks"), [])

    def test_progress_is_recorded_while_the_cycle_runs(self):
        self.cycle()
        run = self.rows("SELECT downloaded, processed FROM electricity_scan_runs")[0]
        self.assertEqual((run["downloaded"], run["processed"]), (1, 1))

    def test_concurrent_cycles_do_not_overlap(self):
        results = []
        gate = threading.Barrier(2)

        def go():
            gate.wait()
            results.append(self.cycle())
        ts = [threading.Thread(target=go) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        statuses = sorted(r["status"] for r in results)
        # one of them scanned; the other either skipped (lock held) or ran strictly after the first finished -- never at the same time
        self.assertIn("SUCCESS", statuses)
        self.assertEqual(len(self.rows("SELECT id FROM electricity_scan_runs WHERE status = 'RUNNING'")), 0)
        self.assertEqual(len(self.rows("SELECT name FROM electricity_job_locks")), 0)                  # the lock is always released

    def test_a_held_lock_makes_the_cycle_skip(self):
        with storage.session(self.db) as c:
            scheduler.acquire(c, "someone-else", datetime.now())
        r = scheduler.run_cycle(self.src, db_path=self.db, run_ocr=False, send_mail=False)
        self.assertEqual(r["status"], "SKIPPED")
        self.assertEqual(len(self.rows("SELECT id FROM electricity_scan_runs")), 0)


# ------------------------------------------------------------------------------------------- one cycle end to end
class CycleTests(Env):
    def test_new_po_is_found_downloaded_read_and_becomes_a_bill(self):
        r = self.cycle()
        self.assertEqual((r["status"], r["mode"]), ("SUCCESS", "full"))
        self.assertEqual((r["downloaded"], r["processed"]), (1, 1))
        b = self.bills()
        self.assertEqual(len(b), 1)
        self.assertEqual((b[0]["plant_code"], b[0]["source"], b[0]["kwh"], b[0]["billing_month"]), ("BG2", "ORACLE_EBS", 90000.0, "2026-09"))
        run = self.rows("SELECT * FROM electricity_scan_runs")[0]
        self.assertEqual((run["status"], run["trigger"], run["downloaded"], run["processed"]), ("SUCCESS", "scheduler", 1, 1))
        self.assertTrue(pathlib.Path(config.STORAGE_DIR, self.rows("SELECT local_path FROM electricity_attachments")[0]["local_path"]).is_file())   # the original is stored

    def test_the_second_cycle_is_incremental_and_creates_nothing_twice(self):
        self.cycle()
        r = self.cycle(now=self.now + timedelta(hours=2))
        self.assertEqual((r["mode"], r["downloaded"], r["processed"]), ("incremental", 0, 0))
        self.assertEqual(len(self.bills()), 1)
        self.assertEqual(len(self.rows("SELECT id FROM electricity_bill_extractions")), 1)

    def test_an_old_po_that_receives_a_bill_months_later_is_found(self):
        self.cycle()
        self.src.blobs[901] = (pdf_bytes(bill_no="B-2002", start="01-10-2026", end="31-10-2026", consumer="123456789012"), "oct.pdf", "application/pdf")
        new = FakeSource.att(1001, "PO_HEADERS", 10, 5002, "oct.pdf", media=901, size=2000, updated=datetime.now())
        new["att_created"] = datetime.now()
        self.src.atts.append(new)                                                                   # PO 10 was created in Sept; this file is attached now
        r = self.cycle(now=datetime.now())
        self.assertEqual((r["new_attachments"], r["downloaded"], r["processed"]), (1, 1, 1))
        self.assertEqual(sorted(b["billing_month"] for b in self.bills()), ["2026-09", "2026-10"])

    def test_a_revised_attachment_keeps_the_first_bill_and_counts_the_latest(self):
        self.cycle()
        self.src.blobs[902] = (pdf_bytes(bill_no="B-1001R", bill_date="12-10-2026", energy=650000.0), "bill_rev.pdf", "application/pdf")
        self.src.atts[0]["media_id"] = 902                                                           # the same attachment now points to a new file
        self.src.atts[0]["att_updated"] = datetime.now()
        r = self.cycle(now=datetime.now())
        self.assertEqual((r["changed_attachments"], r["downloaded"], r["processed"]), (1, 1, 1))
        self.assertEqual(self.rows("SELECT revision FROM electricity_attachments")[0]["revision"], 2)
        b = {x["bill_number"]: x for x in self.bills()}
        self.assertEqual(set(b), {"B-1001", "B-1001R"})                                              # nothing overwritten
        self.assertEqual((b["B-1001"]["is_primary"], b["B-1001R"]["is_primary"]), (0, 1))
        self.assertEqual(len(self.rows("SELECT id FROM electricity_bill_extractions WHERE archived = 0")), 2)

    def test_the_same_file_attached_twice_is_a_duplicate_and_counted_once(self):
        dup = FakeSource.att(1002, "PO_HEADERS", 10, 5003, "copy.pdf", media=903, size=1234)
        self.src.blobs[903] = self.src.blobs[900]
        self.src.atts.append(dup)
        self.cycle()
        self.assertEqual(len(self.bills()), 1)
        self.assertEqual(sorted(r["bill_status"] for r in self.rows("SELECT bill_status FROM electricity_bill_extractions")), ["DUPLICATE", "NEEDS_REVIEW"])

    def test_a_corrupt_document_does_not_stop_the_other_bills(self):
        self.src.blobs[904] = (b"%PDF-1.4 garbage that is not a pdf", "broken.pdf", "application/pdf")
        self.src.atts.append(FakeSource.att(1003, "PO_HEADERS", 20, 5004, "broken.pdf", media=904, size=100))
        r = self.cycle()
        self.assertEqual(r["status"], "SUCCESS")
        self.assertEqual(len(self.bills()), 1)                                                       # the good bill was read
        self.assertIn("EXTRACTION_FAILED", [a["alert_type"] for a in self.rows("SELECT alert_type FROM electricity_alerts WHERE status = 'OPEN'")])
        self.assertGreaterEqual(len(self.rows("SELECT id FROM electricity_processing_errors")) + len(self.rows("SELECT id FROM electricity_alerts")), 1)

    def test_the_reading_budget_defers_the_rest_to_the_next_cycle(self):
        for i in range(3):
            self.src.blobs[950 + i] = (pdf_bytes(bill_no=f"B-77{i}", consumer=f"55555555555{i}"), f"x{i}.pdf", "application/pdf")
            self.src.atts.append(FakeSource.att(1100 + i, "PO_HEADERS", 20, 6000 + i, f"x{i}.pdf", media=950 + i, size=100))
        r = self.cycle(budget_minutes=0)
        self.assertEqual(r["processed"], 0)
        self.assertEqual(len(store.pending_attachment_ids(50, self.db)), 4)
        r = self.cycle(now=self.now + timedelta(hours=2))
        self.assertEqual(r["processed"], 4)

    def test_backfill_from_limits_which_old_pos_are_downloaded(self):
        with storage.session(self.db) as c:
            settings.set_many(c, {"backfill_from": "2026-09-15"})                                    # the fake POs were created on 2026-09-01
        r = self.cycle()
        self.assertEqual(r["downloaded"], 0)
        with storage.session(self.db) as c:
            settings.set_many(c, {"backfill_from": "2026-01-01"})
        self.assertEqual(self.cycle(now=self.now + timedelta(hours=2))["downloaded"], 1)


# ------------------------------------------------------------------------------------------- schedule, back-off, restart
class ScheduleTests(Env):
    def next_due(self):
        return datetime.fromisoformat(self.state("sched.next_due"))

    def test_next_scan_is_two_hours_after_the_start(self):
        self.cycle()
        self.assertEqual(self.next_due(), self.now + timedelta(hours=2))
        with storage.session(self.db) as c:
            self.assertEqual(settings.get(c, "scan_interval_minutes"), 120)                          # the business requirement

    def test_tick_runs_a_scan_only_when_one_is_due(self):
        self.assertIsNotNone(scheduler.tick(self.src, self.db, now=self.now, run_ocr=False, send_mail=False))            # first time: due at once
        self.assertIsNone(scheduler.tick(self.src, self.db, now=self.now + timedelta(minutes=119), run_ocr=False, send_mail=False))
        r = scheduler.tick(self.src, self.db, now=self.now + timedelta(minutes=120), run_ocr=False, send_mail=False)
        self.assertEqual(r["trigger"], "scheduler")
        self.assertEqual(len(self.rows("SELECT id FROM electricity_scan_runs")), 2)

    def test_the_interval_is_configurable(self):
        with storage.session(self.db) as c:
            settings.set_many(c, {"scan_interval_minutes": "30"})
        self.cycle()
        self.assertEqual(self.next_due(), self.now + timedelta(minutes=30))

    def test_the_tick_leaves_a_heartbeat_for_the_health_page(self):
        scheduler.tick(self.src, self.db, now=self.now, run_ocr=False, send_mail=False)
        self.assertEqual(self.state("sched.last_tick_at"), self.now.isoformat(sep=" "))

    def test_oracle_failure_backs_off_exponentially_then_recovers(self):
        self.src.fail = "ORA-12170: TNS:Connect timeout occurred"
        delays = []
        t = self.now
        for _ in range(4):
            r = self.cycle(now=t)
            self.assertEqual(r["status"], "FAILED")
            delays.append((self.next_due() - t).total_seconds() / 60)
            t = self.next_due()
        self.assertEqual(delays, [5, 10, 20, 40])
        self.assertEqual(self.state("sched.consecutive_failures"), "4")
        self.assertIn("ORA-12170", self.state("sched.last_error"))
        self.src.fail = None
        self.assertEqual(self.cycle(now=t)["status"], "SUCCESS")
        self.assertEqual(self.state("sched.consecutive_failures"), "0")
        self.assertEqual(self.next_due(), t + timedelta(hours=2))

    def test_backoff_never_exceeds_the_scan_interval(self):
        self.src.fail = "ORA-03113"
        t = self.now
        for _ in range(9):
            self.cycle(now=t)
            t = self.next_due()
        self.assertLessEqual((self.next_due() - t).total_seconds(), 120 * 60)
        self.cycle(now=t)
        self.assertLessEqual((self.next_due() - t).total_seconds() / 60, 120)

    def test_repeated_failures_raise_a_scheduler_alert_and_success_clears_it(self):
        self.src.fail = "ORA-12541: no listener"
        t = self.now
        for _ in range(3):
            self.cycle(now=t)
            t = self.next_due()
        al = self.rows("SELECT * FROM electricity_alerts WHERE alert_type = 'SCHEDULER_FAILURE' AND status = 'OPEN'")
        self.assertEqual(len(al), 1)
        self.assertEqual(al[0]["severity"], "CRITICAL")
        self.src.fail = None
        self.cycle(now=t)
        self.assertEqual(self.rows("SELECT status FROM electricity_alerts WHERE alert_type = 'SCHEDULER_FAILURE'")[0]["status"], "RESOLVED")

    def test_a_failed_scan_is_recorded_with_its_error(self):
        self.src.fail = "ORA-01017: invalid username/password"
        self.cycle()
        run = self.rows("SELECT * FROM electricity_scan_runs")[0]
        self.assertEqual(run["status"], "FAILED")
        self.assertIn("ORA-01017", run["message"])
        self.assertEqual(len(self.rows("SELECT id FROM electricity_processing_errors WHERE stage = 'SCAN'")), 1)

    def test_a_restart_resumes_from_the_stored_schedule_not_from_memory(self):
        self.cycle()
        due = self.next_due()
        # "restart": nothing in memory survives; only the database does
        self.assertFalse(self.is_due(due - timedelta(seconds=1)))
        self.assertTrue(self.is_due(due))
        self.assertTrue(self.is_due(due + timedelta(days=3)))                                       # a long outage => run at once on start-up

    def is_due(self, when):
        with storage.session(self.db) as c:
            return scheduler.is_due(c, when)

    def test_a_crash_leaves_a_stale_lock_and_a_running_row_that_recovery_cleans(self):
        with storage.session(self.db) as c:
            c.execute("INSERT INTO electricity_scan_runs (mode, trigger, started_at, status) VALUES ('full', 'scheduler', '2026-10-13 08:00:00', 'RUNNING')")
            c.execute("INSERT INTO electricity_job_locks VALUES ('scan-cycle', 'dead:1:1', '2020-01-01 00:00:00', '2020-01-01 00:00:00', '2020-01-01 00:30:00')")
            c.execute("INSERT INTO electricity_jobs (kind, ref_id, status, claimed_by, claimed_at, created_at) VALUES ('PROCESS_UPLOAD', 7, 'RUNNING', 'dead', '2020-01-01 00:00:00', 'x')")
        info = scheduler.recover(self.db)
        self.assertEqual((info["stale_locks"], info["interrupted_runs"], info["requeued_jobs"]), (1, 1, 1))
        self.assertEqual(self.rows("SELECT status FROM electricity_scan_runs")[0]["status"], "FAILED")
        self.assertEqual(self.rows("SELECT status FROM electricity_jobs")[0]["status"], "QUEUED")
        self.assertEqual(self.cycle()["status"], "SUCCESS")                                           # and the scanner works again

    def test_the_first_cycle_of_the_day_is_a_full_reconciliation(self):
        self.assertEqual(self.cycle()["mode"], "full")
        self.assertEqual(self.cycle(now=self.now + timedelta(hours=2))["mode"], "incremental")
        nxt_day = (self.now + timedelta(days=1)).replace(hour=3)
        self.assertEqual(self.cycle(now=nxt_day)["mode"], "full")
        self.assertEqual(self.cycle(now=nxt_day + timedelta(hours=2))["mode"], "incremental")

    def test_scheduler_can_be_paused(self):
        with storage.session(self.db) as c:
            settings.set_many(c, {"scheduler_enabled": "false"})
        self.assertIsNone(scheduler.tick(self.src, self.db, now=self.now, run_ocr=False, send_mail=False))
        self.assertEqual(len(self.rows("SELECT id FROM electricity_scan_runs")), 0)


# ------------------------------------------------------------------------------------------- downloads
class DownloadRetryTests(Env):
    def test_an_interrupted_download_is_retried_later_with_back_off_and_never_marked_done(self):
        del self.src.blobs[900]                                                                       # Oracle cannot deliver the LOB
        self.cycle()
        a = self.rows("SELECT * FROM electricity_attachments")[0]
        self.assertEqual((a["fetch_status"], a["retry_count"]), ("FAILED", 1))
        self.assertIsNone(a["sha256"])
        self.assertEqual(self.bills(), [])
        self.assertIn("DOWNLOAD_FAILURE", [x["alert_type"] for x in self.rows("SELECT alert_type FROM electricity_alerts WHERE status = 'OPEN'")])
        due = datetime.fromisoformat(a["next_retry_at"])
        self.assertEqual(due, self.now + timedelta(minutes=10))
        self.cycle(now=self.now + timedelta(minutes=30))                                              # next cycle: retry is due (10 min) but the LOB is still missing
        self.assertEqual(self.rows("SELECT retry_count FROM electricity_attachments")[0]["retry_count"], 2)
        self.src.blobs[900] = (pdf_bytes(), "bill.pdf", "application/pdf")
        # not retried before its time (20 more minutes after the 2nd failure)
        self.cycle(now=self.now + timedelta(minutes=31))
        self.assertEqual(self.rows("SELECT fetch_status FROM electricity_attachments")[0]["fetch_status"], "FAILED")
        self.cycle(now=self.now + timedelta(minutes=60))
        self.assertEqual(self.rows("SELECT fetch_status FROM electricity_attachments")[0]["fetch_status"], "DOWNLOADED")
        self.assertEqual(len(self.bills()), 1)
        self.assertEqual([a for a in self.rows("SELECT status FROM electricity_alerts WHERE alert_type = 'DOWNLOAD_FAILURE'")][0]["status"], "RESOLVED")

    def test_retries_stop_after_the_maximum_and_the_alert_becomes_critical(self):
        del self.src.blobs[900]
        t = self.now
        for i in range(discovery.MAX_DOWNLOAD_RETRIES + 2):
            self.cycle(now=t)
            t += timedelta(hours=24)
        a = self.rows("SELECT retry_count FROM electricity_attachments")[0]
        self.assertEqual(a["retry_count"], discovery.MAX_DOWNLOAD_RETRIES)
        self.assertEqual(self.rows("SELECT severity FROM electricity_alerts WHERE alert_type = 'DOWNLOAD_FAILURE'")[0]["severity"], "CRITICAL")


# ------------------------------------------------------------------------------------------- job queue
class JobTests(Env):
    def test_enqueue_is_idempotent_while_a_job_is_active(self):
        with storage.session(self.db) as c:
            a = jobs.enqueue(c, "PROCESS_UPLOAD", 5)
            b = jobs.enqueue(c, "PROCESS_UPLOAD", 5)
        self.assertEqual(a, b)
        self.assertEqual(len(self.rows("SELECT id FROM electricity_jobs")), 1)

    def test_many_workers_never_take_the_same_job_twice(self):
        with storage.session(self.db) as c:
            for i in range(40):
                jobs.enqueue(c, "PROCESS_UPLOAD", 1000 + i)
        claimed, lock = [], threading.Lock()

        def worker(name):
            while True:
                with storage.session(self.db) as c:
                    j = jobs.claim_next(c, name)
                if j is None:
                    return
                with lock:
                    claimed.append(j["id"])
        ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(claimed), sorted(set(claimed)))
        self.assertEqual(len(claimed), 40)

    def test_a_failing_job_is_retried_with_back_off_then_failed_and_logged(self):
        with storage.session(self.db) as c:
            jid = jobs.enqueue(c, "PROCESS_UPLOAD", 999, max_attempts=2)            # no such upload => ValueError every time
        self.assertTrue(jobs.work_once("t", self.db, run_ocr=False))
        j = self.rows("SELECT * FROM electricity_jobs")[0]
        self.assertEqual((j["status"], j["attempts"]), ("QUEUED", 1))
        self.assertIsNotNone(j["next_attempt_at"])
        self.assertFalse(jobs.work_once("t", self.db, run_ocr=False, now=datetime.now()))          # not yet: back-off
        self.assertTrue(jobs.work_once("t", self.db, run_ocr=False, now=datetime.now() + timedelta(hours=1)))
        j = self.rows("SELECT * FROM electricity_jobs")[0]
        self.assertEqual((j["status"], j["attempts"]), ("FAILED", 2))
        self.assertEqual(len(self.rows("SELECT id FROM electricity_processing_errors WHERE stage = 'JOB'")), 1)

    def test_retry_extraction_archives_the_old_reading_and_keeps_corrections(self):
        self.cycle()
        ext = self.rows("SELECT id FROM electricity_bill_extractions")[0]["id"]
        store.apply_correction(ext, "kwh", "91000", "reviewer", db_path=self.db)
        with storage.session(self.db) as c:
            jobs.enqueue(c, "RETRY_EXTRACTION", ext)
        self.assertTrue(jobs.work_once("w", self.db, run_ocr=False))
        rows = self.rows("SELECT id, archived, reader_version FROM electricity_bill_extractions ORDER BY id")
        self.assertEqual([r["archived"] for r in rows], [1, 0])
        self.assertIn("#archived", rows[0]["reader_version"])
        self.assertEqual(len(self.rows("SELECT id FROM electricity_manual_corrections WHERE extraction_id = ?", ext)), 1)         # the old correction is still on file
        self.assertEqual(len(self.bills()), 1)                                                         # one live bill
        self.assertEqual(self.bills()[0]["kwh"], 90000.0)                                              # the fresh reading (the earlier correction belongs to the archived one)
        self.assertEqual(len(self.rows("SELECT id FROM electricity_audit_logs WHERE action = 'RETRY_EXTRACTION'")), 1)

    def test_outdated_readings_are_re_read_but_corrected_ones_are_never_replaced(self):
        self.cycle()
        ext = self.rows("SELECT id FROM electricity_bill_extractions")[0]["id"]
        with storage.session(self.db) as c:
            c.execute("UPDATE electricity_bill_extractions SET reader_version = 'reader-1.0' WHERE id = ?", (ext,))
        self.assertEqual(store.outdated_extraction_ids(10, self.db), [ext])
        store.apply_correction(ext, "kwh", "91000", "reviewer", db_path=self.db)
        self.assertEqual(store.outdated_extraction_ids(10, self.db), [])                              # a person's work is protected


# ------------------------------------------------------------------------------------------- manual upload
class UploadTests(Env):
    def save(self, data, name="bill.pdf", plant="BG2", user="plant1", **kw):
        with storage.session(self.db) as c:
            c.execute("INSERT OR IGNORE INTO electricity_pos (po_header_id, po_number, plant_code, plant_name, first_seen, last_seen) VALUES (77, 'P77', 'BG2', 'Bangalore-2', 'x', 'x')")
            return uploads.save_upload(c, data, name, plant, user, **kw)

    def test_upload_is_stored_unchanged_queued_read_and_linked_to_the_plant(self):
        data = pdf_bytes(bill_no="U-0001")
        r = self.save(data, consumer_number="123456789012", billing_month="2026-09")
        self.assertFalse(r["duplicate"])
        u = self.rows("SELECT * FROM electricity_uploads")[0]
        self.assertEqual((u["status"], u["uploaded_by"], u["plant_code"]), ("QUEUED", "plant1", "BG2"))
        self.assertEqual(pathlib.Path(config.STORAGE_DIR, u["local_path"]).read_bytes(), data)         # original byte for byte
        self.assertTrue(jobs.work_once("w", self.db, run_ocr=False))
        u = self.rows("SELECT * FROM electricity_uploads")[0]
        self.assertEqual(u["status"], "PROCESSED")
        b = self.bills()[0]
        self.assertEqual((b["source"], b["plant_code"], b["upload_id"], b["bill_number"]), ("MANUAL_UPLOAD", "BG2", u["id"], "U-0001"))
        self.assertEqual(len(self.rows("SELECT id FROM electricity_audit_logs WHERE action = 'UPLOAD_BILL'")), 1)

    def test_the_same_file_uploaded_twice_is_not_stored_or_counted_twice(self):
        data = pdf_bytes(bill_no="U-0002")
        a = self.save(data)
        jobs.drain("w", self.db, run_ocr=False)
        b = self.save(data)
        self.assertTrue(b["duplicate"])
        self.assertEqual(b["upload_id"], a["upload_id"])
        self.assertEqual(len(self.rows("SELECT id FROM electricity_uploads")), 1)
        self.assertEqual(len(self.bills()), 1)

    def test_an_upload_later_found_in_oracle_is_linked_not_double_counted(self):
        data = self.src.blobs[900][0]                            # byte-identical to the file that Oracle will deliver
        self.save(data, plant="BG2")
        jobs.drain("w", self.db, run_ocr=False)
        self.cycle()                                                                     # Oracle now delivers the same file on PO 10
        self.assertEqual(len(self.bills()), 1)
        u = self.rows("SELECT * FROM electricity_uploads")[0]
        self.assertIsNotNone(u["linked_attachment_id"])
        self.assertIn("Oracle", u["link_note"])
        b = self.bills()[0]
        self.assertEqual(b["source"], "MANUAL_UPLOAD")                                   # the bill with the person's work survives ...
        self.assertEqual(self.rows("SELECT bill_status FROM electricity_bill_extractions WHERE attachment_id IS NOT NULL")[0]["bill_status"], "DUPLICATE")      # ... and Oracle's copy is the duplicate

    def test_the_form_hints_cross_check_the_bill(self):
        self.save(pdf_bytes(bill_no="U-0003"), consumer_number="999999999999", billing_month="2026-01")
        jobs.drain("w", self.db, run_ocr=False)
        import json
        flags = json.loads(self.rows("SELECT flags_json FROM electricity_bill_extractions")[0]["flags_json"])
        self.assertTrue(any("differs from the PO header" in f for f in flags))                      # the typed consumer number disagrees with the bill
        self.assertTrue(any("does not match the PO period" in f for f in flags))

    def test_validation_rejects_bad_input(self):
        good = pdf_bytes()
        for kw, frag in (({"plant": ""}, "plant"), ({"plant": "NOPE"}, "plant"), ({"billing_month": "2026-13"}, "month"), ({"billing_month": "2999-01"}, "future"),
                         ({"discom_code": "FAKEBOARD"}, "board"), ({"po_number": "P1; DROP"}, "po number")):
            with self.assertRaises(uploads.UploadError, msg=str(kw)) as cm:
                self.save(good, **kw)
            self.assertIn(frag, str(cm.exception).lower())

    def test_file_validation_decides_by_content(self):
        for data, frag in ((b"", "empty"), (b"MZ\x90\x00 this is an exe", "only pdf"), (b"<html><script>alert(1)</script></html>", "only pdf"),
                           (b"%PDF-1.4\n1 0 obj << /Type /Catalog /OpenAction << /S /JavaScript /JS (app.alert(1)) >> >> endobj", "active content"),
                           (b"%PDF-1.4\n1 0 obj << /Launch << /F (cmd.exe) >> >> endobj", "active content")):
            with self.assertRaises(uploads.UploadError, msg=data[:20]) as cm:
                uploads.validate(data)
            self.assertIn(frag, str(cm.exception).lower())
        self.assertEqual(uploads.validate(pdf_bytes()), "pdf")

    def test_a_file_named_pdf_that_is_really_an_executable_is_refused(self):
        with self.assertRaises(uploads.UploadError):
            self.save(b"MZ\x90\x00\x03" + b"\x00" * 200, name="bill.pdf")

    def test_images_are_accepted_and_decompression_bombs_refused(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (80, 60), "white").save(buf, "PNG")
        self.assertEqual(uploads.validate(buf.getvalue()), "png")
        old = uploads.MAX_IMAGE_PIXELS
        uploads.MAX_IMAGE_PIXELS = 1000
        try:
            with self.assertRaises(uploads.UploadError):
                uploads.validate(buf.getvalue())
        finally:
            uploads.MAX_IMAGE_PIXELS = old

    def test_oversized_files_are_refused(self):
        old = uploads.MAX_UPLOAD_BYTES
        uploads.MAX_UPLOAD_BYTES = 100
        try:
            with self.assertRaises(uploads.UploadError):
                uploads.validate(pdf_bytes())
        finally:
            uploads.MAX_UPLOAD_BYTES = old

    def test_a_hostile_file_name_cannot_escape_the_store(self):
        self.save(pdf_bytes(bill_no="U-0004"), name="..\\..\\..\\windows\\evil<script>.pdf")
        u = self.rows("SELECT * FROM electricity_uploads")[0]
        self.assertTrue(u["local_path"].startswith("uploads"))
        self.assertNotIn("..", u["local_path"])
        self.assertNotIn("<", u["original_name"])

    def test_a_damaged_upload_fails_cleanly_and_can_be_retried(self):
        broken = b"%PDF-1.4\n" + b"\x00" * 50                                           # looks like a PDF, is not readable
        self.save(broken, name="broken.pdf")
        jobs.drain("w", self.db, run_ocr=False)
        ext = self.rows("SELECT bill_status, upload_id FROM electricity_bill_extractions")[0]
        self.assertIn(ext["bill_status"], ("FAILED", "UNSUPPORTED"))
        self.assertEqual(len(self.bills()), 0)


# ------------------------------------------------------------------------------------------- repeated alerts and mail over several cycles
class RepeatedAlertTests(Env):
    def test_a_standing_problem_alerts_once_over_many_scans_and_is_emailed_once(self):
        self.src.blobs[900] = (pdf_bytes(bill_no="B-1001", pf_penalty=5000.0), "bill.pdf", "application/pdf")
        with storage.session(self.db) as c:
            settings.set_many(c, {"alert_email_enabled": "true", "alert_email_to": "ops@x.in", "alert_email_min_severity": "WARNING"})
        sent = []
        smtp = SmtpSettings("h", 587, "s@x.in", "pw", True)
        for i in range(5):
            scheduler.run_cycle(self.src, db_path=self.db, now=self.now + timedelta(hours=2 * i), run_ocr=False, send_mail=True,
                                mail_transport=lambda s, m, r: sent.append(r), smtp=smtp)
        self.assertEqual(len([a for a in self.rows("SELECT * FROM electricity_alerts WHERE alert_type = 'PF_PENALTY_ACTUAL'")]), 1)
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()

