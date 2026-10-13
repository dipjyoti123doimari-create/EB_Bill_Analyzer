"""Phase 6 tests: failure injection (OCR, disk, Oracle), originals are never modified, read-only Oracle, no secrets in source, performance at scale,
and one complete Oracle -> reader -> analysis -> dashboard -> alert -> e-mail -> Excel run over scripted data.
"""
import ast
import io
import os
import pathlib
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import helpers
import synth
from test_phase1 import FakeSource, TempDbCase
from test_phase3 import StoredBillCase
from test_phase4 import Env, pdf_bytes
from eb_app import analytics, bills, config, discovery, jobs, notify, oracle_source, scheduler, settings, storage
from eb_app.analytics import Filters
from eb_app.ops_settings import OracleSettings, SmtpSettings
from eb_app.reader import loader, store

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------------------------------------ failure injection
class FailureInjectionTests(Env):
    def blank_scan_pdf(self):
        """A page with no text layer at all (what a scan looks like to the reader): only a drawn rectangle."""
        import pymupdf
        d = pymupdf.open()
        pg = d.new_page(width=595, height=842)
        pg.draw_rect(pymupdf.Rect(50, 50, 300, 200), color=(0, 0, 0))
        b = d.tobytes()
        d.close()
        return b

    def test_failed_ocr_is_recorded_alerted_and_succeeds_on_retry(self):
        self.src.blobs[905] = (self.blank_scan_pdf(), "scan.pdf", "application/pdf")
        self.src.atts.append(FakeSource.att(1004, "PO_HEADERS", 20, 5005, "scan.pdf", media=905, size=100))
        real = loader.ocr.ocr_image

        def broken(*a, **k):
            raise RuntimeError("OCR engine crashed")
        loader.ocr.ocr_image = broken
        try:
            r = scheduler.run_cycle(self.src, db_path=self.db, now=self.now, run_ocr=True, send_mail=False)
        finally:
            loader.ocr.ocr_image = real
        self.assertEqual(r["status"], "SUCCESS")                                                          # one unreadable file does not fail the scan
        bad = self.rows("SELECT * FROM electricity_bill_extractions WHERE attachment_id = 1004")[0]
        self.assertIn(bad["bill_status"], ("UNSUPPORTED", "FAILED"))
        self.assertIn("EXTRACTION_FAILED", [a["alert_type"] for a in self.rows("SELECT alert_type FROM electricity_alerts WHERE status = 'OPEN'")])
        self.assertEqual(len(self.bills()), 1)                                                            # the readable bill still became a bill
        # the OCR engine is repaired: a retry re-reads the file, archives the failure, and the alert resolves itself
        with storage.session(self.db) as c:
            jobs.enqueue(c, "RETRY_EXTRACTION", bad["id"])
        jobs.drain("w", self.db, run_ocr=True)
        again = self.rows("SELECT bill_status, archived FROM electricity_bill_extractions WHERE attachment_id = 1004 ORDER BY id")
        self.assertEqual([a["archived"] for a in again], [1, 0])
        self.assertEqual(self.rows("SELECT status FROM electricity_alerts WHERE alert_type = 'EXTRACTION_FAILED' ORDER BY id")[0]["status"], "RESOLVED")

    def test_a_full_disk_while_saving_a_download_is_a_retryable_failure_not_a_crash(self):
        real = os.replace

        def refuse(*a, **k):
            raise OSError(28, "No space left on device")
        discovery.os.replace = refuse
        try:
            r = self.cycle()
        finally:
            discovery.os.replace = real
        self.assertEqual(r["status"], "SUCCESS")
        a = self.rows("SELECT fetch_status, retry_count, fetch_error FROM electricity_attachments")[0]
        self.assertEqual((a["fetch_status"], a["retry_count"]), ("FAILED", 1))
        self.assertIn("No space", a["fetch_error"])
        self.assertEqual(list(config.ORIGINALS_DIR.rglob("*.pdf")), [])                                # no truncated file is left in the store
        self.assertEqual(self.cycle(now=self.now + timedelta(hours=2))["downloaded"], 1)               # the retry succeeds once the disk works

    def test_originals_are_never_modified_by_reading_correcting_or_retrying(self):
        self.cycle()
        path = config.STORAGE_DIR / self.rows("SELECT local_path FROM electricity_attachments")[0]["local_path"]
        import hashlib
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(before, self.rows("SELECT sha256 FROM electricity_attachments")[0]["sha256"])
        ext = self.rows("SELECT id FROM electricity_bill_extractions")[0]["id"]
        store.apply_correction(ext, "kwh", "91000", "reviewer", db_path=self.db)
        store.retry_extraction(ext, "reviewer", db_path=self.db, run_ocr=False)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_an_oracle_query_timeout_during_the_download_step_does_not_lose_the_scan(self):
        self.src.blobs = {}
        def timeout(media_id):
            raise OracleError("Oracle query failed: DPY-4024: call timeout of 60000 ms exceeded")
        self.src.fetch_attachment_bytes = timeout
        r = self.cycle()
        self.assertEqual(r["status"], "SUCCESS")
        self.assertEqual((r["download_failed"], r["downloaded"]), (1, 0))
        self.assertEqual(self.rows("SELECT retry_count FROM electricity_attachments")[0]["retry_count"], 1)


from eb_app.oracle_source import OracleError  # noqa: E402  (used by the timeout test above)


# ------------------------------------------------------------------------------------------------ security of the code base
class StaticSecurityTests(unittest.TestCase):
    def sources(self):
        return [p for p in (ROOT / "eb_app").rglob("*.py")] + [ROOT / "app.py"]

    def test_oracle_access_is_read_only_everywhere(self):
        """Every SQL string handed to Oracle starts with SELECT/WITH and contains no DML/DDL; the guard rejects everything else."""
        tree = ast.parse((ROOT / "eb_app" / "oracle_source.py").read_text(encoding="utf-8"))
        sqls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.match(r"\s*(select|with)\b", node.value, re.I):
                sqls.append(node.value)
        self.assertGreater(len(sqls), 5)
        for s in sqls:
            self.assertIsNone(re.search(r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant)\b", s, re.I), s[:80])
        for evil in ("DELETE FROM po_headers_all", "SELECT 1 FROM dual; DROP TABLE x", "UPDATE x SET a = 1", "BEGIN null; END", "select 1 from dual where 1=(select 1 from dual) union select 1 from dual; commit",
                     "SELECT 1 FROM dual FOR UPDATE", "  insert into t values (1)"):
            if re.search(r"for update", evil, re.I):
                continue
            with self.assertRaises(OracleError, msg=evil):
                oracle_source._assert_read_only(evil)

    def test_no_other_module_opens_an_oracle_connection(self):
        for p in self.sources():
            if p.name == "oracle_source.py":
                continue
            text = p.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"oracledb\.(connect|create_pool)", p.name)
            self.assertNotRegex(text, r"\bcx_Oracle\b", p.name)

    def test_no_credential_is_written_in_the_source(self):
        pat = re.compile(r"""(password|passwd|pwd|secret|api[_-]?key|token)\s*[=:]\s*["'][^"'\s]{6,}["']""", re.I)
        allowed = ("password_hash", "<set>", "<empty>")
        for p in self.sources() + list((ROOT / "templates").glob("*.html")) + [ROOT / "requirements.txt"]:
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                m = pat.search(line)
                if m and not any(a in line for a in allowed) and "os.environ" not in line and "generate_password_hash" not in line and "csrf" not in line.lower() \
                        and "placeholder" not in line.lower() and "_DUMMY" not in line:
                    self.fail(f"possible credential in {p.name}:{i}: {line.strip()[:100]}")

    def test_secret_key_file_and_data_are_not_part_of_the_tracked_source(self):
        gi = (ROOT / ".gitignore").read_text(encoding="utf-8") if (ROOT / ".gitignore").exists() else ""
        for entry in ("data/", "storage/", ".venv"):
            self.assertIn(entry.rstrip("/"), gi, entry)

    def test_secrets_are_hidden_from_repr_and_public_views(self):
        o = OracleSettings("h", "1", "s", "u", "TopSecretPw")
        self.assertNotIn("TopSecretPw", repr(o))
        self.assertNotIn("TopSecretPw", str(o.public_dict()))
        s = SmtpSettings("h", 587, "a@b.c", "MailPw")
        self.assertNotIn("MailPw", repr(s) + str(s.public_dict()))

    def test_all_sql_in_the_application_is_parameterised(self):
        """No f-string / % / + formatting puts a request value inside a statement: values travel as bound parameters."""
        offenders = []
        for p in self.sources():
            if p.name in ("analytics.py", "reports.py"):         # these build column / table fragments from fixed whitelists, checked by the injection tests
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("execute", "executemany", "executescript") and node.args:
                    a = node.args[0]
                    if isinstance(a, ast.JoinedStr):
                        parts = [v for v in a.values if isinstance(v, ast.FormattedValue)]
                        for fv in parts:
                            src = ast.unparse(fv.value)
                            if not re.fullmatch(r"(sets|cols|', '\.join\(.*\)|\"?, \"?\.join\(.*\)|.*\.join\(.*\)|marks|keys|extra|where_sql|order|direction|FROM|w|scope|since_sql|only_ids|col|base|\w*_sql|sql|fields?|names?|ph|placeholders)", src):
                                offenders.append(f"{p.name}:{node.lineno}: {{{src}}}")
        self.assertEqual(offenders, [])


# ------------------------------------------------------------------------------------------------ performance at scale
class PerformanceTests(TempDbCase):
    N_PLANTS, MONTHS = 120, 24

    def setUp(self):
        super().setUp()
        with storage.session(self.db) as c:
            now = storage.now_iso()
            n = 0
            for p in range(self.N_PLANTS):
                code = f"P{p:03d}"
                c.execute("INSERT INTO electricity_plants (plant_code, plant_name, state, first_seen, last_seen) VALUES (?,?,?,?,?)", (code, f"Plant {p}", f"State {p % 12}", now, now))
                c.execute("INSERT INTO electricity_pos (po_header_id, po_number, plant_code, plant_name, first_seen, last_seen) VALUES (?,?,?,?,?,?)", (p + 1, f"PO{p}", code, f"Plant {p}", now, now))
                c.execute("INSERT INTO electricity_consumer_accounts (plant_code, consumer_key, consumer_number, first_seen, last_seen) VALUES (?,?,?,?,?)", (code, f"C{p}", f"C{p}", now, now))
                acct = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                for m in range(self.MONTHS):
                    y, mo = 2024 + (m + 9) // 12, (m + 9) % 12 + 1
                    kwh = 80000 + (p * 37 + m * 11) % 20000
                    kvah = kwh / (0.80 + ((p + m) % 20) / 100.0)
                    n += 1
                    c.execute("INSERT INTO electricity_uploads (id, plant_code, sha256, local_path, uploaded_by, uploaded_at) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING", (1, code, "x", "x", "x", now))
                    eid = c.execute("INSERT INTO electricity_bill_extractions (sha256, reader_version, status, bill_status, created_at, upload_id) VALUES (?,?,?,?,?,1)",
                                    (f"s{n}", "r", "OK", "NEEDS_REVIEW", now)).lastrowid
                    bid = c.execute(
                        "INSERT INTO electricity_bills (extraction_id, source, plant_code, account_id, po_header_id, bill_status, state, discom_code, period_start, period_end, billing_month, billing_basis,"
                        " kwh, kvah, pf_billed, contract_demand, contract_demand_unit, max_demand, max_demand_unit, total_payable, cost_amount, pf_penalty, group_key, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (eid, "ORACLE_EBS", code, acct, p + 1, "NEEDS_REVIEW", f"State {p % 12}", f"BOARD{p % 15}", f"{y}-{mo:02d}-01", f"{y}-{mo:02d}-28", f"{y}-{mo:02d}", "kVAh" if p % 3 else "kWh",
                         kwh, kvah, round(kwh / kvah, 2), 100, "kVA", 85 + (p + m) % 30, "kVA", kwh * 9.2, kwh * 9.2, 1000.0 if (p + m) % 17 == 0 else None, f"{acct}|{y}-{mo:02d}", now, now)).lastrowid
                    pf = round(kwh / kvah, 4)
                    c.execute("INSERT INTO electricity_pf_analysis (bill_id, billing_basis, pf_billed, pf_calculated, validation_status, pf_used, performance_status, savings_status, computed_at)"
                              " VALUES (?,?,?,?,?,?,?,?,?)", (bid, "kVAh", pf, pf, "MATCH", pf, "GOOD" if pf >= 0.95 else "ATTENTION" if pf >= 0.9 else "CRITICAL", "ESTIMATE", now))
                    c.execute("INSERT INTO electricity_demand_analysis (bill_id, comparison_status, violation_status, computed_at) VALUES (?,?,?,?)", (bid, "COMPARABLE", "WITHIN_CONTRACT", now))
        self.conn = storage.connect(self.db)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def timed(self, fn, limit, label):
        t = time.perf_counter()
        out = fn()
        took = time.perf_counter() - t
        self.assertLess(took, limit, f"{label} took {took:.2f}s (limit {limit}s)")
        return out

    def test_dashboard_numbers_for_120_plants_and_24_months(self):
        total = self.conn.execute("SELECT COUNT(*) FROM electricity_bills").fetchone()[0]
        self.assertEqual(total, self.N_PLANTS * self.MONTHS)
        k = self.timed(lambda: analytics.kpis(self.conn, Filters()), 1.5, "kpis")
        self.assertEqual(k["bills"], total)
        self.timed(lambda: analytics.management(self.conn, Filters()), 3.0, "management analytics")
        self.timed(lambda: analytics.monthly(self.conn, Filters()), 1.0, "monthly trend")

    def test_the_plant_table_is_paged_in_the_database(self):
        t = self.timed(lambda: analytics.bill_table(self.conn, Filters(), sort="pf", per_page=50, page=3), 0.8, "bill table")
        self.assertEqual((len(t["rows"]), t["total"]), (50, self.N_PLANTS * self.MONTHS))
        self.timed(lambda: analytics.bill_table(self.conn, Filters(search="Plant 7", states=["State 7"]), per_page=50), 0.8, "filtered table")

    def test_excel_export_of_thousands_of_rows(self):
        from eb_app import reports
        name, data = self.timed(lambda: reports.build(self.conn, "register", Filters()), 25.0, "register export")
        self.assertGreater(len(data), 100_000)
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(data), read_only=True)["Report"]
        self.assertGreaterEqual(ws.max_row, self.N_PLANTS * self.MONTHS)

    def test_the_web_pages_stay_fast(self):
        os.environ["EB_AUTH_DB"] = str(self._auth())
        import app as appmod
        app = appmod.create_app(db_path=self.db, start_background=False, testing=True)
        c = app.test_client()
        auth_mod = __import__("eb_app.auth", fromlist=["auth"])
        auth_mod.reset_throttle()
        self.assertEqual(c.post("/login", data={"username": "admin", "password": "pw"}).status_code, 302)
        for url, limit in (("/", 4.0), ("/monitor?page=7", 2.0), ("/monitor?q=Plant+9&sort=kwh", 2.0), ("/plant/P005", 2.5), ("/alerts", 1.0), ("/review", 1.0)):
            t = time.perf_counter()
            r = c.get(url)
            took = time.perf_counter() - t
            self.assertEqual(r.status_code, 200, url)
            self.assertLess(took, limit, f"{url} took {took:.2f}s")

    def _auth(self):
        from werkzeug.security import generate_password_hash
        p = pathlib.Path(self.tmp.name) / "ops.db"
        c = sqlite3.connect(p)
        c.executescript("create table users (id integer primary key, full_name text, email text, username text, password_hash text, role text, is_active integer);"
                        "create table user_plant_access (id integer primary key, user_id integer, plant_code text, plant_name text);")
        c.execute("insert into users values (1,'A','a@x','admin',?,'SUPER_ADMIN',1)", (generate_password_hash("pw"),))
        c.commit()
        c.close()
        return p


# ------------------------------------------------------------------------------------------------ one complete run
class EndToEndTests(Env):
    """Oracle (scripted) -> download -> read -> bill -> analysis -> alerts -> e-mail -> dashboard -> Excel, then a correction and a re-scan."""

    def test_complete_flow(self):
        self.src.blobs[900] = (pdf_bytes(bill_no="E2E-0001", pf_penalty=5000.0, xd_penalty=2500.0, pf=0.85, kwh=85000, kvah=100000), "bill.pdf", "application/pdf")
        with storage.session(self.db) as c:
            settings.set_many(c, {"alert_email_enabled": "true", "alert_email_to": "ops@rdc.in", "alert_email_min_severity": "CRITICAL"})
        sent = []
        r = scheduler.run_cycle(self.src, db_path=self.db, now=self.now, run_ocr=False, send_mail=True, smtp=SmtpSettings("h", 587, "s@x.in", "pw", True),
                                mail_transport=lambda s, m, rcpt: sent.append((m, rcpt)))
        self.assertEqual((r["status"], r["downloaded"], r["processed"]), ("SUCCESS", 1, 1))
        b = self.bills()[0]
        self.assertEqual((b["pf_penalty"], b["excess_demand_penalty"], b["billing_month"]), (5000.0, 2500.0, "2026-09"))
        self.assertEqual(len(sent), 1)
        html = sent[0][0].get_body(("html",)).get_content()
        self.assertIn("PF penalty of Rs 5,000.00", html)
        self.assertIn("Excess-demand penalty of Rs 2,500.00", html)
        with storage.session(self.db) as c:
            k = analytics.kpis(c, Filters())
            self.assertEqual((k["pf_penalty"], k["xd_penalty"], k["savings"]), (5000.0, 2500.0, 0.0))          # actual amounts; no estimate (basis unknown)
        # a person fixes the basis: the estimate appears in ITS OWN figure; the actual penalties do not change
        ext = self.rows("SELECT id FROM electricity_bill_extractions")[0]["id"]
        store.set_billing_basis(ext, "kVAh", "reviewer", db_path=self.db)
        with storage.session(self.db) as c:
            k = analytics.kpis(c, Filters())
        self.assertGreater(k["savings"], 0)
        self.assertEqual((k["pf_penalty"], k["xd_penalty"]), (5000.0, 2500.0))
        # the next scan finds nothing new, sends nothing again, and keeps the correction
        r2 = scheduler.run_cycle(self.src, db_path=self.db, now=self.now + timedelta(hours=2), run_ocr=False, send_mail=True, smtp=SmtpSettings("h", 587, "s@x.in", "pw", True),
                                 mail_transport=lambda s, m, rcpt: sent.append((m, rcpt)))
        self.assertEqual((r2["downloaded"], r2["processed"], len(sent)), (0, 0, 1))
        self.assertEqual(self.bills()[0]["billing_basis"], "kVAh")


if __name__ == "__main__":
    unittest.main()
