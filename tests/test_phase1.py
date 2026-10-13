"""Phase 1 tests: rules, read-only guard, discovery logic (fake Oracle) and a live read-only smoke test.

Run:  python -m unittest discover -s tests -v
"""

import os
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eb_app import config, discovery, storage
from eb_app.identification import RuleSet
from eb_app.ops_settings import OracleSettings, load_oracle_settings
from eb_app.oracle_source import OracleError, OracleSource, _assert_read_only


# ----------------------------------------------------------------------------- fake Oracle
class FakeSource:
    """Same interface as OracleSource.* used by discovery, with scripted data."""

    def __init__(self):
        self.items = {"EXP00084": [111]}
        self.lines = [
            {"po_line_id": 1, "po_header_id": 10, "line_num": 1, "item_description": "Electricity charges", "item_id": 111},
            {"po_line_id": 2, "po_header_id": 20, "line_num": 1, "item_description": "Electricity charges", "item_id": 111},
            {"po_line_id": 3, "po_header_id": 30, "line_num": 1, "item_description": "Demand Draft", "item_id": 111},
        ]
        self.headers = [
            self.hdr(10, "P10", "BESCOM", "BG2", "Banglore-2", attr3="278589111475"),
            self.hdr(20, "P20", "MSEDCL", "PU2", "Pune-2", attr3="----"),
            self.hdr(30, "P30", "HDFC Bank Ltd- Purchase of Demand Draft", "HY1", "Hyderabad-1"),
        ]
        self.atts = [self.att(1000, "PO_HEADERS", 10, 5001, "bill.pdf", media=900, size=1234)]
        self.blobs = {900: (b"%PDF-1.4 fake", "bill.pdf", "application/pdf")}
        self.calls = []
        self.fail = None

    @staticmethod
    def hdr(hid, num, supplier, plant, plant_name, attr3=None):
        return {"po_header_id": hid, "po_number": num, "org_id": 102, "vendor_id": hid, "supplier_name": supplier,
                "supplier_code": "S" + num, "vendor_site_id": 1, "creation_date": datetime(2026, 9, 1, 10, 0, 0),
                "last_update_date": datetime(2026, 9, 2), "authorization_status": "APPROVED", "cancel_flag": "N",
                "closed_code": None, "type_lookup_code": "STANDARD", "comments": "PO FOR ELECTRICITY BILL",
                "attribute1": "2026/08/01 00:00:00", "attribute2": "2026/08/31 00:00:00", "attribute3": attr3,
                "plant_org_id": 7, "plant_count": 1, "plant_code": plant, "plant_name": plant_name,
                "line_description": "Electricity charges"}

    @staticmethod
    def att(att_id, entity, pk1, doc_id, name, media=None, datatype=6, url=None, size=None, updated=None):
        t = datetime(2026, 9, 5, 12, 0, 0)
        return {"attached_document_id": att_id, "entity_name": entity, "pk1_value": str(pk1), "document_id": doc_id,
                "seq_num": 1, "att_created": t, "att_updated": updated or t, "datatype_id": datatype, "media_id": media,
                "doc_file_name": name, "url": url, "category_id": None, "doc_description": None, "doc_title": None,
                "lob_file_name": name if media else None, "content_type": "application/pdf" if media else None,
                "size_bytes": size}

    # --- interface
    def resolve_item_ids(self, codes):
        return {c: self.items[c] for c in codes if c in self.items}

    def po_lines_for_items(self, item_ids):
        if self.fail:
            raise OracleError(self.fail)
        return self.lines

    def po_headers(self, header_ids, item_ids):
        return [h for h in self.headers if h["po_header_id"] in header_ids]

    def attachments(self, header_ids, line_ids, since=None):
        self.calls.append(since)
        out = []
        for a in self.atts:
            owner = int(a["pk1_value"]) if a["entity_name"] == "PO_HEADERS" else None
            if owner is not None and owner not in header_ids:
                continue
            if a["entity_name"] == "PO_LINES" and int(a["pk1_value"]) not in line_ids:
                continue
            if since is not None and a["att_updated"] < since:
                continue
            out.append(a)
        return out

    def fetch_attachment_bytes(self, media_id):
        if media_id not in self.blobs:
            raise OracleError("No FND_LOBS row")
        return self.blobs[media_id]


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "eb.db"
        self._orig = (config.DATA_DIR, config.STORAGE_DIR, config.ORIGINALS_DIR)
        config.DATA_DIR = Path(self.tmp.name)
        config.STORAGE_DIR = Path(self.tmp.name) / "storage"
        config.ORIGINALS_DIR = config.STORAGE_DIR / "originals"
        storage.init_db(self.db)

    def tearDown(self):
        config.DATA_DIR, config.STORAGE_DIR, config.ORIGINALS_DIR = self._orig
        self.tmp.cleanup()

    def rows(self, sql, *args):
        with storage.session(self.db) as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]


# ----------------------------------------------------------------------------- unit tests
class ReadOnlyGuard(unittest.TestCase):
    def test_select_and_with_allowed(self):
        _assert_read_only("SELECT 1 FROM dual")
        _assert_read_only("  with x as (select 1 a from dual) select a from x")

    def test_all_column_names_we_use_pass(self):
        _assert_read_only("SELECT a.creation_date, a.last_update_date, h.closed_code, l.item_description FROM t")

    def test_writes_and_multi_statements_rejected(self):
        for bad in ("DELETE FROM po_headers_all", "update x set a=1", "select 1 from dual; drop table x",
                    "INSERT INTO t VALUES (1)", "begin null; end;", "select * from t for update wait 5; commit",
                    "CREATE TABLE t (a int)", "call proc()", "truncate table x"):
            with self.assertRaises(OracleError, msg=bad):
                _assert_read_only(bad)


class SettingsSafety(unittest.TestCase):
    def test_password_never_in_repr_or_public_dict(self):
        s = OracleSettings("h", "1", "svc", "u", "S3cr3t!", "C:/ic")
        self.assertNotIn("S3cr3t!", repr(s))
        self.assertNotIn("S3cr3t!", str(s.public_dict()))
        self.assertEqual(s.public_dict()["password"], "<set>")

    def test_env_override(self):
        os.environ["EB_ORACLE_HOST"] = "override-host"
        try:
            self.assertEqual(load_oracle_settings().host, "override-host")
        finally:
            del os.environ["EB_ORACLE_HOST"]

    def test_error_text_scrubbed(self):
        src = OracleSource(OracleSettings("h", "1", "svc", "u", "S3cr3t!", ""))
        self.assertNotIn("S3cr3t!", src._safe(Exception("ORA-01017 for S3cr3t! user")))


class RuleTests(TempDbCase):
    def test_default_item_rule_seeded(self):
        with storage.session(self.db) as c:
            self.assertEqual(RuleSet.load(c).item_codes, ["EXP00084"])

    def test_exclusions(self):
        rs = RuleSet(item_codes=["EXP00084"], supplier_exclude=["HDFC"], description_exclude=["demand draft"],
                     plant_exclude=["COR"])
        self.assertEqual(rs.evaluate({"supplier_name": "BESCOM", "plant_code": "BG2"}), (True, None))
        self.assertFalse(rs.evaluate({"supplier_name": "HDFC Bank", "plant_code": "BG2"})[0])
        self.assertFalse(rs.evaluate({"supplier_name": "X", "comments": "Purchase of Demand Draft"})[0])
        self.assertFalse(rs.evaluate({"supplier_name": "X", "plant_code": "cor"})[0])

    def test_include_list(self):
        rs = RuleSet(supplier_include=["bescom", "msedcl"])
        self.assertTrue(rs.evaluate({"supplier_name": "AEE Bescom Hoskote"})[0])
        self.assertFalse(rs.evaluate({"supplier_name": "HDFC"})[0])


class ParsingTests(unittest.TestCase):
    def test_ebs_dates(self):
        self.assertEqual(discovery._parse_ebs_date("2026/09/01 00:00:00"), "2026-09-01")
        self.assertIsNone(discovery._parse_ebs_date("31-08-2026"))
        self.assertIsNone(discovery._parse_ebs_date("2026/13/45 00:00:00"))
        self.assertIsNone(discovery._parse_ebs_date(None))

    def test_consumer_hint_junk_dropped(self):
        for junk in ("----", "--", "1", "", None, " . "):
            self.assertIsNone(discovery._clean_hint(junk), junk)
        self.assertEqual(discovery._clean_hint(" 278589111475 "), "278589111475")


class DiscoveryTests(TempDbCase):
    def test_full_scan_stores_pos_plants_and_attachments(self):
        src = FakeSource()
        r = discovery.run_scan(src, mode="full", db_path=self.db)
        self.assertEqual(r["status"], "SUCCESS", r)
        self.assertEqual((r["pos_inspected"], r["new_pos"], r["new_attachments"]), (3, 3, 1))
        po = self.rows("SELECT * FROM electricity_pos WHERE po_header_id = 10")[0]
        self.assertEqual((po["plant_code"], po["billing_start"], po["billing_end"], po["consumer_hint"]),
                         ("BG2", "2026-08-01", "2026-08-31", "278589111475"))
        self.assertIsNone(self.rows("SELECT consumer_hint FROM electricity_pos WHERE po_header_id = 20")[0]["consumer_hint"])

    def test_rule_change_excludes_without_losing_data(self):
        src = FakeSource()
        with storage.session(self.db) as c:
            c.execute("INSERT INTO electricity_id_rules (rule_type,value,created_at) VALUES ('SUPPLIER_EXCLUDE','HDFC','x')")
        discovery.run_scan(src, mode="full", db_path=self.db)
        rows = {r["po_header_id"]: r for r in self.rows("SELECT * FROM electricity_pos")}
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[30]["eligible"], 0)
        self.assertIn("HDFC", rows[30]["exclusion_reason"])
        self.assertEqual(rows[10]["eligible"], 1)

    def test_cancelled_po_not_eligible(self):
        src = FakeSource()
        src.headers[0]["cancel_flag"] = "Y"
        discovery.run_scan(src, mode="full", db_path=self.db)
        self.assertEqual(self.rows("SELECT eligible FROM electricity_pos WHERE po_header_id=10")[0]["eligible"], 0)

    def test_rescan_is_idempotent(self):
        src = FakeSource()
        discovery.run_scan(src, mode="full", db_path=self.db)
        r = discovery.run_scan(src, mode="full", db_path=self.db)
        self.assertEqual((r["new_pos"], r["new_attachments"], r["changed_attachments"]), (0, 0, 0))
        self.assertEqual(self.rows("SELECT COUNT(*) c FROM electricity_attachments")[0]["c"], 1)

    def test_old_po_gets_new_attachment_months_later_in_incremental_mode(self):
        src = FakeSource()
        discovery.run_scan(src, mode="full", db_path=self.db)
        # PO 20 existed since September; a bill is attached to it much later.
        src.atts.append(src.att(2000, "PO_HEADERS", 20, 5002, "late.pdf", media=901,
                                updated=datetime.now() + timedelta(minutes=1)))
        r = discovery.run_scan(src, mode="incremental", db_path=self.db)
        self.assertEqual(r["status"], "SUCCESS", r)
        self.assertEqual(r["new_attachments"], 1)
        self.assertIsNotNone(src.calls[-1])   # incremental really used the 'since' filter
        self.assertEqual(self.rows("SELECT po_header_id FROM electricity_attachments WHERE attached_document_id=2000")[0]["po_header_id"], 20)

    def test_brand_new_po_gets_full_history_in_incremental_mode(self):
        src = FakeSource()
        discovery.run_scan(src, mode="full", db_path=self.db)
        src.lines.append({"po_line_id": 4, "po_header_id": 40, "line_num": 1, "item_description": "Electricity", "item_id": 111})
        src.headers.append(FakeSource.hdr(40, "P40", "TSSPDCL", "HY2", "Hyderabad-2"))
        old = datetime(2026, 1, 1)    # attachment older than the incremental window
        src.atts.append(src.att(3000, "PO_HEADERS", 40, 5003, "old.pdf", media=902, updated=old))
        r = discovery.run_scan(src, mode="incremental", db_path=self.db)
        self.assertEqual((r["new_pos"], r["new_attachments"]), (1, 1))

    def test_line_level_attachment_maps_to_header(self):
        src = FakeSource()
        src.atts.append(src.att(4000, "PO_LINES", 2, 5004, "line.pdf", media=903))
        discovery.run_scan(src, mode="full", db_path=self.db)
        self.assertEqual(self.rows("SELECT po_header_id FROM electricity_attachments WHERE attached_document_id=4000")[0]["po_header_id"], 20)

    def test_web_link_not_downloadable(self):
        src = FakeSource()
        src.atts.append(src.att(5000, "PO_HEADERS", 10, 5005, None, datatype=5, url="https://dms.rdcconcrete.com/permalink?ID=X"))
        discovery.run_scan(src, mode="full", db_path=self.db)
        self.assertEqual(self.rows("SELECT fetch_status FROM electricity_attachments WHERE attached_document_id=5000")[0]["fetch_status"], "WEB_LINK")

    def test_revised_attachment_requeued_and_revision_bumped(self):
        src = FakeSource()
        discovery.run_scan(src, mode="full", db_path=self.db)
        discovery.download_pending(src, db_path=self.db)
        self.assertEqual(self.rows("SELECT fetch_status FROM electricity_attachments")[0]["fetch_status"], "DOWNLOADED")
        src.atts[0]["media_id"] = 901
        src.blobs[901] = (b"%PDF-1.4 revised", "bill.pdf", "application/pdf")
        r = discovery.run_scan(src, mode="full", db_path=self.db)
        self.assertEqual(r["changed_attachments"], 1)
        row = self.rows("SELECT fetch_status, revision FROM electricity_attachments")[0]
        self.assertEqual((row["fetch_status"], row["revision"]), ("PENDING", 2))
        discovery.download_pending(src, db_path=self.db)
        files = list(config.ORIGINALS_DIR.rglob("*.pdf"))
        self.assertEqual(len(files), 2)   # the original version is preserved, not overwritten

    def test_download_stores_hashed_file_and_dedups(self):
        import hashlib
        src = FakeSource()
        src.atts.append(src.att(6000, "PO_HEADERS", 20, 5006, "copy.pdf", media=900))   # same bytes, second attachment
        discovery.run_scan(src, mode="full", db_path=self.db)
        r = discovery.download_pending(src, db_path=self.db)
        self.assertEqual((r["downloaded"], r["failed"]), (2, 0))
        digest = hashlib.sha256(b"%PDF-1.4 fake").hexdigest()
        self.assertEqual(len(list(config.ORIGINALS_DIR.rglob(f"{digest}.pdf"))), 1)
        self.assertEqual(self.rows("SELECT COUNT(DISTINCT sha256) c FROM electricity_attachments")[0]["c"], 1)

    def test_download_failure_recorded_not_raised(self):
        src = FakeSource()
        src.atts.append(src.att(7000, "PO_HEADERS", 10, 5007, "gone.pdf", media=999))
        discovery.run_scan(src, mode="full", db_path=self.db)
        r = discovery.download_pending(src, db_path=self.db)
        self.assertEqual(r["failed"], 1)
        self.assertEqual(self.rows("SELECT fetch_status FROM electricity_attachments WHERE attached_document_id=7000")[0]["fetch_status"], "FAILED")

    def test_oracle_failure_recorded_as_failed_run_and_checkpoint_not_advanced(self):
        src = FakeSource()
        discovery.run_scan(src, mode="full", db_path=self.db)
        with storage.session(self.db) as c:
            before = storage.get_state(c, "last_success_started_at")
        src.fail = "ORA-12170: TNS connect timeout"
        r = discovery.run_scan(src, mode="incremental", db_path=self.db)
        self.assertEqual(r["status"], "FAILED")
        with storage.session(self.db) as c:
            self.assertEqual(storage.get_state(c, "last_success_started_at"), before)
        self.assertEqual(self.rows("SELECT status FROM electricity_scan_runs ORDER BY id DESC LIMIT 1")[0]["status"], "FAILED")

    def test_missing_item_code_is_reported(self):
        src = FakeSource()
        src.items = {}
        r = discovery.run_scan(src, mode="full", db_path=self.db)
        self.assertEqual(r["status"], "FAILED")
        self.assertIn("EXP00084", r["message"])

    def test_unsafe_extension_never_used_in_path(self):
        self.assertEqual(discovery._safe_extension("../../evil.p/../x", None), ".bin")
        self.assertEqual(discovery._safe_extension("a.PDF", None), ".pdf")
        self.assertEqual(discovery._safe_extension(None, "image/jpeg"), ".jpg")


# ----------------------------------------------------------------------------- live smoke test (read-only)
def _oracle_up() -> bool:
    s = load_oracle_settings()
    if not s.is_complete():
        return False
    try:
        with socket.create_connection((s.host, int(s.port)), timeout=2):
            return True
    except OSError:
        return False


@unittest.skipUnless(_oracle_up(), "Oracle EBS not reachable from this machine")
class LiveOracleSmoke(TempDbCase):
    @classmethod
    def setUpClass(cls):
        cls.src = OracleSource()

    @classmethod
    def tearDownClass(cls):
        cls.src.close()

    def test_connection_is_the_read_only_account(self):
        info = self.src.ping()
        self.assertEqual(info["database"], "RDCAZPRD")

    def test_item_resolves(self):
        ids = self.src.resolve_item_ids(["EXP00084"])
        self.assertIn("EXP00084", ids)

    def test_real_scan_maps_plants_and_finds_pdf_attachments(self):
        r = discovery.run_scan(self.src, mode="full", db_path=self.db)
        self.assertEqual(r["status"], "SUCCESS", r)
        self.assertGreater(r["pos_inspected"], 1000)
        unmapped = self.rows("SELECT COUNT(*) c FROM electricity_pos WHERE plant_code IS NULL AND po_created > date('now','-400 day')")[0]["c"]
        self.assertEqual(unmapped, 0, "recent electricity POs must all map to a plant")
        multi = self.rows("SELECT COUNT(*) c FROM electricity_pos WHERE plant_count > 1 AND po_created > date('now','-400 day')")[0]["c"]
        self.assertEqual(multi, 0)
        pdfs = self.rows("SELECT COUNT(*) c FROM electricity_attachments WHERE content_type = 'application/pdf'")[0]["c"]
        self.assertGreater(pdfs, 1000)

    def test_download_real_pdf_matches_oracle_size(self):
        discovery.run_scan(self.src, mode="incremental", db_path=self.db)   # first run = effectively full for a fresh db
        r = discovery.download_pending(self.src, limit=3, db_path=self.db)
        self.assertEqual(r["failed"], 0, r)
        for row in self.rows("SELECT * FROM electricity_attachments WHERE fetch_status='DOWNLOADED'"):
            data = (config.STORAGE_DIR / row["local_path"]).read_bytes()
            self.assertEqual(len(data), row["size_bytes"])
            if row["content_type"] == "application/pdf":
                self.assertTrue(data.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
