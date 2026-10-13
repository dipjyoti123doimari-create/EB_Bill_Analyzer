"""Phase 3 tests: PF / demand / tariff engine, bill materialisation, alerts, analytics, e-mail rules.

Every expected number below is computed by hand in the test, independently of the code under test.
"""
import json
import pathlib
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import synth
from test_phase1 import TempDbCase
from eb_app import alerts, analytics, bills, config, notify, settings, storage
from eb_app.analysis import engine, tariff
from eb_app.analytics import Filters
from eb_app.ops_settings import SmtpSettings
from eb_app.reader import store

CFG = {k: v[0] for k, v in settings.DEFAULTS.items()}


def bill(**kw):
    b = dict(billing_basis="kVAh", kwh=90000.0, kvah=100000.0, pf_billed=0.90, pf_direction=None, contract_demand=100.0, contract_demand_unit="kVA",
             max_demand=92.0, max_demand_unit="kVA", billing_demand=95.0, energy_charges=700000.0, demand_charges=45000.0, fixed_charges=None,
             pf_penalty=None, pf_penalty_status=None, pf_incentive=None, pf_incentive_status=None, excess_demand_penalty=None, excess_demand_status=None,
             state="Maharashtra", discom_code="MSEDCL", consumer_category=None, voltage_category=None, period_start="2026-09-01", period_end="2026-09-30",
             quality="UNVERIFIED")
    b.update(kw)
    return b


def rule(**kw):
    r = dict(id=1, state="Maharashtra", discom_code=None, consumer_category=None, voltage_category=None, effective_from="2026-04-01", effective_to=None,
             billing_basis_rule=None, pf_penalty_threshold=None, contract_demand_tolerance_pct=None, source_reference="MERC order 2026/04", approval_status="APPROVED",
             pf_penalty_rule=None, pf_incentive_rule=None, excess_demand_rule=None, demand_charge_rule=None, tariff_rates=None)
    r.update(kw)
    return r


def run(b, rules=(), cfg=None):
    return engine.analyze_bill(b, list(rules), cfg or CFG)


# ----------------------------------------------------------------------------------------------- 3.1 PF calculation
class PowerFactorTests(unittest.TestCase):
    def test_pf_is_kwh_over_kvah_and_does_not_overwrite_the_billed_pf(self):
        pf, _ = run(bill(kwh=90000.0, kvah=100000.0, pf_billed=0.90))
        self.assertAlmostEqual(pf["pf_calculated"], 0.9)
        self.assertEqual(pf["pf_billed"], 0.90)
        self.assertEqual(pf["validation_status"], "MATCH")

    def test_disagreement_beyond_the_tolerance_is_a_mismatch_and_both_values_are_kept(self):
        pf, _ = run(bill(kwh=78000.0, kvah=100000.0, pf_billed=0.95))
        self.assertEqual(pf["validation_status"], "MISMATCH")
        self.assertEqual((pf["pf_billed"], pf["pf_calculated"]), (0.95, 0.78))
        self.assertAlmostEqual(pf["pf_difference"], 0.17)
        self.assertEqual(pf["pf_source"], "BILLED_CONFLICTS_WITH_KWH_KVAH")

    def test_tolerance_is_configurable(self):
        b = bill(kwh=92000.0, kvah=100000.0, pf_billed=0.95)           # differs by 0.03
        self.assertEqual(run(b)[0]["validation_status"], "MISMATCH")
        self.assertEqual(run(b, cfg={**CFG, "pf_tolerance": 0.05})[0]["validation_status"], "MATCH")

    def test_kwh_larger_than_kvah_is_impossible_not_a_pf_above_one(self):
        pf, _ = run(bill(kwh=120000.0, kvah=100000.0, pf_billed=0.95))
        self.assertIsNone(pf["pf_calculated"])
        self.assertEqual(pf["validation_status"], "INVALID_ENERGY")

    def test_an_implausibly_low_ratio_is_a_misread_not_a_real_pf(self):
        pf, _ = run(bill(kwh=1330.0, kvah=108034.0, pf_billed=None))       # 0.0123: a misread kVAh
        self.assertIsNone(pf["pf_calculated"])
        self.assertEqual(pf["validation_status"], "NO_PF")
        self.assertIsNone(pf["pf_used"])

    def test_no_billed_pf_uses_the_calculated_one_and_says_so(self):
        pf, _ = run(bill(pf_billed=None))
        self.assertEqual((pf["pf_used"], pf["pf_source"]), (0.9, "CALCULATED_FROM_KWH_KVAH"))
        self.assertEqual(pf["validation_status"], "NOT_COMPARABLE_NO_BILLED_PF")

    def test_billed_pf_without_energy_readings_is_kept(self):
        pf, _ = run(bill(kwh=None, kvah=None, pf_billed=0.97))
        self.assertEqual((pf["pf_used"], pf["pf_source"], pf["validation_status"]), (0.97, "BILLED", "NOT_COMPARABLE_NO_KWH_KVAH"))

    def test_nothing_known_is_not_zero(self):
        pf, _ = run(bill(kwh=None, kvah=None, pf_billed=None))
        self.assertIsNone(pf["pf_used"])
        self.assertEqual((pf["validation_status"], pf["performance_status"]), ("NO_PF", "UNKNOWN"))

    def test_leading_pf_is_carried_through(self):
        pf, _ = run(bill(pf_direction="LEAD"))
        self.assertEqual(pf["direction"], "LEAD")
        self.assertTrue(any("LEAD" in n for n in pf["notes"]))


# ----------------------------------------------------------------------------------------------- 3.2 management bands
class PerformanceBandTests(unittest.TestCase):
    def band(self, pf):
        return engine.classify_pf(pf, CFG)

    def test_default_bands_and_their_boundaries(self):
        self.assertEqual(self.band(1.0), "EXCELLENT")
        self.assertEqual(self.band(0.99), "EXCELLENT")
        self.assertEqual(self.band(0.9899), "GOOD")
        self.assertEqual(self.band(0.95), "GOOD")
        self.assertEqual(self.band(0.9499), "ATTENTION")
        self.assertEqual(self.band(0.90), "ATTENTION")
        self.assertEqual(self.band(0.8999), "CRITICAL")
        self.assertEqual(self.band(None), "UNKNOWN")

    def test_bands_are_configurable(self):
        cfg = {**CFG, "pf_good": 0.92, "pf_attention": 0.85}
        self.assertEqual(engine.classify_pf(0.93, cfg), "GOOD")
        self.assertEqual(engine.classify_pf(0.86, cfg), "ATTENTION")
        self.assertEqual(engine.classify_pf(0.84, cfg), "CRITICAL")


# ----------------------------------------------------------------------------------------------- 3.3 Case A: kWh billing
class KwhBillingTests(unittest.TestCase):
    PEN = dict(threshold=0.90, step=0.01, pct_per_step=1.0, rounding="floor", base="energy_charges")

    def test_actual_penalty_on_the_bill_is_reported_without_any_rule(self):
        pf, _ = run(bill(billing_basis="kWh", pf_billed=0.85, kwh=85000.0, kvah=100000.0, pf_penalty=5000.0, pf_penalty_status="FOUND"))
        self.assertEqual(pf["actual_pf_penalty"], 5000.0)
        self.assertIsNone(pf["expected_pf_penalty"])                         # no rule => no invented number
        self.assertEqual(pf["expected_status"], "UNAVAILABLE_NO_RULE")
        self.assertEqual(pf["savings_status"], "NOT_APPLICABLE_KWH_BASIS")

    def test_expected_penalty_comes_only_from_an_approved_rule_and_is_compared_with_the_bill(self):
        # PF 0.85 vs threshold 0.90: floor(0.05 / 0.01) = 5 steps x 1% of energy charges 700000 = 35000
        b = bill(billing_basis="kWh", pf_billed=0.85, kwh=85000.0, kvah=100000.0, pf_penalty=35000.0, pf_penalty_status="FOUND")
        pf, _ = run(b, [rule(pf_penalty_rule=self.PEN)])
        self.assertEqual(pf["expected_pf_penalty"], 35000.0)
        self.assertEqual(pf["expected_status"], "MATCHES_BILL")
        pf2, _ = run({**b, "pf_penalty": 30000.0}, [rule(pf_penalty_rule=self.PEN)])
        self.assertEqual(pf2["expected_status"], "MISMATCH_WITH_BILL")
        self.assertEqual(pf2["expected_vs_bill"], -5000.0)

    def test_pf_at_or_above_the_rule_threshold_has_zero_expected_penalty(self):
        pf, _ = run(bill(billing_basis="kWh", pf_billed=0.91, kwh=91000.0, kvah=100000.0), [rule(pf_penalty_rule=self.PEN)])
        self.assertEqual(pf["expected_pf_penalty"], 0.0)

    def test_rounding_ceil_and_cap_are_applied_as_written_in_the_rule(self):
        r = dict(self.PEN, rounding="ceil", max_pct=3.0)
        pf, _ = run(bill(billing_basis="kWh", pf_billed=0.855, kwh=85500.0, kvah=100000.0), [rule(pf_penalty_rule=r)])
        self.assertEqual(pf["expected_pf_penalty"], 21000.0)                  # ceil(4.5) = 5 steps -> 5% capped at 3% of 700000

    def test_a_missing_charge_base_makes_the_calculation_unavailable_not_zero(self):
        pf, _ = run(bill(billing_basis="kWh", pf_billed=0.85, kwh=85000.0, kvah=100000.0, energy_charges=None), [rule(pf_penalty_rule=self.PEN)])
        self.assertIsNone(pf["expected_pf_penalty"])
        self.assertEqual(pf["expected_status"], "UNAVAILABLE_BASE_MISSING")

    def test_a_kvah_bill_gets_an_expected_penalty_only_when_an_approved_rule_provides_one(self):
        b = bill(billing_basis="kVAh", pf_billed=0.88, kwh=88000.0, kvah=100000.0, pf_penalty=14000.0, pf_penalty_status="FOUND")
        self.assertEqual(run(b)[0]["expected_status"], "UNAVAILABLE_NO_RULE")
        pf, _ = run(b, [rule(pf_penalty_rule=self.PEN)])                                    # floor(0.02/0.01) = 2 steps x 1 % x 700000
        self.assertEqual((pf["expected_pf_penalty"], pf["expected_status"]), (14000.0, "MATCHES_BILL"))
        self.assertEqual(pf["actual_pf_penalty"], 14000.0)                                   # the actual amount stays the bill's own figure

    def test_incentive_rule(self):
        inc = dict(threshold=0.95, step=0.01, pct_per_step=0.5, rounding="floor", base="energy_charges", max_pct=5)
        pf, _ = run(bill(billing_basis="kWh", pf_billed=0.98, kwh=98000.0, kvah=100000.0, pf_incentive=10500.0, pf_incentive_status="FOUND"), [rule(pf_incentive_rule=inc)])
        self.assertTrue(pf["expected_status"].startswith("INCENTIVE_"))
        self.assertTrue(any("10,500.00" in n for n in pf["notes"]))           # floor(3) x 0.5% x 700000


# ----------------------------------------------------------------------------------------------- 3.3 Case B: kVAh billing
class KvahBillingTests(unittest.TestCase):
    def test_saving_formula_current_target_reduction_rate(self):
        # kWh 90000 / PF 0.90 = 100000 kVAh now; 90000 / 0.99 = 90909.09 at target; reduction 9090.91; rate 700000/100000 = 7.0 -> 63636.4
        pf, _ = run(bill(billing_basis="kVAh"))
        self.assertEqual(pf["current_kvah"], 100000.0)
        self.assertEqual(pf["target_kvah"], 90909.09)
        self.assertEqual(pf["kvah_reduction"], 9090.91)
        self.assertEqual((pf["marginal_rate"], pf["rate_source"]), (7.0, "BILL_AVERAGE_ENERGY_RATE"))
        self.assertAlmostEqual(pf["estimated_savings"], 63636.37, places=1)
        self.assertEqual(pf["savings_status"], "ESTIMATE_UNVERIFIED_INPUTS")

    def test_a_tariff_rule_supplies_the_marginal_rate_and_a_verified_bill_drops_the_unverified_label(self):
        pf, _ = run(bill(quality="VERIFIED"), [rule(tariff_rates={"energy_rate_per_kvah": 6.0})])
        self.assertEqual((pf["marginal_rate"], pf["rate_source"], pf["savings_status"]), (6.0, "TARIFF_RULE", "ESTIMATE"))
        self.assertAlmostEqual(pf["estimated_savings"], 54545.46, places=1)

    def test_target_pf_is_configurable(self):
        pf, _ = run(bill(), cfg={**CFG, "target_pf": 0.95})
        self.assertEqual(pf["target_kvah"], round(90000 / 0.95, 2))

    def test_no_separate_penalty_is_invented_for_a_kvah_bill(self):
        pf, _ = run(bill())
        self.assertIsNone(pf["actual_pf_penalty"])
        self.assertIsNone(pf["expected_pf_penalty"])
        self.assertEqual(pf["penalty_status"], "NOT_FOUND")

    def test_an_adjustment_printed_on_a_kvah_bill_is_an_actual_amount_and_the_saving_stays_separate(self):
        pf, _ = run(bill(pf_penalty=1200.0, pf_penalty_status="FOUND"))
        self.assertEqual(pf["actual_pf_penalty"], 1200.0)
        self.assertGreater(pf["estimated_savings"], 0)                       # the two figures coexist, in different columns
        self.assertNotEqual(pf["actual_pf_penalty"], pf["estimated_savings"])

    def test_at_or_above_target_means_zero_saving_not_missing(self):
        pf, _ = run(bill(kwh=99000.0, kvah=100000.0, pf_billed=0.99))
        self.assertEqual((pf["savings_status"], pf["estimated_savings"], pf["kvah_reduction"]), ("AT_OR_ABOVE_TARGET", 0.0, 0.0))

    def test_no_saving_when_the_basis_is_unknown_mixed_or_kwh(self):
        self.assertEqual(run(bill(billing_basis="Unknown"))[0]["savings_status"], "UNAVAILABLE_BASIS_UNKNOWN")
        self.assertEqual(run(bill(billing_basis="Mixed"))[0]["savings_status"], "UNAVAILABLE_BASIS_MIXED")
        self.assertEqual(run(bill(billing_basis="kWh"))[0]["savings_status"], "NOT_APPLICABLE_KWH_BASIS")
        self.assertIsNone(run(bill(billing_basis="Unknown"))[0]["estimated_savings"])

    def test_no_saving_when_the_pf_inputs_contradict_each_other(self):
        pf, _ = run(bill(kwh=78000.0, kvah=100000.0, pf_billed=0.95))
        self.assertEqual(pf["savings_status"], "UNAVAILABLE_PF_CONFLICT")
        self.assertIsNone(pf["estimated_savings"])

    def test_no_saving_without_a_rate(self):
        pf, _ = run(bill(energy_charges=None))
        self.assertEqual(pf["savings_status"], "UNAVAILABLE_NO_RATE")


# ----------------------------------------------------------------------------------------------- 3.4 demand
class DemandTests(unittest.TestCase):
    def dem(self, rules=(), **kw):
        return run(bill(**kw), rules)[1]

    def test_utilisation_and_excess(self):
        d = self.dem(contract_demand=100.0, max_demand=92.0)
        self.assertEqual((d["comparison_status"], d["utilization_pct"], d["exceeds_contract"], d["excess_demand"]), ("COMPARABLE", 92.0, 0, 0.0))
        d = self.dem(contract_demand=100.0, max_demand=108.0)
        self.assertEqual((d["utilization_pct"], d["exceeds_contract"], d["excess_demand"]), (108.0, 1, 8.0))

    def test_above_contract_without_a_rule_is_not_called_a_violation(self):
        d = self.dem(contract_demand=100.0, max_demand=108.0)
        self.assertEqual(d["violation_status"], "UNVERIFIED_NO_RULE")
        self.assertEqual(d["expected_status"], "UNAVAILABLE_NO_RULE")

    def test_the_permitted_tolerance_decides_the_violation(self):
        r = rule(contract_demand_tolerance_pct=5.0)
        self.assertEqual(self.dem([r], contract_demand=100.0, max_demand=104.0)["violation_status"], "WITHIN_PERMITTED_LIMIT")
        self.assertEqual(self.dem([r], contract_demand=100.0, max_demand=105.0)["violation_status"], "WITHIN_PERMITTED_LIMIT")
        self.assertEqual(self.dem([r], contract_demand=100.0, max_demand=108.0)["violation_status"], "VIOLATION")

    def test_zero_tolerance_rule_makes_any_excess_a_violation(self):
        self.assertEqual(self.dem([rule(contract_demand_tolerance_pct=0.0)], contract_demand=100.0, max_demand=100.5)["violation_status"], "VIOLATION")

    def test_regular_demand_charges_are_not_an_excess_demand_penalty(self):
        d = self.dem(demand_charges=45000.0, excess_demand_penalty=None)
        self.assertIsNone(d["actual_excess_penalty"])
        self.assertEqual(d["penalty_status"], "NOT_FOUND")

    def test_a_penalty_billed_by_the_board_is_an_actual_violation_even_without_a_rule(self):
        d = self.dem(contract_demand=100.0, max_demand=92.0, excess_demand_penalty=3000.0, excess_demand_status="FOUND")
        self.assertEqual(d["violation_status"], "VIOLATION_BILLED")
        self.assertEqual(d["actual_excess_penalty"], 3000.0)

    def test_expected_excess_penalty_from_an_approved_rule(self):
        er = {"excess_basis": "over_contract_demand", "unit": "kVA", "rate_per_unit": 475.0, "multiplier": 2.0}
        d = self.dem([rule(contract_demand_tolerance_pct=0.0, excess_demand_rule=er)], contract_demand=100.0, max_demand=108.0,
                     excess_demand_penalty=7600.0, excess_demand_status="FOUND")
        self.assertEqual(d["expected_excess_penalty"], 7600.0)                  # 8 kVA x 475 x 2
        self.assertEqual(d["expected_status"], "MATCHES_BILL")
        er2 = dict(er, excess_basis="over_tolerance_limit")
        d = self.dem([rule(contract_demand_tolerance_pct=5.0, excess_demand_rule=er2)], contract_demand=100.0, max_demand=108.0)
        self.assertEqual(d["expected_excess_penalty"], 2850.0)                  # (108 - 105) x 475 x 2

    def test_kw_is_never_compared_with_kva(self):
        d = self.dem(contract_demand=100.0, contract_demand_unit="kVA", max_demand=90.0, max_demand_unit="kW")
        self.assertEqual(d["comparison_status"], "UNITS_INCOMPATIBLE")
        self.assertIsNone(d["utilization_pct"])
        self.assertEqual(d["violation_status"], "NOT_ASSESSABLE")

    def test_compatible_units_are_converted(self):
        d = self.dem(contract_demand=2.0, contract_demand_unit="MVA", max_demand=1500.0, max_demand_unit="kVA")
        self.assertEqual(d["utilization_pct"], 75.0)
        d = self.dem(contract_demand=100.0, contract_demand_unit="HP", max_demand=70.0, max_demand_unit="kW")
        self.assertAlmostEqual(d["utilization_pct"], round(70 / (100 * 0.7457) * 100, 2))        # HP -> kW by definition

    def test_unknown_or_energy_units_are_flagged_for_review_not_guessed(self):
        self.assertEqual(self.dem(max_demand_unit=None)["comparison_status"], "UNIT_UNKNOWN")
        d = self.dem(max_demand_unit="kVAh")                                    # an energy unit in a demand field
        self.assertEqual(d["comparison_status"], "UNIT_UNKNOWN")
        self.assertTrue(any("energy unit" in n for n in d["notes"]))

    def test_missing_values_are_a_missing_comparison(self):
        self.assertEqual(self.dem(contract_demand=None)["comparison_status"], "MISSING_VALUE")
        self.assertEqual(self.dem(contract_demand=0.0)["comparison_status"], "NO_CONTRACT_DEMAND")

    def test_billing_demand_is_kept_separate(self):
        d = self.dem(contract_demand=100.0, max_demand=60.0, billing_demand=80.0)
        self.assertEqual((d["contract_demand"], d["max_demand"], d["billing_demand"]), (100.0, 60.0, 80.0))


# ----------------------------------------------------------------------------------------------- 3.5 tariff registry
class TariffRegistryTests(TempDbCase):
    GOOD = dict(state="Maharashtra", effective_from="2026-04-01", source_reference="MERC order 12/2026",
                pf_penalty_rule={"threshold": 0.9, "step": 0.01, "pct_per_step": 1, "rounding": "floor", "base": "energy_charges"})

    def make(self, **kw):
        with storage.session(self.db) as c:
            return tariff.create_rule(c, {**self.GOOD, **kw}, "admin")

    def approve(self, rid):
        with storage.session(self.db) as c:
            tariff.set_approval(c, rid, "APPROVED", "admin")

    def rules(self):
        with storage.session(self.db) as c:
            return tariff.load_approved(c)

    def test_nothing_is_built_in(self):
        self.assertEqual(self.rules(), [])

    def test_a_draft_is_never_used_until_approved(self):
        rid = self.make()
        self.assertEqual(self.rules(), [])
        self.approve(rid)
        self.assertEqual([r["id"] for r in self.rules()], [rid])

    def test_a_rule_needs_a_source_before_approval(self):
        rid = self.make(source_reference="")
        with storage.session(self.db) as c, self.assertRaises(tariff.RuleError):
            tariff.set_approval(c, rid, "APPROVED", "admin")

    def test_a_rule_must_name_a_state_or_a_board_and_define_something(self):
        with storage.session(self.db) as c:
            with self.assertRaises(tariff.RuleError):
                tariff.create_rule(c, {**self.GOOD, "state": ""}, "admin")
            with self.assertRaises(tariff.RuleError):
                tariff.create_rule(c, {"state": "X", "effective_from": "2026-01-01"}, "admin")
            with self.assertRaises(tariff.RuleError):
                tariff.create_rule(c, {**self.GOOD, "pf_penalty_rule": {"threshold": 0.9}}, "admin")          # incomplete
            with self.assertRaises(tariff.RuleError):
                tariff.create_rule(c, {**self.GOOD, "effective_to": "2026-01-01"}, "admin")

    def find(self, rules, **kw):
        a = dict(state="Maharashtra", discom="MSEDCL", category=None, voltage=None, start="2026-09-01", end="2026-09-30")
        a.update(kw)
        return tariff.find_rule(rules, a["state"], a["discom"], a["category"], a["voltage"], a["start"], a["end"])

    def test_effective_dates_use_the_billing_period_never_today(self):
        old = rule(id=1, effective_from="2025-04-01", effective_to="2026-03-31")
        new = rule(id=2, effective_from="2026-04-01")
        self.assertEqual(self.find([old, new], start="2026-01-01", end="2026-01-31")[0]["id"], 1)
        self.assertEqual(self.find([old, new], start="2026-09-01", end="2026-09-30")[0]["id"], 2)
        self.assertEqual(self.find([old, new], start="2024-01-01", end="2024-01-31")[1], "NONE")

    def test_a_period_that_spans_a_tariff_change_gets_no_independent_calculation(self):
        old = rule(id=1, effective_from="2025-04-01", effective_to="2026-03-31")
        new = rule(id=2, effective_from="2026-04-01")
        r, st, _ = self.find([old, new], start="2026-03-15", end="2026-04-14")
        self.assertEqual((r, st), (None, "SPANS_CHANGE"))

    def test_state_board_category_matching(self):
        r_state = rule(id=1)
        r_board = rule(id=2, state=None, discom_code="MSEDCL")
        r_cat = rule(id=3, consumer_category="HT-I")
        self.assertEqual(self.find([r_state], state="Kerala", discom="KSEB")[1], "NONE")
        self.assertEqual(self.find([r_board], state="Gujarat")[0]["id"], 2)                # board rule applies wherever the board is named
        self.assertEqual(self.find([r_cat])[1], "CATEGORY_UNKNOWN")                       # the account's category is not recorded
        self.assertEqual(self.find([r_cat], category="ht-i")[0]["id"], 3)
        self.assertEqual(self.find([r_cat], category="HT-II")[1], "NONE")

    def test_the_more_specific_rule_wins_and_a_tie_is_ambiguous(self):
        general, specific = rule(id=1), rule(id=2, discom_code="MSEDCL")
        self.assertEqual(self.find([general, specific])[0]["id"], 2)
        self.assertEqual(self.find([rule(id=1), rule(id=2)])[1], "AMBIGUOUS")

    def test_unknown_period_gets_no_rule(self):
        self.assertEqual(self.find([rule()], start=None, end=None)[1], "NO_PERIOD")

    def test_missing_rule_shows_the_bills_charges_and_marks_the_calculation_unavailable(self):
        pf, dem = run(bill(billing_basis="kWh", pf_billed=0.85, kwh=85000.0, kvah=100000.0, pf_penalty=35000.0, pf_penalty_status="FOUND",
                           contract_demand=100.0, max_demand=108.0))
        self.assertEqual(pf["actual_pf_penalty"], 35000.0)
        self.assertEqual(pf["expected_status"], "UNAVAILABLE_NO_RULE")
        self.assertEqual(dem["violation_status"], "UNVERIFIED_NO_RULE")


# ----------------------------------------------------------------------------------------------- bills, alerts, analytics on stored data
class StoredBillCase(TempDbCase):
    """Real pipeline: synthetic PDFs -> reader -> store -> bill + analysis rows."""

    def conn(self):
        c = storage.connect(self.db)
        self._conns.append(c)
        return c

    def tearDown(self):
        for c in self._conns:                                   # Windows cannot delete a database file that is still open
            c.close()
        super().tearDown()

    def setUp(self):
        super().setUp()
        self.n = 0
        self._conns = []
        with storage.session(self.db) as c:
            for pid, plant, name, sup in ((1, "PU1", "Pune-1", "MSEDCL PUNE"), (2, "NA1", "Nashik-1", "MSEDCL NASHIK"), (3, "KT1", "Kottayam", "KSEB KOTTAYAM")):
                c.execute("INSERT INTO electricity_pos (po_header_id, po_number, supplier_name, plant_code, plant_name, billing_start, billing_end, first_seen, last_seen)"
                          " VALUES (?,?,?,?,?,?,?,?,?)", (pid, f"P{pid}", sup, plant, name, "2026-09-01", "2026-09-30", "x", "x"))

    def add(self, po=1, basis=None, **kw):
        self.n += 1
        att = 500 + self.n
        path = config.ORIGINALS_DIR / "t" / f"b{att}.pdf"
        synth.make_bill(path, **kw)
        with storage.session(self.db) as c:
            c.execute("INSERT INTO electricity_attachments (attached_document_id, po_header_id, entity_name, pk1_value, document_id, file_name, fetch_status, sha256, local_path,"
                      " first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (att, po, "PO_HEADERS", str(po), att, f"b{att}.pdf", "DOWNLOADED", f"sha{att}", f"originals/t/b{att}.pdf", "x", "x"))
        ext = store.process_attachment(att, db_path=self.db, run_ocr=False)["extraction_id"]
        if basis:
            store.set_billing_basis(ext, basis, "reviewer", db_path=self.db)
        return ext

    def bill_row(self, ext):
        return self.rows("SELECT * FROM electricity_bills WHERE extraction_id = ?", ext)[0]

    def pf_row(self, ext):
        return self.rows("SELECT p.* FROM electricity_pf_analysis p JOIN electricity_bills b ON b.id = p.bill_id WHERE b.extraction_id = ?", ext)[0]

    def dem_row(self, ext):
        return self.rows("SELECT d.* FROM electricity_demand_analysis d JOIN electricity_bills b ON b.id = d.bill_id WHERE b.extraction_id = ?", ext)[0]


class BillMaterialisationTests(StoredBillCase):
    def test_a_read_bill_becomes_a_bill_with_plant_account_period_and_analysis(self):
        ext = self.add(pf_penalty=5000.0)
        b = self.bill_row(ext)
        self.assertEqual((b["plant_code"], b["source"], b["billing_month"], b["kwh"], b["kvah"], b["pf_billed"]), ("PU1", "ORACLE_EBS", "2026-09", 90000.0, 100000.0, 0.9))
        self.assertEqual((b["pf_penalty"], b["pf_penalty_status"]), (5000.0, "FOUND"))
        self.assertIsNotNone(b["account_id"])
        self.assertEqual(self.pf_row(ext)["validation_status"], "MATCH")
        self.assertEqual(self.dem_row(ext)["utilization_pct"], 92.0)
        self.assertEqual(self.rows("SELECT consumer_number FROM electricity_consumer_accounts")[0]["consumer_number"], "123456789012")

    def test_zero_found_unreadable_and_not_found_are_distinct_for_penalties(self):
        zero = self.add(pf_penalty=0.0, consumer="111111111111", bill_no="Z-0001")
        none = self.add(consumer="222222222222", bill_no="N-0001")
        self.assertEqual((self.bill_row(zero)["pf_penalty"], self.bill_row(zero)["pf_penalty_status"]), (0.0, "ZERO"))
        self.assertEqual((self.bill_row(none)["pf_penalty"], self.bill_row(none)["pf_penalty_status"]), (None, "NOT_FOUND"))

    def test_a_correction_flows_into_the_bill_and_its_analysis(self):
        ext = self.add()
        self.assertEqual(self.pf_row(ext)["validation_status"], "MATCH")
        store.apply_correction(ext, "pf_billed", "0.95", "reviewer", db_path=self.db)
        self.assertEqual(self.bill_row(ext)["pf_billed"], 0.95)
        self.assertEqual(self.pf_row(ext)["validation_status"], "MISMATCH")              # 0.95 vs kWh/kVAh 0.90: now flagged
        store.apply_correction(ext, "max_demand", "120", "reviewer", unit="kVA", db_path=self.db)
        self.assertEqual(self.dem_row(ext)["exceeds_contract"], 1)

    def test_negative_adjustments_arrears_and_each_charge_category_are_stored_separately(self):
        ext = self.add(extra=[("Arrears", "25000.00"), ("Adjustment / Credit", "-1500.00"), ("Fuel Adjustment Charges", "8200.00"),
                              ("Wheeling Charges", "3100.00"), ("Late Payment Surcharge", "450.00")])
        b = self.bill_row(ext)
        self.assertEqual((b["arrears"], b["adjustments_credits"], b["fuel_adjustment"], b["wheeling"], b["late_payment"]), (25000.0, -1500.0, 8200.0, 3100.0, 450.0))
        self.assertEqual((b["energy_charges"], b["demand_charges"], b["electricity_duty"]), (700000.0, 45000.0, 12000.0))
        # a credit is not netted into another category, and the fuel adjustment is not mistaken for a credit
        self.assertNotEqual(b["adjustments_credits"], 6700.0)

    def test_leading_or_lagging_is_derived_from_the_reactive_readings_and_labelled_as_derived(self):
        ext = self.add()
        self.assertIsNone(self.pf_row(ext)["direction"])                                     # nothing printed, nothing guessed
        store.apply_correction(ext, "kvarh_lag", "2000", "reviewer", db_path=self.db)
        store.apply_correction(ext, "kvarh_lead", "3500", "reviewer", db_path=self.db)
        d = self.pf_row(ext)["direction"]
        self.assertTrue(d.startswith("LEAD") and "derived" in d, d)

    def test_the_billing_basis_set_by_a_person_changes_the_saving_status(self):
        ext = self.add()
        self.assertEqual(self.pf_row(ext)["savings_status"], "UNAVAILABLE_BASIS_UNKNOWN")
        store.set_billing_basis(ext, "kVAh", "reviewer", "tariff section says kVAh", db_path=self.db)
        self.assertEqual(self.pf_row(ext)["savings_status"], "ESTIMATE_UNVERIFIED_INPUTS")
        with self.assertRaises(ValueError):
            store.set_billing_basis(ext, "banana", "reviewer", db_path=self.db)
        self.assertEqual(self.rows("SELECT action FROM electricity_audit_logs WHERE action = 'SET_BILLING_BASIS'")[0]["action"], "SET_BILLING_BASIS")

    def test_revised_bills_are_all_kept_and_only_the_latest_counts(self):
        a = self.add(bill_no="B-0001", bill_date="03-10-2026", energy=700000.0)
        b = self.add(bill_no="B-0002", bill_date="10-10-2026", energy=650000.0)                    # revised bill, same account and month
        rows = {r["bill_number"]: r for r in self.rows("SELECT * FROM electricity_bills")}
        self.assertEqual((rows["B-0001"]["is_primary"], rows["B-0002"]["is_primary"], rows["B-0002"]["revision_count"]), (0, 1, 2))
        self.assertEqual(analytics.kpis(self.conn(), Filters())["bills"], 1)                   # not double counted
        self.assertEqual(len(self.rows("SELECT id FROM electricity_bills")), 2)                # but nothing was overwritten

    def test_two_meters_at_one_plant_are_separate_accounts_and_both_count(self):
        self.add(consumer="111111111111", bill_no="M-0001", kwh=90000, kvah=100000)
        self.add(consumer="222222222222", bill_no="M-0002", kwh=45000, kvah=50000)
        self.assertEqual(len(self.rows("SELECT id FROM electricity_consumer_accounts")), 2)
        c = self.conn()
        k = analytics.kpis(c, Filters())
        self.assertEqual(k["bills"], 2)
        self.assertEqual(k["overall_pf"], 0.9)                                                  # (90000 + 45000) / (100000 + 50000)
        d = analytics.plant_detail(c, "PU1", Filters())
        self.assertEqual(len(d["per_account"]), 2)                                              # meter-wise view as well as the combined one

    def test_bills_of_a_non_electricity_attachment_or_duplicate_get_no_bill_row(self):
        ext = self.add()
        self.assertEqual(len(self.rows("SELECT id FROM electricity_bills")), 1)
        # the same content on another attachment is a duplicate: no second bill
        att = 900
        with storage.session(self.db) as c:
            c.execute("INSERT INTO electricity_attachments (attached_document_id, po_header_id, entity_name, pk1_value, document_id, file_name, fetch_status, sha256, local_path,"
                      " first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (att, 1, "PO_HEADERS", "1", att, "dup.pdf", "DOWNLOADED", "sha501", "originals/t/b501.pdf", "x", "x"))
        self.assertEqual(store.process_attachment(att, db_path=self.db, run_ocr=False)["bill_status"], "DUPLICATE")
        self.assertEqual(len(self.rows("SELECT id FROM electricity_bills")), 1)


# ----------------------------------------------------------------------------------------------- alerts
class AlertTests(StoredBillCase):
    def alerts_(self, status="OPEN"):
        return self.rows("SELECT * FROM electricity_alerts WHERE status = ? ORDER BY id", status)

    def types(self):
        return sorted(a["alert_type"] for a in self.alerts_())

    def test_actual_penalties_raise_critical_alerts_with_the_billed_amount(self):
        self.add(pf_penalty=5000.0, xd_penalty=2500.0, pf=0.95, kwh=95000, kvah=100000)
        al = {a["alert_type"]: a for a in self.alerts_()}
        self.assertEqual(al["PF_PENALTY_ACTUAL"]["severity"], "CRITICAL")
        self.assertIn("5,000.00", al["PF_PENALTY_ACTUAL"]["title"])
        self.assertEqual(al["EXCESS_DEMAND_PENALTY_ACTUAL"]["severity"], "CRITICAL")
        self.assertIn("unverified", al["PF_PENALTY_ACTUAL"]["title"])

    def test_low_pf_alert_severity_depends_on_verification(self):
        ext = self.add()                                                                       # PF 0.90 -> ATTENTION band
        a = [x for x in self.alerts_() if x["alert_type"] == "PF_BELOW_THRESHOLD"][0]
        self.assertEqual(a["severity"], "INFO")                                                # unverified + attention => only informational
        ext2 = self.add(consumer="333333333333", bill_no="L-0001", kwh=70000, kvah=100000, pf=0.70)
        crit = [x for x in self.alerts_() if x["alert_type"] == "PF_BELOW_THRESHOLD" and "0.70" in x["title"]][0]
        self.assertEqual(crit["severity"], "WARNING")                                          # critical PF but unverified => warning, not critical
        for f in ("pf", "pf_billed", "kwh", "kvah", "consumer_number", "billing_period", "total_payable", "contract_demand", "max_demand", "bill_date"):
            store.mark_verified(ext2, f, "reviewer", db_path=self.db)
        self.assertEqual(self.rows("SELECT quality FROM electricity_bills WHERE extraction_id = ?", ext2)[0]["quality"], "VERIFIED")
        crit = [x for x in self.alerts_() if x["alert_type"] == "PF_BELOW_THRESHOLD" and "0.70" in x["title"]][0]
        self.assertEqual(crit["severity"], "CRITICAL")

    def test_inconsistent_pf_raises_an_alert_and_a_correction_resolves_it(self):
        ext = self.add(kwh=78000, kvah=100000, pf=0.95)
        self.assertIn("PF_INCONSISTENT", self.types())
        store.apply_correction(ext, "kwh", "95000", "reviewer", db_path=self.db)
        self.assertNotIn("PF_INCONSISTENT", self.types())
        resolved = self.alerts_("RESOLVED")
        self.assertTrue(any(a["alert_type"] == "PF_INCONSISTENT" and "no longer present" in a["resolution_note"] for a in resolved))

    def test_the_same_condition_is_one_alert_not_one_per_scan(self):
        ext = self.add(pf_penalty=5000.0)
        with storage.session(self.db) as c:
            for _ in range(5):
                bills.refresh_bill(c, ext)                                                     # what every 2-hourly pass effectively does
        pen = [a for a in self.alerts_() if a["alert_type"] == "PF_PENALTY_ACTUAL"]
        self.assertEqual(len(pen), 1)
        self.assertGreaterEqual(pen[0]["occurrences"], 5)

    def test_contract_demand_alerts_follow_the_tariff_rule(self):
        ext = self.add(cd=100, md=108)
        a = [x for x in self.alerts_() if x["alert_type"] == "CONTRACT_DEMAND_EXCEEDED"][0]
        self.assertEqual(a["severity"], "INFO")                                                # no approved rule => not a confirmed violation
        with storage.session(self.db) as c:
            rid = tariff.create_rule(c, {"state": "Maharashtra", "effective_from": "2026-04-01", "contract_demand_tolerance_pct": 5, "source_reference": "MERC 2026"}, "admin")
            tariff.set_approval(c, rid, "APPROVED", "admin")
            bills.reanalyze_all(c)
            bills.refresh_bill(c, ext)
        a = [x for x in self.alerts_() if x["alert_type"] == "CONTRACT_DEMAND_EXCEEDED"][0]
        self.assertEqual(a["severity"], "CRITICAL")

    def test_acknowledge_and_resolve(self):
        self.add(pf_penalty=5000.0)
        aid = [a for a in self.alerts_() if a["alert_type"] == "PF_PENALTY_ACTUAL"][0]["id"]
        with storage.session(self.db) as c:
            self.assertEqual(alerts.acknowledge(c, aid, "dj"), 1)
            self.assertEqual(alerts.acknowledge(c, aid, "dj"), 0)                              # already acknowledged
        self.assertEqual(self.rows("SELECT status, acknowledged_by FROM electricity_alerts WHERE id = ?", aid)[0]["status"], "ACKNOWLEDGED")
        with storage.session(self.db) as c:
            alerts.resolve(c, aid, "dj", "penalty disputed with the board")
        self.assertEqual(self.rows("SELECT status FROM electricity_alerts WHERE id = ?", aid)[0]["status"], "RESOLVED")

    def test_a_resolved_condition_that_returns_creates_a_new_alert(self):
        ext = self.add(pf_penalty=5000.0)
        aid = [a for a in self.alerts_() if a["alert_type"] == "PF_PENALTY_ACTUAL"][0]["id"]
        with storage.session(self.db) as c:
            alerts.resolve(c, aid, "dj", "handled")
            bills.refresh_bill(c, ext)
        self.assertEqual(len([a for a in self.alerts_() if a["alert_type"] == "PF_PENALTY_ACTUAL"]), 1)
        self.assertNotEqual([a for a in self.alerts_() if a["alert_type"] == "PF_PENALTY_ACTUAL"][0]["id"], aid)

    def test_failed_extraction_alert(self):
        bad = config.ORIGINALS_DIR / "t" / "broken.pdf"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"%PDF-1.4 this is not a pdf at all")
        with storage.session(self.db) as c:
            c.execute("INSERT INTO electricity_attachments (attached_document_id, po_header_id, entity_name, pk1_value, document_id, file_name, fetch_status, sha256, local_path,"
                      " first_seen, last_seen) VALUES (700, 1, 'PO_HEADERS', '1', 700, 'broken.pdf', 'DOWNLOADED', 'shabad', 'originals/t/broken.pdf', 'x', 'x')")
        r = store.process_attachment(700, db_path=self.db, run_ocr=False)
        self.assertIn(r["bill_status"], ("UNSUPPORTED", "FAILED"))
        self.assertIn("EXTRACTION_FAILED", self.types())

    def test_duplicate_alert(self):
        self.add()
        with storage.session(self.db) as c:
            c.execute("INSERT INTO electricity_attachments (attached_document_id, po_header_id, entity_name, pk1_value, document_id, file_name, fetch_status, sha256, local_path,"
                      " first_seen, last_seen) VALUES (701, 1, 'PO_HEADERS', '1', 701, 'dup.pdf', 'DOWNLOADED', 'sha501', 'originals/t/b501.pdf', 'x', 'x')")
        store.process_attachment(701, db_path=self.db, run_ocr=False)
        self.assertIn("DUPLICATE_BILL", self.types())

    def test_missing_pf_and_energy_alerts(self):
        # a bill that prints neither PF nor kWh/kVAh
        self.n += 1
        att = 800
        from helpers import make_pdf, pad
        make_pdf(config.ORIGINALS_DIR / "t" / "nopf.pdf", pad([(40, 40, "Maharashtra State Electricity Distribution Company Ltd (MSEDCL)", 10), (40, 80, "Consumer Number"), (330, 80, "123456789012"),
                                                              (40, 98, "Bill Date"), (330, 98, "03-10-2026"), (40, 116, "Billing Period"), (330, 116, "01-09-2026 to 30-09-2026"),
                                                              (40, 134, "Total Payable"), (330, 134, "50000.00"), (40, 152, "Energy Charges"), (330, 152, "40000.00"), (40, 170, "Electricity Duty"), (330, 170, "2000.00")]))
        with storage.session(self.db) as c:
            c.execute("INSERT INTO electricity_attachments (attached_document_id, po_header_id, entity_name, pk1_value, document_id, file_name, fetch_status, sha256, local_path,"
                      " first_seen, last_seen) VALUES (800, 1, 'PO_HEADERS', '1', 800, 'nopf.pdf', 'DOWNLOADED', 'shanopf', 'originals/t/nopf.pdf', 'x', 'x')")
        store.process_attachment(800, db_path=self.db, run_ocr=False)
        self.assertIn("MISSING_PF", self.types())
        self.assertIn("MISSING_ENERGY", self.types())

    def test_missing_expected_bill_follows_a_configured_schedule_only(self):
        self.add()
        with storage.session(self.db) as c:
            alerts.evaluate_missing_bills(c, date(2027, 6, 1))
        self.assertNotIn("MISSING_EXPECTED_BILL", self.types())                                # no schedule => never assumes monthly
        with storage.session(self.db) as c:
            c.execute("UPDATE electricity_consumer_accounts SET schedule_enabled = 1, expected_every_days = 31, grace_days = 7")
            alerts.evaluate_missing_bills(c, date(2026, 10, 20))                                # 30 Sep + 31 + 7 = 7 Nov: not yet due
        self.assertNotIn("MISSING_EXPECTED_BILL", self.types())
        with storage.session(self.db) as c:
            alerts.evaluate_missing_bills(c, date(2026, 11, 8))
        self.assertIn("MISSING_EXPECTED_BILL", self.types())
        self.add(start="01-10-2026", end="31-10-2026", bill_no="B-OCT", bill_date="03-11-2026")  # the bill arrives
        with storage.session(self.db) as c:
            alerts.evaluate_missing_bills(c, date(2026, 11, 8))
        self.assertNotIn("MISSING_EXPECTED_BILL", self.types())

    def test_scheduler_failure_alert_after_repeated_failures(self):
        with storage.session(self.db) as c:
            alerts.evaluate_scheduler(c, 2, "ORA-12170")
        self.assertNotIn("SCHEDULER_FAILURE", self.types())
        with storage.session(self.db) as c:
            alerts.evaluate_scheduler(c, 3, "ORA-12170")
        self.assertIn("SCHEDULER_FAILURE", self.types())
        with storage.session(self.db) as c:
            alerts.evaluate_scheduler(c, 0, "")
        self.assertNotIn("SCHEDULER_FAILURE", self.types())

    def test_download_failure_alert_clears_when_the_file_arrives(self):
        self.add()
        with storage.session(self.db) as c:
            c.execute("UPDATE electricity_attachments SET fetch_status = 'FAILED', fetch_error = 'ORA-03113', retry_count = 2 WHERE attached_document_id = 501")
            alerts.evaluate_download_failures(c)
        self.assertIn("DOWNLOAD_FAILURE", self.types())
        with storage.session(self.db) as c:
            c.execute("UPDATE electricity_attachments SET fetch_status = 'DOWNLOADED' WHERE attached_document_id = 501")
            alerts.evaluate_download_failures(c)
        self.assertNotIn("DOWNLOAD_FAILURE", self.types())


# ----------------------------------------------------------------------------------------------- 3.6 analytics
class AnalyticsTests(StoredBillCase):
    def setUp(self):
        super().setUp()
        self.add(po=1, consumer="1" * 12, bill_no="A-0001", start="01-08-2026", end="31-08-2026", kwh=90000, kvah=100000, pf=0.90, energy=700000.0)
        self.add(po=1, consumer="1" * 12, bill_no="A-0002", start="01-09-2026", end="30-09-2026", kwh=95000, kvah=100000, pf=0.95, energy=720000.0, pf_penalty=2000.0)
        self.add(po=2, consumer="2" * 12, bill_no="B-0003", start="01-09-2026", end="30-09-2026", kwh=60000, kvah=100000, pf=0.60, energy=500000.0)
        self.add(po=3, consumer="3" * 12, bill_no="C-0001", start="01-09-2026", end="30-09-2026", kwh=108900, kvah=110000, pf=0.99, energy=600000.0,
                 discom_line="Kerala State Electricity Board Ltd (KSEB)")
        self.c = self.conn()

    def test_company_pf_is_energy_weighted_not_the_mean_of_plant_pfs(self):
        k = analytics.kpis(self.c, Filters())
        self.assertEqual(k["overall_pf"], round((90000 + 95000 + 60000 + 108900) / (100000 * 3 + 110000), 4))     # 353900 / 410000 = 0.8632
        k2 = analytics.kpis(self.c, Filters(month_from="2026-09", month_to="2026-09"))
        self.assertEqual(k2["overall_pf"], round((95000 + 60000 + 108900) / 310000, 4))                              # 0.8513 (energy-weighted)
        self.assertEqual(round(k2["mean_billed_pf"], 4), round((0.95 + 0.60 + 0.99) / 3, 4))                         # 0.8467 (plain mean) - a different number

    def test_actual_penalties_and_savings_are_separate_totals(self):
        k = analytics.kpis(self.c, Filters())
        self.assertEqual(k["pf_penalty"], 2000.0)
        self.assertEqual(k["pf_penalty_bills"], 1)
        self.assertEqual(k["savings"], 0.0)                                                      # basis Unknown for every synthetic bill => no estimate at all
        store.set_billing_basis(2, "kVAh", "reviewer", db_path=self.db)                          # PU1 September (0.95 -> 0.99)
        with storage.session(self.db) as c:
            k = analytics.kpis(c, Filters())
        self.assertGreater(k["savings"], 0)
        self.assertEqual(k["pf_penalty"], 2000.0)                                                # unchanged by the estimate

    def test_plants_below_target_and_worst_best(self):
        m = analytics.management(self.c, Filters(month_from="2026-09", month_to="2026-09"))
        self.assertEqual([p["plant_code"] for p in m["worst"]][:2], ["NA1", "PU1"])
        self.assertEqual(m["best"][0]["plant_code"], "KT1")
        self.assertEqual(sorted(p["plant_code"] for p in m["plants_below_target"]), ["NA1", "PU1"])      # KT1 is exactly at the 0.99 target: not below it

    def test_monthly_trend_and_mom_cost(self):
        mo = analytics.monthly(self.c, Filters())
        self.assertEqual([m["month"] for m in mo], ["2026-08", "2026-09"])
        self.assertEqual(mo[0]["pf"], 0.9)
        self.assertEqual(mo[1]["pf"], round(263900 / 310000, 4))
        self.assertIsNotNone(mo[1]["cost_change_pct"])
        self.assertAlmostEqual(mo[0]["cost_per_kwh"], round(mo[0]["cost_k"] / 90000, 3), places=3)

    def test_periods_end_at_the_latest_month_with_data(self):
        self.assertEqual(analytics.resolve_period(self.c, "current")[:2], ("2026-09", "2026-09"))
        self.assertEqual(analytics.resolve_period(self.c, "previous")[:2], ("2026-08", "2026-08"))
        self.assertEqual(analytics.resolve_period(self.c, "last3")[:2], ("2026-07", "2026-09"))
        self.assertEqual(analytics.resolve_period(self.c, "last12")[:2], ("2025-10", "2026-09"))
        self.assertEqual(analytics.resolve_period(self.c, "custom", "2026-01", "2026-03")[:2], ("2026-01", "2026-03"))

    def test_state_and_board_performance(self):
        st = {s["grp"]: s for s in analytics.group_performance(self.c, Filters(), "state")}
        self.assertEqual(st["Maharashtra"]["bills"], 3)
        self.assertEqual(st["Kerala"]["pf"], 0.99)
        self.assertEqual({s["grp"] for s in analytics.group_performance(self.c, Filters(), "discom")}, {"MSEDCL", "KSEB"})

    def test_filters_sorting_and_paging_run_in_the_database(self):
        t = analytics.bill_table(self.c, Filters(plants=["PU1"]), sort="pf", direction="asc", page=1, per_page=5)
        self.assertEqual((t["total"], [r["plant_code"] for r in t["rows"]]), (2, ["PU1", "PU1"]))
        self.assertLessEqual(t["rows"][0]["pf_used"], t["rows"][1]["pf_used"])
        self.assertEqual(analytics.bill_table(self.c, Filters(exception="pf_penalty"))["total"], 1)
        self.assertEqual(analytics.bill_table(self.c, Filters(pf_status="CRITICAL"))["total"], 1)
        self.assertEqual(analytics.bill_table(self.c, Filters(search="C-0001"))["total"], 1)
        self.assertEqual(analytics.bill_table(self.c, Filters(states=["Kerala"]))["total"], 1)
        t = analytics.bill_table(self.c, Filters(), per_page=1)                       # page size is clamped to a sane minimum
        self.assertEqual((t["per_page"], t["pages"], t["total"]), (5, 1, 4))
        self.assertEqual(analytics.bill_table(self.c, Filters(), page=2)["rows"], [])

    def test_a_search_with_sql_in_it_is_just_text(self):
        t = analytics.bill_table(self.c, Filters(search="x'; DROP TABLE electricity_bills; --"))
        self.assertEqual(t["total"], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM electricity_bills").fetchone()[0], 4)

    def test_role_scope_limits_every_figure(self):
        self.assertEqual(analytics.kpis(self.c, Filters(allowed_plants=["KT1"]))["bills"], 1)
        self.assertEqual(analytics.kpis(self.c, Filters(allowed_plants=[]))["bills"], 0)
        self.assertEqual({p["plant_code"] for p in analytics.filter_options(self.c, ["NA1"])["plants"]}, {"NA1"})

    def test_contradictory_figures_are_left_out_of_company_totals_and_counted(self):
        # kVAh misread 100x larger on one bill: its pair must not drag the company PF down
        self.add(po=2, consumer="9" * 12, bill_no="X-0001", start="01-09-2026", end="30-09-2026", kwh=1330, kvah=108034, pf=0.98, energy=100000.0)
        k = analytics.kpis(self.conn(), Filters(month_from="2026-09", month_to="2026-09"))
        self.assertEqual(k["overall_pf"], round((95000 + 60000 + 108900) / 310000, 4))
        self.assertEqual(k["pairs_excluded"], 1)

    def test_verified_only_mode(self):
        self.assertEqual(analytics.kpis(self.c, Filters(quality="verified"))["bills"], 0)


# ----------------------------------------------------------------------------------------------- e-mail rules
class MailTests(StoredBillCase):
    SMTP = SmtpSettings("mail.example", 587, "no-reply@example.com", "secret-pw", True)

    def setUp(self):
        super().setUp()
        self.sent = []
        self.transport = lambda smtp, msg, rcpt: self.sent.append((msg, rcpt))
        with storage.session(self.db) as c:
            settings.set_many(c, {"alert_email_enabled": "true", "alert_email_to": "a@x.in, b@x.in", "alert_email_min_severity": "WARNING",
                                  "alert_renotify_hours": "72", "escalation_hours": "48", "escalation_to": "boss@x.in"})
        self.add(pf_penalty=5000.0)

    def send(self, now=None):
        with storage.session(self.db) as c:
            return notify.send_pending(c, smtp=self.SMTP, transport=self.transport, now=now or datetime.now())

    def test_one_digest_with_the_required_content(self):
        r = self.send()
        self.assertGreaterEqual(r["sent"], 1)
        self.assertEqual(len(self.sent), 1)
        msg, rcpt = self.sent[0]
        self.assertEqual(rcpt, ["a@x.in", "b@x.in"])
        body = msg.get_body(("html",)).get_content()
        for needle in ("PU1", "MSEDCL", "2026-09", "B-1001", "P1", "0.90", "100 kVA", "92 kVA", "PF penalty Rs 5,000.00", "Problem", "Required action", "/bill/"):
            self.assertIn(needle, body)
        self.assertNotIn("secret-pw", msg.as_string())                                          # credentials never in a message

    def test_an_alert_is_not_re_sent_every_scan(self):
        self.send()
        self.send()
        self.send()
        self.assertEqual(len(self.sent), 1)

    def test_renotify_after_the_configured_hours_only_while_unresolved(self):
        digests = lambda: [m for m, r in self.sent if r == ["a@x.in", "b@x.in"]]
        self.send()
        self.assertEqual(len(digests()), 1)
        self.send(now=datetime.now() + timedelta(hours=40))
        self.assertEqual(len(digests()), 1)                                                     # before the 72 h
        self.send(now=datetime.now() + timedelta(hours=73))
        self.assertEqual(len(digests()), 2)
        with storage.session(self.db) as c:
            c.execute("UPDATE electricity_alerts SET status = 'ACKNOWLEDGED'")
        self.send(now=datetime.now() + timedelta(hours=200))
        self.assertEqual(len(digests()), 2)                                                     # acknowledged => silent

    def test_critical_alert_escalates_once(self):
        self.send(now=datetime.now() + timedelta(hours=49))
        esc = [m for m, r in self.sent if r == ["boss@x.in"]]
        self.assertEqual(len(esc), 1)
        self.send(now=datetime.now() + timedelta(hours=60))
        self.assertEqual(len([m for m, r in self.sent if r == ["boss@x.in"]]), 1)

    def test_minimum_severity_filter(self):
        with storage.session(self.db) as c:
            settings.set_many(c, {"alert_email_min_severity": "CRITICAL"})
            c.execute("UPDATE electricity_alerts SET last_emailed_at = NULL")
        self.send()
        body = self.sent[0][0].get_body(("html",)).get_content()
        self.assertIn("CRITICAL", body)
        self.assertNotIn("is below target", body)

    def test_switched_off_or_unconfigured_sends_nothing(self):
        with storage.session(self.db) as c:
            settings.set_many(c, {"alert_email_enabled": "false"})
        self.assertIsNotNone(self.send()["skipped"])
        with storage.session(self.db) as c:
            settings.set_many(c, {"alert_email_enabled": "true"})
            r = notify.send_pending(c, smtp=SmtpSettings("", 587, "", "", True), transport=self.transport)
        self.assertEqual(r["skipped"], "SMTP is not configured")
        self.assertEqual(self.sent, [])

    def test_a_mail_failure_is_recorded_and_retried_later(self):
        def boom(*a):
            raise OSError("connection refused")
        with storage.session(self.db) as c:
            r = notify.send_pending(c, smtp=self.SMTP, transport=boom)
        self.assertGreaterEqual(r["failed"], 1)
        self.assertEqual(self.rows("SELECT status FROM electricity_notifications")[0]["status"], "FAILED")
        self.assertIsNone(self.rows("SELECT last_emailed_at FROM electricity_alerts")[0]["last_emailed_at"])
        self.assertGreaterEqual(self.send()["sent"], 1)                                          # next run succeeds

    def test_settings_validation(self):
        with storage.session(self.db) as c:
            with self.assertRaises(ValueError):
                settings.set_many(c, {"alert_email_to": "not-an-address"})
            with self.assertRaises(ValueError):
                settings.set_many(c, {"pf_good": "0.85"})                                         # below pf_attention 0.90: bands must be ordered
            with self.assertRaises(ValueError):
                settings.set_many(c, {"scan_interval_minutes": "0"})


if __name__ == "__main__":
    unittest.main()

