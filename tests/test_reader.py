"""Phase 2 tests: parsing, label scoring, layout, extraction on synthetic bills with known answers, loader safety,
persistence/corrections, and a regression check against the hand-verified real-bill gold sets.

Run:  .venv\\Scripts\\python -m unittest discover -s tests -v
"""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from eb_app import config, storage
from eb_app.reader import fields as F
from eb_app.reader import loader, store
from eb_app.reader.layout import PageIndex, build_rows, groups_in_row
from eb_app.reader.model import Cell, Page
from eb_app.reader.pipeline import analyze_document, analyze_file
from eb_app.reader.textnorm import (detect_unit, first_number, fix_devanagari_order, is_numeric_cell, norm, parse_dates,
                                    parse_numbers)
import helpers


def cell(text, x0, y0, x1=None, y1=None, conf=1.0):
    return Cell(text, x0, y0, x1 if x1 is not None else x0 + 6 * len(text), y1 if y1 is not None else y0 + 8, conf, "native", 1)


# ------------------------------------------------------------------------------------------------- parsing
class NumberParsing(unittest.TestCase):
    def test_indian_grouping_and_signs(self):
        self.assertEqual(parse_numbers("Rs. 1,25,460.00"), [125460.0])
        self.assertEqual(parse_numbers("-1,816.96"), [-1816.96])
        self.assertEqual(parse_numbers("(1,234.50)"), [-1234.5])
        self.assertEqual(parse_numbers("1,234.50 Dr"), [-1234.5])
        self.assertEqual(parse_numbers("343.63Cr"), [343.63])
        self.assertEqual(parse_numbers("−1,234.50"), [-1234.5])           # Unicode minus
        self.assertEqual(parse_numbers("`60340.00"), [60340.0])                # rupee glyph printed as a backtick
        self.assertEqual(parse_numbers("Rs.765/-"), [765.0])

    def test_numeric_cells_include_currency_prefixes(self):
        for t in ("Rs. 500", "Ps. 765", "INR 12,000.00", "0.7690", ":154092836", "-98.130"):
            self.assertTrue(is_numeric_cell(t), t)
        for t in ("Bill No", "(KW)", "kWh", "75% of CD"):
            self.assertFalse(is_numeric_cell(t), t)

    def test_unparseable_is_none_not_zero(self):
        self.assertIsNone(first_number("n/a"))
        self.assertIsNone(first_number(""))

    def test_units(self):
        self.assertEqual(detect_unit("Contract Demand (KVA)"), "kVA")
        self.assertEqual(detect_unit("MDI : 7.00 (kVA)"), "kVA")
        self.assertEqual(detect_unit("KVAH"), "kVAh")
        self.assertEqual(detect_unit("KWh Units"), "kWh")
        self.assertIsNone(detect_unit("Total"))


class DateParsing(unittest.TestCase):
    def d(self, t):
        return [x.isoformat() for x in parse_dates(t)]

    def test_day_first_indian_dates(self):
        self.assertEqual(self.d("01/09/2026"), ["2026-09-01"])
        self.assertEqual(self.d("03-10-2026"), ["2026-10-03"])
        self.assertEqual(self.d("30.09.2026"), ["2026-09-30"])

    def test_month_names_and_short_years(self):
        self.assertEqual(self.d("01-Sept-2026"), ["2026-09-01"])
        self.assertEqual(self.d("1 Sep 26"), ["2026-09-01"])
        self.assertEqual(self.d("Sep 01, 2026"), ["2026-09-01"])
        self.assertEqual(self.d("2026/09/01 00:00:00"), ["2026-09-01"])

    def test_typographic_hyphens(self):
        self.assertEqual(self.d("24‐AUG‐2026"), ["2026-08-24"])

    def test_two_dates_do_not_overlap(self):
        # OCR glued 'dd-mm-yyyy-dd-mm-yyyy'; the old parser invented a third date out of the middle
        self.assertEqual(self.d("01-08-2026-01-09-2026"), ["2026-08-01", "2026-09-01"])
        self.assertEqual(self.d("26/08/2026- 26/09/2026"), ["2026-08-26", "2026-09-26"])

    def test_impossible_dates_rejected(self):
        self.assertEqual(self.d("31/02/2026"), [])
        self.assertEqual(self.d("45/13/2026"), [])


class IndicNormalisation(unittest.TestCase):
    def test_visual_order_vowel_sign_is_repaired(self):
        self.assertEqual(fix_devanagari_order("िबल"), "बिल")      # 'िबल' -> 'बिल'

    def test_hindi_and_english_labels_both_match(self):
        s = F.FIELDS["bill_date"]
        self.assertGreaterEqual(F.label_score("िबल ितिथ /Bill Date", s.labels, s.exclude), 0.9)
        self.assertGreaterEqual(F.label_score("बिल तिथि", s.labels, s.exclude), 0.9)         # Hindi only
        self.assertGreaterEqual(F.label_score("Bill Date", s.labels, s.exclude), 0.95)

    def test_hindi_total_payable(self):
        self.assertGreaterEqual(F.label_score("कुल देय राशि", F.TOTAL_PAYABLE_LABELS, F.TOTAL_PAYABLE_EXCLUDE), 0.9)

    def test_hash_labels_do_not_collide_with_cons(self):
        cn = F.FIELDS["consumer_number"]
        self.assertGreaterEqual(F.label_score("Cons#", cn.labels, cn.exclude), 0.9)         # KSEB consumer number
        self.assertEqual(F.label_score("SD HELD186200NET CONS", cn.labels, cn.exclude), 0.0)   # 'CONS' = consumption on CSPDCL


# ------------------------------------------------------------------------------------------------- labels
class LabelScoring(unittest.TestCase):
    def score(self, text, field):
        s = F.FIELDS[field]
        return F.label_score(text, s.labels, s.exclude)

    def test_similar_labels_are_not_merged(self):
        self.assertGreater(self.score("Contract Demand (KVA) :", "contract_demand"), 0.9)
        self.assertEqual(self.score("Sanctioned Load", "contract_demand"), 0.0)
        self.assertGreater(self.score("Sanctioned Load", "sanctioned_load"), 0.9)
        self.assertEqual(self.score("Billed Demand", "max_demand"), 0.0)                    # billing demand is not recorded MD
        self.assertGreater(self.score("Billed Demand", "billing_demand"), 0.9)

    def test_pf_labels(self):
        self.assertGreater(self.score("Billed P.F.", "pf_billed"), 0.9)
        self.assertGreater(self.score("Power Factor", "pf"), 0.9)
        self.assertEqual(self.score("PF Penalty (Recorded PF : 0.88)", "pf"), 0.0)          # a charge line, not the factor itself
        self.assertEqual(self.score("P.F.Penal Charges/P.F.Incentive", "pf"), 0.0)

    def test_short_abbreviations_need_whole_words(self):
        self.assertGreater(self.score("PF", "pf"), 0.9)
        self.assertEqual(self.score("IPFS Reading", "pf"), 0.0)
        self.assertEqual(self.score("CD", "contract_demand"), 0.0)                          # 'CD' is also Consumer Deposit (KSEB): never trusted alone

    def test_energy_labels_exclude_reactive_and_rates(self):
        self.assertEqual(self.score("Reactive Energy Consumption Lag", "kwh"), 0.0)
        self.assertEqual(self.score("Energy Charge per KWh", "kwh"), 0.0)

    def test_ocr_slip_tolerated_but_dropped_words_are_not(self):
        self.assertGreater(self.score("Contract Demend", "contract_demand"), 0.7)
        self.assertEqual(F.label_score("Consumption", F.FIELDS["kwh"].labels, F.FIELDS["kwh"].exclude), 0.0)   # not 'Consumption KWh'


# ------------------------------------------------------------------------------------------------- layout
class Layout(unittest.TestCase):
    def test_rows_cluster_left_to_right_top_to_bottom(self):
        cells = [cell("B", 100, 10), cell("A", 10, 11), cell("C", 10, 40)]
        rows = build_rows(cells)
        self.assertEqual([[c.text for c in r] for r in rows], [["A", "B"], ["C"]])

    def test_label_amount_in_one_cell_is_split(self):
        g = groups_in_row([cell("Gross Total 237560.00", 475, 10, 560, 18)])
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0].label.text, "Gross Total")
        self.assertEqual(first_number(g[0].numbers[0].text), 237560.0)

    def test_formula_note_attaches_to_its_label(self):
        row = [cell("Net Payable", 10, 10), cell("(e+f+g)", 90, 10), cell("61189.00", 200, 10)]
        g = groups_in_row(row)
        self.assertEqual(len(g), 1)
        self.assertEqual(first_number(g[0].numbers[0].text), 61189.0)

    def test_below_prefers_the_number_over_its_unit_tag(self):
        page = Page(1, 600, 800, [cell("MDI", 200, 10), cell("63.08", 200, 30), cell("(KW)", 201, 30), cell("63.64", 200, 50), cell("(KVA)", 201, 50)])
        pi = PageIndex(page)
        got = pi.below(page.cells[0], max_rows=2)
        self.assertEqual(got[0].text, "63.08")
        self.assertEqual(pi.unit_tag_near(got[0]), "KW")

    def test_stacked_two_line_label_is_merged(self):
        page = Page(1, 600, 800, [cell("Contract", 30, 10, 70, 18), cell("Demand(kVA)", 20, 22, 90, 30), cell("75.0", 40, 40)])
        pi = PageIndex(page)
        self.assertIn("Contract Demand(kVA)", [v.text for v in pi.vlabels])


# ------------------------------------------------------------------------------------------------- extraction on synthetic bills
class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class SyntheticBills(TmpDirCase):
    def read(self, items, **kw):
        path = helpers.make_pdf(self.dir / "bill.pdf", helpers.pad(items), **kw)
        return analyze_file(path, run_ocr=False)

    def v(self, res, field):
        f = res["fields"].get(field)
        return None if f is None else f["value"]

    def test_header_row_x_row_label_table_and_arithmetic_basis(self):
        res = self.read(helpers.tsspdcl_like())
        self.assertEqual(self.v(res, "kwh"), 14097)
        self.assertEqual(self.v(res, "kvah"), 21297)
        self.assertAlmostEqual(self.v(res, "max_demand"), 91.7132, places=3)
        self.assertEqual(self.v(res, "consumer_number"), "CBC3023")
        self.assertEqual(self.v(res, "total_payable"), 237560.0)
        self.assertEqual(res["billing_basis"]["basis"], "kVAh")           # 21297 units x Ps.765 = 162922.05
        self.assertAlmostEqual(self.v(res, "pf_calculated"), 14097 / 21297, places=4)

    def test_kseb_average_strip_is_not_mistaken_for_the_month(self):
        res = self.read(helpers.ksEB_like())
        self.assertEqual(self.v(res, "kwh"), 6311)                        # NOT the 6112 under 'Average'
        self.assertEqual(self.v(res, "kvah"), 6575)
        self.assertEqual(self.v(res, "max_demand"), 48.67)                # highest zone, NOT the 47.28 'Average' MD
        self.assertEqual(self.v(res, "pf"), 0.96)
        self.assertTrue(res["fields"]["kwh"]["cross_checked"])            # 6311/6575 reproduces the printed 0.96

    def test_pf_printed_as_percentage_and_footnote_digit(self):
        res = self.read([(20, 50, "Average Power Factor"), (200, 50, "96.00"),
                         (20, 80, "Power Factor"), (140, 80, "8"), (170, 80, ":0.78")])
        pfs = sorted(c["value"] for c in [res["fields"].get("pf")] if c)
        self.assertTrue(pfs and all(0.5 < p <= 1.0 for p in pfs), pfs)    # never 0.08

    def test_kwh_billed_bill_with_zero_kvah_keeps_the_kwh(self):
        res = self.read([(23, 50, "Billed Units Kwh :"), (143, 50, "2125.00000"), (213, 50, "Billed Units Kvah :"), (313, 50, "0.00")])
        self.assertEqual(self.v(res, "kwh"), 2125)

    def test_energy_figure_never_lands_in_a_demand_field(self):
        res = self.read([(20, 50, "75.00 KVA Billed Units Kvah :"), (240, 50, "8040.00")])
        self.assertIsNone(self.v(res, "billing_demand"))

    def test_serial_number_before_next_label_is_not_the_value(self):
        res = self.read([(20, 50, "Amount Due"), (150, 50, "22"), (175, 50, "Total Amount Due"), (300, 50, "-2572.81")])
        self.assertEqual(self.v(res, "total_payable"), -2572.81)          # a credit balance

    def test_history_table_does_not_override_the_current_month(self):
        items = [(20, 40, "Bill Month"), (150, 40, "Units"), (250, 40, "MD"),
                 (20, 55, "Sep-2026"), (150, 55, "6000"), (250, 55, "58.0"),
                 (20, 70, "Aug-2026"), (150, 70, "5900"), (250, 70, "57.0"),
                 (20, 85, "Jul-2026"), (150, 85, "5800"), (250, 85, "56.0"),
                 (20, 120, "Maximum Demand"), (150, 120, "61.5")]
        res = self.read(items)
        self.assertEqual(self.v(res, "max_demand"), 61.5)

    def test_missing_field_is_not_found_never_zero(self):
        res = self.read([(20, 50, "Consumer No"), (150, 50, "123456789")])
        self.assertNotIn("kwh", res["fields"])
        self.assertNotIn("kvah", res["fields"])
        self.assertIn("kwh", res["missing_fields"])

    def test_printed_zero_is_zero_not_missing(self):
        res = self.read([(20, 50, "Contract Demand (KVA)"), (200, 50, "0.00")])
        self.assertEqual(res["fields"]["contract_demand"]["status"], "ZERO")

    def test_billing_basis_unknown_when_both_readings_equal(self):
        items = helpers.tsspdcl_like()
        items = [i for i in items if i[2] not in ("14097", "21297", "Ps. 765")] + [(185, 144, "21297"), (287, 144, "21297"), (191, 234, "Ps. 765")]
        res = self.read(items)
        self.assertEqual(res["billing_basis"]["basis"], "Unknown")        # equal kWh and kVAh prove nothing

    def test_billing_basis_inferred_only_kwh_is_flagged_as_inference(self):
        res = self.read([(20, 50, "Total kWh:"), (120, 50, "8281")])
        b = res["billing_basis"]
        self.assertEqual(b["basis"], "kWh")
        self.assertTrue(b.get("inferred"))
        self.assertLess(b["confidence"], 0.9)

    def test_third_party_invoice_is_recognised(self):
        res = self.read([(20, 40, "B.S.Industries"), (20, 60, "Electricity charge"), (150, 60, "9 per unit"), (250, 60, "622"), (330, 60, "5,598"),
                         (20, 90, "DESCRIPTION"), (150, 90, "RATE"), (250, 90, "QNT"), (330, 90, "Amount")])
        self.assertEqual(res["classification"]["issuer_type"], "THIRD_PARTY_INVOICE")
        self.assertEqual(self.v(res, "kwh"), 622)
        self.assertEqual(res["fields"]["kwh"]["verification"], "NEEDS_REVIEW")

    def test_unrelated_document_is_not_an_electricity_bill(self):
        res = self.read([(20, 40, "TAX INVOICE"), (20, 60, "Transport charges for plant shifting"), (20, 80, "GSTIN 33BOCPC8276H1Z0")])
        self.assertEqual(res["classification"]["verdict"], "NOT_ELECTRICITY_BILL")
        self.assertEqual(store.bill_status(res), "NOT_A_BILL")


# ------------------------------------------------------------------------------------------------- loader safety
class LoaderSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_type_is_decided_by_content_not_by_name(self):
        pdf = helpers.make_pdf(self.dir / "scan.jpg", [(20, 40, "hello world this is a pdf with a lying extension")])
        self.assertEqual(loader.load_document(pdf, run_ocr=False).kind, "pdf")

    def test_raw_http_response_saved_as_a_pdf_is_unwrapped(self):
        body = helpers.make_pdf(self.dir / "inner.pdf", helpers.tsspdcl_like()).read_bytes()
        wrapped = b"HTTP/1.0 200 OK\r\nContent-Type: application/pdf\r\nDate: x\r\n\r\n" + body
        p = self.dir / "wrapped.pdf"
        p.write_bytes(wrapped)
        doc = loader.load_document(p, run_ocr=False)
        self.assertEqual(doc.kind, "pdf")
        self.assertTrue(any("HTTP" in w for w in doc.warnings))
        self.assertGreater(len(doc.pages[0].cells), 10)

    def test_unsupported_and_corrupt_files_do_not_raise(self):
        p = self.dir / "x.pdf"
        p.write_bytes(b"this is not a pdf at all")
        self.assertEqual(loader.load_document(p).kind, "unsupported")
        q = self.dir / "broken.pdf"
        q.write_bytes(b"%PDF-1.4\n garbage garbage")
        d = loader.load_document(q, run_ocr=False)                  # must not raise
        self.assertIn(d.kind, ("unsupported", "pdf"))

    def test_page_without_text_layer_is_flagged_when_ocr_is_off(self):
        import pymupdf
        doc = pymupdf.open()
        doc.new_page()
        p = self.dir / "blank.pdf"
        doc.save(str(p))
        d = loader.load_document(p, run_ocr=False)
        self.assertEqual(d.pages[0].method, "none")
        self.assertTrue(d.warnings)

    def test_garbage_text_layer_is_not_trusted(self):
        garbage = [cell("�" * 30 + "ab", 10, 10 + 12 * i) for i in range(5)]
        self.assertFalse(loader._text_layer_ok(garbage))
        good = [cell("Consumer Number CBC3023 Total Consumption 14097", 10, 10)]
        self.assertTrue(loader._text_layer_ok(good))

    def test_word_then_decimal_cells_are_split(self):
        out = loader._split_label_value_cells([cell("Normal 4906.500", 35, 338, 93, 346), cell("Previous 31/08/2026", 19, 100, 99, 108)])
        self.assertEqual([c.text for c in out], ["Normal", "4906.500", "Previous", "31/08/2026"])
        out2 = loader._split_label_value_cells([cell("Rs. 500.00", 10, 10)])
        self.assertEqual([c.text for c in out2], ["Rs. 500.00"])        # a currency prefix is not a label


# ------------------------------------------------------------------------------------------------- charge summary
class ChargeSummary(unittest.TestCase):
    @staticmethod
    def line(label, category, amount, page=1):
        return {"label": label, "category": category, "amount": amount, "numbers": [amount], "page": page, "bbox": [0, 0, 1, 1], "conf": 1.0}

    def test_tod_slabs_are_summed_and_a_printed_category_total_wins(self):
        from eb_app.reader import charges
        s = charges.summarise([self.line("Energy charges - Normal", "energy_charges", 26831.25), self.line("Energy charges - Peak", "energy_charges", 9468.75)])
        self.assertEqual(s["charge_energy_charges"]["value"], 36300.0)
        self.assertIn("sum of 2", s["charge_energy_charges"]["method"])
        s2 = charges.summarise([self.line("Energy charges - Normal", "energy_charges", 100.0), self.line("Total Energy Charge", "energy_charges", 95.0)])
        self.assertEqual(s2["charge_energy_charges"]["value"], 95.0)

    def test_pf_line_sign_decides_penalty_or_incentive(self):
        from eb_app.reader import charges
        pen = charges.summarise([self.line("PF Penalty", "pf_adjustment", 320.64)])
        self.assertIn("pf_penalty", pen)
        self.assertNotIn("pf_incentive", pen)
        inc = charges.summarise([self.line("PF Incentive / Disincentive", "pf_adjustment", -205.13)])
        self.assertEqual(inc["pf_incentive"]["value"], 205.13)
        self.assertNotIn("pf_penalty", inc)

    def test_the_pf_value_itself_is_not_added_to_the_penalty(self):
        from eb_app.reader import charges
        s = charges.summarise([self.line("PF Penalty (Recorded PF : 0.88)", "pf_adjustment", 0.88), self.line("PF Penalty (Recorded PF : 0.88)", "pf_adjustment", 320.64)])
        self.assertEqual(s["pf_penalty"]["value"], 320.64)

    def test_zero_stays_zero_and_a_real_penalty_needs_confirmation(self):
        from eb_app.reader import charges
        zero = charges.summarise([self.line("Charges For Excess Demand", "excess_demand_penalty", 0.0)])
        self.assertEqual((zero["charge_excess_demand_penalty"]["status"], zero["charge_excess_demand_penalty"]["verification"]), ("ZERO", "AUTO"))
        real = charges.summarise([self.line("Charges For Excess Demand", "excess_demand_penalty", 2500.0)])
        self.assertEqual(real["charge_excess_demand_penalty"]["verification"], "NEEDS_REVIEW")

    def test_no_line_means_no_field(self):
        from eb_app.reader import charges
        self.assertEqual(charges.summarise([self.line("Demand Charges", "demand_charges", 54000.0)]).keys(), {"charge_demand_charges"})


# ------------------------------------------------------------------------------------------------- image inputs through the real OCR path
def _arial():
    for f in ("C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if pathlib.Path(f).exists():
            return f
    return None


@unittest.skipIf(os.environ.get("EB_SKIP_OCR_TESTS") == "1" or _arial() is None, "OCR tests skipped (EB_SKIP_OCR_TESTS=1 or no font)")
class ImageInputs(TmpDirCase):
    """Every supported image format is read by OCR and yields the right numbers; a sideways page is turned upright."""

    @staticmethod
    def bill_image():
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (1000, 760), "white")
        d = ImageDraw.Draw(img)
        big = ImageFont.truetype(_arial(), 30)
        rows = [("Consumer Number", "CBC3023"), ("Bill Date", "03-10-2026"), ("Contract Demand (KVA)", "160"), ("Units Consumed KWH", "14097"),
                ("Net Payable Amount", "237560.00"), ("Power Factor", "0.66")]
        for i, (a, b) in enumerate(rows):
            d.text((40, 60 + 100 * i), a, fill="black", font=big)
            d.text((560, 60 + 100 * i), b, fill="black", font=big)
        return img

    def check(self, path):
        doc = loader.load_document(path, run_ocr=True, use_cache=False)
        self.assertEqual(doc.kind, "image")
        res = analyze_document(doc)
        got = {k: v["value"] for k, v in res["fields"].items()}
        self.assertEqual(got.get("consumer_number"), "CBC3023")
        self.assertEqual(got.get("kwh"), 14097)
        self.assertEqual(got.get("total_payable"), 237560.0)
        self.assertEqual(got.get("contract_demand"), 160)
        self.assertEqual(doc.pages[0].method, "ocr")
        return doc

    def test_png_jpeg_tiff_webp_bmp(self):
        img = self.bill_image()
        for ext, kw in (("png", {}), ("jpg", {"quality": 85}), ("tiff", {}), ("webp", {"quality": 90}), ("bmp", {})):
            p = self.dir / f"bill.{ext}"
            img.save(p, **kw)
            with self.subTest(fmt=ext):
                self.check(p)

    def test_sideways_page_is_turned_upright(self):
        p = self.dir / "sideways.png"
        self.bill_image().rotate(90, expand=True).save(p)
        doc = self.check(p)
        self.assertIn(doc.pages[0].rotation_applied, (90, 270))

    def test_multipage_tiff_reads_every_page(self):
        imgs = [self.bill_image(), self.bill_image()]
        p = self.dir / "multi.tiff"
        imgs[0].save(p, save_all=True, append_images=imgs[1:])
        doc = loader.load_document(p, run_ocr=True, use_cache=False)
        self.assertEqual(len(doc.pages), 2)
        self.assertTrue(all(len(pg.cells) >= 8 for pg in doc.pages))

    def test_a_blank_image_does_not_crash_and_is_not_a_bill(self):
        from PIL import Image
        p = self.dir / "blank.png"
        Image.new("RGB", (600, 400), "white").save(p)
        res = analyze_document(loader.load_document(p, run_ocr=True, use_cache=False))
        self.assertEqual(res["fields"], {})
        self.assertEqual(store.bill_status(res), "NOT_A_BILL")


# ------------------------------------------------------------------------------------------------- optional AI fallback
class FakeProvider:
    name = "fake"

    def __init__(self, answer):
        self.answer, self.seen = answer, None

    def extract(self, page_texts, fields):
        self.seen = (page_texts, fields)
        return self.answer


class AIAssistTests(TmpDirCase):
    from eb_app.reader import ai_assist as ai

    def doc(self, items):
        return loader.load_document(helpers.make_pdf(self.dir / "ai.pdf", helpers.pad(items)), run_ocr=False)

    def test_aadhaar_is_redacted_but_a_12_digit_consumer_number_is_not(self):
        self.assertIn("[AADHAAR]", self.ai.redact("Aadhaar 2345 6789 0123"))
        self.assertIn("[AADHAAR]", self.ai.redact("UID: 234567890123"))
        self.assertEqual(self.ai.redact("Consumer No. : 183271653074"), "Consumer No. : 183271653074")

    def test_off_by_default_and_nothing_is_called(self):
        import os
        self.assertFalse(self.ai.enabled())
        os.environ["EB_AI_ASSIST"] = "1"
        try:
            self.assertFalse(self.ai.enabled())                         # a provider must be configured too
            self.assertIsNone(self.ai.load_provider())
        finally:
            del os.environ["EB_AI_ASSIST"]

    def test_fills_only_missing_fields_and_marks_them_for_review(self):
        doc = self.doc([(20, 50, "Bill dated"), (200, 50, "03-Oct-2026"), (20, 80, "Consumer No"), (200, 80, "123456789012")])
        prov = FakeProvider({"billing_period": {"value": ["2026-09-01", "2026-09-30"], "quote": "Period 01/09/2026 to 30/09/2026"},
                             "consumer_number": {"value": "OVERRIDE", "quote": "OVERRIDE"}})
        res = analyze_document(doc, ai_provider=prov)
        self.assertNotIn("billing_period", res["fields"])               # the quote is not in the shared text -> rejected
        self.assertEqual(res["fields"]["consumer_number"]["value"], "123456789012")      # an existing value is never overridden

    def test_supported_answer_is_accepted_with_evidence(self):
        doc = self.doc([(20, 50, "Billing window 01/09/2026 to 30/09/2026"), (20, 80, "Consumer No"), (200, 80, "123456789012")])
        prov = FakeProvider({"billing_period": {"value": ["2026-09-01", "2026-09-30"], "quote": "Billing window 01/09/2026 to 30/09/2026"}})
        res = analyze_document(doc, ai_provider=prov)
        rec = res["fields"]["billing_period"]
        self.assertEqual((rec["verification"], rec["method"]), ("NEEDS_REVIEW", "ai:fake"))
        self.assertIn("quoted from the bill", rec["evidence"])

    def test_fabricated_numbers_are_rejected(self):
        doc = self.doc([(20, 50, "Units consumed 4500"), (20, 80, "Consumer No"), (200, 80, "123456789012")])
        prov = FakeProvider({"kwh": {"value": 9999, "quote": "Units consumed 4500"}})
        self.assertEqual(self.ai.assist(doc, ["kwh"], prov), {})          # 9999 does not appear in the quoted text -> rejected

    def test_personal_data_is_redacted_before_sharing(self):
        doc = self.doc([(20, 50, "Mobile 9876543210 mail raj@example.com PAN ABCDE1234F GSTIN 27AAECM2933K1ZB"), (20, 80, "Consumer No"), (200, 80, "123456789012")])
        prov = FakeProvider({})
        self.ai.assist(doc, ["kwh"], prov)
        shared = "\n".join(prov.seen[0])
        for secret in ("9876543210", "raj@example.com", "ABCDE1234F", "27AAECM2933K1ZB"):
            self.assertNotIn(secret, shared)
        self.assertIn("123456789012", shared)                           # bill data itself is kept

    def test_provider_failure_never_breaks_reading(self):
        class Boom:
            name = "boom"

            def extract(self, *_):
                raise RuntimeError("service down")
        doc = self.doc([(20, 80, "Consumer No"), (200, 80, "123456789012")])
        res = analyze_document(doc, ai_provider=Boom())
        self.assertEqual(res["fields"]["consumer_number"]["value"], "123456789012")


# ------------------------------------------------------------------------------------------------- persistence
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "eb.db"
        self._orig = (config.DATA_DIR, config.STORAGE_DIR, config.ORIGINALS_DIR)
        config.DATA_DIR = pathlib.Path(self.tmp.name)
        config.STORAGE_DIR = pathlib.Path(self.tmp.name) / "storage"
        config.ORIGINALS_DIR = config.STORAGE_DIR / "originals"
        storage.init_db(self.db)
        self.pdf = helpers.make_pdf(config.ORIGINALS_DIR / "ab" / "ab1.pdf", helpers.pad(helpers.ksEB_like()))
        with storage.session(self.db) as c:
            c.execute("INSERT INTO electricity_pos (po_header_id, po_number, supplier_name, plant_code, billing_start, billing_end, consumer_hint, first_seen, last_seen)"
                      " VALUES (1, 'P1', 'KSEB THRISSUR', 'TR1', '2026-08-31', '2026-09-30', '1356950001519', 'x', 'x')")
            for att, name in ((100, "a"), (101, "b")):
                c.execute("INSERT INTO electricity_attachments (attached_document_id, po_header_id, entity_name, pk1_value, document_id, file_name, fetch_status,"
                          " sha256, local_path, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (att, 1, "PO_HEADERS", "1", att, name + ".pdf", "DOWNLOADED", "samehash", "originals/ab/ab1.pdf", "x", "x"))

    def tearDown(self):
        config.DATA_DIR, config.STORAGE_DIR, config.ORIGINALS_DIR = self._orig
        self.tmp.cleanup()

    def rows(self, sql, *a):
        with storage.session(self.db) as c:
            return [dict(r) for r in c.execute(sql, a).fetchall()]

    def test_process_stores_fields_with_evidence_and_is_idempotent(self):
        r = store.process_attachment(100, db_path=self.db, run_ocr=False)
        self.assertFalse(r["reused"])
        f = {x["field_name"]: x for x in self.rows("SELECT * FROM electricity_bill_fields")}
        self.assertEqual(f["kwh"]["value_num"], 6311)
        self.assertEqual(f["max_demand"]["unit_normalized"], "kVA")
        self.assertIsNotNone(f["kwh"]["bbox_json"])
        self.assertTrue(f["kwh"]["evidence"])
        self.assertTrue(store.process_attachment(100, db_path=self.db, run_ocr=False)["reused"])
        self.assertEqual(len(self.rows("SELECT id FROM electricity_bill_extractions")), 1)

    def test_identical_file_on_another_attachment_is_a_duplicate(self):
        store.process_attachment(100, db_path=self.db, run_ocr=False)
        r2 = store.process_attachment(101, db_path=self.db, run_ocr=False)
        self.assertEqual(r2["bill_status"], "DUPLICATE")
        self.assertIsNotNone(self.rows("SELECT duplicate_of FROM electricity_bill_extractions WHERE attachment_id = 101")[0]["duplicate_of"])

    def test_po_header_cross_check_flags_a_mismatch(self):
        with storage.session(self.db) as c:
            c.execute("UPDATE electricity_pos SET consumer_hint = '999999999', billing_start = '2026-01-01', billing_end = '2026-01-31'")
        r = store.process_attachment(100, db_path=self.db, run_ocr=False)
        flags = json.loads(self.rows("SELECT flags_json FROM electricity_bill_extractions WHERE id = ?", r["extraction_id"])[0]["flags_json"])
        self.assertTrue(any("PO header" in f for f in flags))
        self.assertTrue(any("PO period" in f for f in flags))

    def test_correction_keeps_original_audits_and_exports_only_when_approved(self):
        ext = store.process_attachment(100, db_path=self.db, run_ocr=False)["extraction_id"]
        out = store.apply_correction(ext, "max_demand", "49.10", user="reviewer", unit="kVA", reason="checked against the zone table", db_path=self.db)
        self.assertEqual(out["original"], "48.67")
        row = self.rows("SELECT * FROM electricity_bill_fields WHERE field_name = 'max_demand'")[0]
        self.assertEqual((row["value_num"], row["verification_status"], row["extracted_value_text"]), (49.10, "CORRECTED", "48.67"))
        self.assertEqual(self.rows("SELECT regression_status FROM electricity_manual_corrections")[0]["regression_status"], "PENDING")
        out_file = pathlib.Path(self.tmp.name) / "reg.jsonl"
        self.assertEqual(store.export_regression(out_file, db_path=self.db), 0)          # nothing is exported until a reviewer approves it
        store.approve_correction(1, db_path=self.db)
        self.assertEqual(store.export_regression(out_file, db_path=self.db), 1)
        rec = json.loads(out_file.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual((rec["field"], rec["extracted"], rec["verified"]), ("max_demand", "48.67", "49.1"))

    def test_corrections_need_a_named_person_and_a_readable_value(self):
        ext = store.process_attachment(100, db_path=self.db, run_ocr=False)["extraction_id"]
        with self.assertRaises(ValueError):
            store.apply_correction(ext, "kwh", "6311", user="", db_path=self.db)
        with self.assertRaises(ValueError):
            store.apply_correction(ext, "kwh", "about six thousand", user="x", db_path=self.db)
        with self.assertRaises(ValueError):
            store.apply_correction(ext, "bill_date", "31/02/2026", user="x", db_path=self.db)

    def test_correcting_a_missing_field_creates_it(self):
        ext = store.process_attachment(100, db_path=self.db, run_ocr=False)["extraction_id"]
        store.apply_correction(ext, "contract_demand", "75", user="reviewer", unit="kVA", db_path=self.db)
        self.assertEqual(self.rows("SELECT value_num, verification_status FROM electricity_bill_fields WHERE field_name = 'contract_demand'")[0]["verification_status"], "CORRECTED")

    def add_attachment(self, att_id, items, name):
        helpers.make_pdf(config.ORIGINALS_DIR / "cd" / f"{name}.pdf", helpers.pad(items))
        with storage.session(self.db) as c:
            c.execute("INSERT INTO electricity_attachments (attached_document_id, po_header_id, entity_name, pk1_value, document_id, file_name, fetch_status,"
                      " sha256, local_path, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (att_id, 1, "PO_HEADERS", "1", att_id, name + ".pdf", "DOWNLOADED", "hash-" + name, f"originals/cd/{name}.pdf", "x", "x"))

    def test_agreement_with_the_po_header_is_recorded_as_corroboration(self):
        ext = store.process_attachment(100, db_path=self.db, run_ocr=False)["extraction_id"]
        row = self.rows("SELECT evidence, confidence FROM electricity_bill_fields WHERE extraction_id = ? AND field_name = 'consumer_number'", ext)[0]
        self.assertIn("PO header", row["evidence"])
        self.assertGreater(row["confidence"], 0.85)

    def test_bill_without_a_printed_period_falls_back_to_the_po_period_flagged(self):
        self.add_attachment(102, helpers.tsspdcl_like(), "noperiod")
        ext = store.process_attachment(102, db_path=self.db, run_ocr=False)["extraction_id"]
        row = self.rows("SELECT value_text, method, verification_status FROM electricity_bill_fields WHERE extraction_id = ? AND field_name = 'billing_period'", ext)[0]
        self.assertEqual((row["method"], row["verification_status"]), ("oracle_po_header", "NEEDS_REVIEW"))
        self.assertEqual(row["value_text"], "2026-08-31 .. 2026-09-30")

    def test_missing_fields_are_stored_as_not_found_rows(self):
        self.add_attachment(103, helpers.tsspdcl_like(), "nopf")
        ext = store.process_attachment(103, db_path=self.db, run_ocr=False)["extraction_id"]
        st = {r["field_name"]: r["status"] for r in self.rows("SELECT field_name, status FROM electricity_bill_fields WHERE extraction_id = ?", ext)}
        self.assertEqual(st.get("bill_date"), "NOT_FOUND")            # not printed on this bill: recorded, never defaulted
        self.assertEqual(st.get("kwh"), "FOUND")

    def test_same_template_with_different_numbers_shares_a_layout_signature(self):
        other = [(x, y, t.replace("6311", "7000").replace("6575", "7400").replace("66835.00", "71000.00")) for x, y, t in helpers.ksEB_like()]
        self.add_attachment(104, other, "kseb2")
        a = store.process_attachment(100, db_path=self.db, run_ocr=False)["extraction_id"]
        b = store.process_attachment(104, db_path=self.db, run_ocr=False)["extraction_id"]
        sig = {r["id"]: r["layout_signature"] for r in self.rows("SELECT id, layout_signature FROM electricity_bill_extractions")}
        self.assertEqual(sig[a], sig[b])

    def test_state_hint_from_supplier(self):
        self.assertEqual(store.state_from_supplier("AEE BESCOM HOSKOTE"), "Karnataka")
        self.assertEqual(store.state_from_supplier("TSSPDCL-HYD3"), "Telangana")
        self.assertIsNone(store.state_from_supplier("HDFC Bank Ltd- Purchase of Demand Draft"))


# ------------------------------------------------------------------------------------------------- regression on real, hand-verified bills
GOLD_DIR = pathlib.Path(__file__).resolve().parent / "gold"
REAL_DB = config.DB_PATH


def _gold_available() -> bool:
    return REAL_DB.exists() and (config.STORAGE_DIR / "originals").exists()


@unittest.skipUnless(_gold_available(), "downloaded corpus not present on this machine")
class GoldRegression(unittest.TestCase):
    """Re-reads the real bills whose values were verified by hand and demands they stay correct."""

    KEY = {"pf": ("pf_billed", "pf"), "excess_demand_penalty": ("charge_excess_demand_penalty",)}

    def evaluate(self, name):
        gold = json.loads((GOLD_DIR / name).read_text(encoding="utf-8"))["bills"]
        c = sqlite3.connect(REAL_DB)
        checked = correct = auto_wrong = 0
        problems = []
        for g in gold:
            row = c.execute("SELECT local_path FROM electricity_attachments WHERE attached_document_id = ?", (g["id"],)).fetchone()
            if not row:
                continue
            res = analyze_file(config.STORAGE_DIR / row[0])
            for field, want in g["expect"].items():
                if field == "basis":
                    have, rec = (res["billing_basis"]["basis"] if res["billing_basis"]["basis"] != "Unknown" else None), None
                else:
                    have, rec = None, None
                    for n in self.KEY.get(field, (field,)):
                        if n in res["fields"]:
                            have, rec = res["fields"][n]["value"], res["fields"][n]
                            break
                if want is None:
                    continue
                checked += 1
                ok = have is not None and self._same(field, want, have)
                correct += ok
                if not ok:
                    problems.append(f"{g['id']} {field}: want {want} got {have}")
                    if rec and rec["verification"] == "AUTO":
                        auto_wrong += 1
        return checked, correct, auto_wrong, problems

    @staticmethod
    def _same(field, want, have):
        if isinstance(want, list) and field == "consumer_number":
            return str(have) in [str(w) for w in want]
        if isinstance(want, list):
            return list(have) == want
        if isinstance(want, (int, float)):
            return abs(float(have) - want) <= max(1.0 if field == "total_payable" else 0.0, abs(want) * 2e-4 + 0.005)
        return str(have) == str(want)

    def test_development_set(self):
        checked, correct, auto_wrong, problems = self.evaluate("bills.json")
        self.assertGreaterEqual(correct / checked, 0.95, problems)
        self.assertEqual(auto_wrong, 0, problems)

    def test_scanned_and_photographed_bills(self):
        checked, correct, auto_wrong, problems = self.evaluate("scans.json")
        self.assertGreaterEqual(correct / checked, 0.92, problems)
        self.assertEqual(auto_wrong, 0, problems)

    def test_actual_penalty_amounts_match_the_printed_bills(self):
        """Every PF-penalty / excess-demand amount below was read off the printed bill by hand (quantity x rate re-computed where printed that way)."""
        checked, correct, auto_wrong, problems = self.evaluate("penalties.json")
        self.assertGreater(checked, 25)
        self.assertEqual(correct, checked, problems)
        self.assertEqual(auto_wrong, 0, problems)

    def test_heldout_batches_never_release_a_wrong_value_as_auto(self):
        for name in ("heldout1.json", "heldout2.json"):
            checked, correct, auto_wrong, problems = self.evaluate(name)
            self.assertGreaterEqual(correct / checked, 0.90, (name, problems))
            self.assertEqual(auto_wrong, 0, (name, problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
