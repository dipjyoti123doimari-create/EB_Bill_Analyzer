"""
analytics.py
============
Management analytics (Phase 3.6) and the server-side queries behind the dashboard, the plant table and the reports.

Principles
  * Only the PRIMARY bill of an (account, month) counts, so a revised or duplicate bill is not counted twice.
  * Overall / plant PF is ENERGY-WEIGHTED: total kWh / total kVAh over bills that have both. The plain mean of billed PFs is reported
    separately and labelled as such; the two are never mixed.
  * ACTUAL penalties (printed on the bills) and ESTIMATED savings (theoretical kVAh) are separate figures everywhere.
  * Every figure says how many bills it rests on and how many of them are still unverified.
  * "Current month" means the latest billing month present in the data (bills for a month arrive after it ends), and the
    longer windows end there.
"""

from dataclasses import dataclass, field
from datetime import date

from . import settings

# A kWh/kVAh pair is used for an energy-weighted PF only when it is plausible (0.30 <= kWh/kVAh <= 1) and agrees with the PF printed on
# the bill (or the bill prints none). A cost counts only when the bill's own figures do not contradict each other. Excluded bills stay
# visible in the tables and are counted in the KPI notes: a total must never be built on a figure the bill itself contradicts.
VALID_PAIR = ("(b.kwh > 0 AND b.kvah > 0 AND b.data_flags NOT LIKE '%,PF_PAIR_IMPLAUSIBLE,%' "
              "AND COALESCE(pf.validation_status, '') IN ('MATCH', 'NOT_COMPARABLE_NO_BILLED_PF'))")
COST = "(CASE WHEN b.data_flags NOT LIKE '%,COST_CONFLICT,%' THEN b.cost_amount END)"
PF_BANDS = ("EXCELLENT", "GOOD", "ATTENTION", "CRITICAL", "UNKNOWN")
EXCEPTIONS = {
    "pf_below": "pf.performance_status IN ('ATTENTION','CRITICAL')",
    "pf_critical": "pf.performance_status = 'CRITICAL'",
    "pf_penalty": "b.pf_penalty > 0",
    "demand_violation": "d.violation_status IN ('VIOLATION','VIOLATION_BILLED')",
    "demand_above_contract": "d.exceeds_contract = 1",
    "excess_penalty": "b.excess_demand_penalty > 0",
    "needs_review": "b.bill_status IN ('NEEDS_REVIEW','PARTIAL')",
    "missing_pf": "pf.validation_status = 'NO_PF'",
    "pf_mismatch": "pf.validation_status IN ('MISMATCH','INVALID_ENERGY')",
    "penalty_unreadable": "(b.pf_penalty_status = 'UNREADABLE' OR b.excess_demand_status = 'UNREADABLE')",
    "revised": "b.revision_count > 1",
}
SORTS = {
    "plant": "b.plant_code", "state": "b.state", "discom": "b.discom_code", "period": "b.period_end", "month": "b.billing_month", "basis": "b.billing_basis",
    "kwh": "b.kwh", "kvah": "b.kvah", "pf": "COALESCE(pf.pf_used, 0)", "cd": "b.contract_demand", "md": "b.max_demand",
    "pf_penalty": "b.pf_penalty", "demand_penalty": "b.excess_demand_penalty", "total": "b.total_payable", "status": "b.bill_status",
}


def month_shift(ym: str, n: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    t = y * 12 + (m - 1) + n
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def latest_month(conn) -> str:
    r = conn.execute("SELECT MAX(billing_month) FROM electricity_bills WHERE is_primary = 1").fetchone()[0]
    return r or date.today().strftime("%Y-%m")


def resolve_period(conn, kind: str, custom_from: str | None = None, custom_to: str | None = None):
    """(month_from, month_to, label) for current / previous / last3 / last6 / last12 / custom / all."""
    a = latest_month(conn)
    if kind == "current":
        return a, a, f"Current month ({a})"
    if kind == "previous":
        p = month_shift(a, -1)
        return p, p, f"Previous month ({p})"
    if kind in ("last3", "last6", "last12"):
        n = int(kind[4:])
        return month_shift(a, -(n - 1)), a, f"Last {n} months ({month_shift(a, -(n - 1))} to {a})"
    if kind == "custom" and custom_from and custom_to:
        return custom_from, custom_to, f"{custom_from} to {custom_to}"
    return None, None, "All periods"


@dataclass
class Filters:
    plants: list = field(default_factory=list)
    states: list = field(default_factory=list)
    discoms: list = field(default_factory=list)
    month_from: str | None = None
    month_to: str | None = None
    basis: str | None = None
    pf_status: str | None = None
    exception: str | None = None
    search: str | None = None
    quality: str = "all"                    # 'all' = every machine-read bill; 'verified' = only bills a person verified
    source: str | None = None
    allowed_plants: list | None = None      # role scope; None = every plant
    primary_only: bool = True               # False = also list revised / superseded bills (used by the register)

    def where(self, with_period=True):
        sql, p = (["b.is_primary = 1"] if self.primary_only else ["1 = 1"]), {}
        # bills of a PO that the identification rules now exclude do not count (the rows stay; a rule change is reversible)
        sql.append("(b.po_header_id IS NULL OR EXISTS (SELECT 1 FROM electricity_pos po WHERE po.po_header_id = b.po_header_id AND po.eligible = 1))")

        def in_list(col, vals, prefix):
            if vals:
                keys = [f":{prefix}{i}" for i in range(len(vals))]
                p.update({f"{prefix}{i}": v for i, v in enumerate(vals)})
                sql.append(f"{col} IN ({','.join(keys)})")

        in_list("b.plant_code", self.plants, "pl")
        in_list("b.state", self.states, "st")
        in_list("b.discom_code", self.discoms, "dc")
        if self.allowed_plants is not None:
            if not self.allowed_plants:
                sql.append("1 = 0")
            else:
                in_list("b.plant_code", self.allowed_plants, "al")
        if with_period and self.month_from:
            sql.append("b.billing_month >= :mf")
            p["mf"] = self.month_from
        if with_period and self.month_to:
            sql.append("b.billing_month <= :mt")
            p["mt"] = self.month_to
        if self.basis:
            sql.append("b.billing_basis = :basis")
            p["basis"] = self.basis
        if self.pf_status in PF_BANDS:
            sql.append("COALESCE(pf.performance_status, 'UNKNOWN') = :pfs")
            p["pfs"] = self.pf_status
        if self.exception in EXCEPTIONS:
            sql.append(EXCEPTIONS[self.exception])
        if self.quality == "verified":
            sql.append("b.quality = 'VERIFIED'")
        if self.source in ("ORACLE_EBS", "MANUAL_UPLOAD"):
            sql.append("b.source = :src")
            p["src"] = self.source
        if self.search:
            sql.append("(b.plant_code LIKE :q OR pl.plant_name LIKE :q OR b.consumer_number LIKE :q OR b.bill_number LIKE :q OR b.po_number LIKE :q "
                       "OR b.discom_code LIKE :q OR b.state LIKE :q)")
            p["q"] = f"%{self.search.strip()}%"
        return " AND ".join(sql), p

    @classmethod
    def from_args(cls, args, allowed_plants=None, conn=None):
        g = lambda k: [x for x in args.getlist(k) if x]
        period = args.get("period", "")
        mf, mt = args.get("month_from") or None, args.get("month_to") or None
        if conn is not None and period in ("current", "previous", "last3", "last6", "last12"):
            mf, mt, _ = resolve_period(conn, period)
        elif args.get("year") and not (mf or mt):
            y = args.get("year")
            mo = args.get("month")
            if y.isdigit():
                mf, mt = (f"{y}-{int(mo):02d}", f"{y}-{int(mo):02d}") if mo and mo.isdigit() else (f"{y}-01", f"{y}-12")
        return cls(plants=g("plant"), states=g("state"), discoms=g("discom"), month_from=mf, month_to=mt, basis=args.get("basis") or None,
                   pf_status=args.get("pf_status") or None, exception=args.get("exception") or None, search=(args.get("q") or "").strip() or None,
                   quality=args.get("quality") if args.get("quality") in ("all", "verified") else "all", source=args.get("source") or None,
                   allowed_plants=allowed_plants)


FROM = ("FROM electricity_bills b LEFT JOIN electricity_pf_analysis pf ON pf.bill_id = b.id LEFT JOIN electricity_demand_analysis d ON d.bill_id = b.id "
        "LEFT JOIN electricity_plants pl ON pl.plant_code = b.plant_code")


def _one(conn, sql, p):
    return conn.execute(sql, p).fetchone()


def _ratio(a, b):
    return round(a / b, 4) if a is not None and b else None


# --------------------------------------------------------------------------- headline numbers
def kpis(conn, f: Filters) -> dict:
    w, p = f.where()
    r = _one(conn, f"""SELECT COUNT(*) AS bills, COUNT(DISTINCT b.plant_code) AS plants, SUM(b.quality = 'UNVERIFIED') AS unverified,
        SUM(CASE WHEN {VALID_PAIR} THEN b.kwh END) AS pair_kwh, SUM(CASE WHEN {VALID_PAIR} THEN b.kvah END) AS pair_kvah,
        SUM(CASE WHEN {VALID_PAIR} THEN 1 ELSE 0 END) AS pair_bills,
        AVG(b.pf_billed) AS mean_billed_pf, SUM(b.pf_billed IS NOT NULL) AS pf_bills,
        SUM(CASE WHEN d.violation_status = 'VIOLATION' THEN 1 ELSE 0 END) AS violations,
        SUM(CASE WHEN d.violation_status = 'VIOLATION_BILLED' THEN 1 ELSE 0 END) AS violations_billed,
        SUM(CASE WHEN d.exceeds_contract = 1 AND d.violation_status LIKE 'UNVERIFIED%' THEN 1 ELSE 0 END) AS above_cd_unverified,
        SUM(CASE WHEN b.pf_penalty > 0 THEN b.pf_penalty ELSE 0 END) AS pf_penalty, SUM(b.pf_penalty > 0) AS pf_penalty_bills,
        SUM(b.pf_penalty_status = 'UNREADABLE') AS pf_penalty_unreadable,
        SUM(CASE WHEN b.excess_demand_penalty > 0 THEN b.excess_demand_penalty ELSE 0 END) AS xd_penalty, SUM(b.excess_demand_penalty > 0) AS xd_bills,
        SUM(b.excess_demand_status = 'UNREADABLE') AS xd_unreadable,
        SUM(CASE WHEN pf.savings_status LIKE 'ESTIMATE%' THEN pf.estimated_savings ELSE 0 END) AS savings,
        SUM(CASE WHEN pf.savings_status LIKE 'ESTIMATE%' THEN 1 ELSE 0 END) AS savings_bills,
        SUM(CASE WHEN pf.savings_status = 'ESTIMATE_UNVERIFIED_INPUTS' THEN 1 ELSE 0 END) AS savings_unverified,
        SUM(CASE WHEN b.kwh > 0 AND b.kvah > 0 AND NOT {VALID_PAIR} THEN 1 ELSE 0 END) AS pairs_excluded,
        SUM(CASE WHEN b.data_flags LIKE '%,COST_CONFLICT,%' THEN 1 ELSE 0 END) AS cost_excluded, SUM({COST}) AS cost
        {FROM} WHERE {w}""", p)
    out = dict(r)
    out["overall_pf"] = _ratio(out["pair_kwh"], out["pair_kvah"])
    out["mean_billed_pf"] = round(out["mean_billed_pf"], 4) if out["mean_billed_pf"] is not None else None
    for k in ("unverified", "violations", "violations_billed", "above_cd_unverified", "pf_penalty_bills", "pf_penalty_unreadable", "xd_bills", "xd_unreadable",
              "savings_bills", "savings_unverified", "pair_bills", "pf_bills", "pairs_excluded", "cost_excluded"):
        out[k] = out[k] or 0
    for k in ("pf_penalty", "xd_penalty", "savings", "cost"):
        out[k] = round(out[k] or 0.0, 2)
    cfg = settings.get_all(conn)
    plants = plant_pf(conn, f)
    out["plants_low_pf"] = sum(1 for x in plants if x["pf"] is not None and x["pf"] < cfg["pf_good"])
    out["plants_critical_pf"] = sum(1 for x in plants if x["pf"] is not None and x["pf"] < cfg["pf_attention"])
    out["plants_with_pf"] = sum(1 for x in plants if x["pf"] is not None)
    out["plants_total_known"] = _one(conn, "SELECT COUNT(DISTINCT plant_code) FROM electricity_pos WHERE eligible = 1 AND plant_code IS NOT NULL", {})[0]
    out["needs_review_total"] = _one(conn, "SELECT COUNT(*) FROM electricity_bill_extractions WHERE archived = 0 AND bill_status IN ('NEEDS_REVIEW','PARTIAL')", {})[0]
    out["failed_total"] = _one(conn, "SELECT COUNT(*) FROM electricity_bill_extractions WHERE archived = 0 AND bill_status IN ('FAILED','UNSUPPORTED')", {})[0]
    last = _one(conn, "SELECT finished_at, mode FROM electricity_scan_runs WHERE status = 'SUCCESS' ORDER BY id DESC LIMIT 1", {})
    out["last_scan"] = dict(last) if last else None
    out["pf_targets"] = {"target": cfg["target_pf"], "good": cfg["pf_good"], "attention": cfg["pf_attention"]}
    return out


def plant_pf(conn, f: Filters) -> list:
    """Per plant: energy-weighted PF (kWh/kVAh) where bills have both, else the mean of billed PFs (method says which)."""
    w, p = f.where()
    rows = conn.execute(f"""SELECT b.plant_code, MIN(pl.plant_name) AS plant_name, MIN(b.state) AS state, MIN(b.discom_code) AS discom, COUNT(*) AS bills,
        SUM(b.quality = 'UNVERIFIED') AS unverified,
        SUM(CASE WHEN {VALID_PAIR} THEN b.kwh END) AS kwh, SUM(CASE WHEN {VALID_PAIR} THEN b.kvah END) AS kvah,
        SUM(CASE WHEN {VALID_PAIR} THEN 1 ELSE 0 END) AS pair_bills,
        AVG(b.pf_billed) AS mean_pf, SUM(b.pf_billed IS NOT NULL) AS pf_bills,
        SUM(CASE WHEN b.pf_penalty > 0 THEN b.pf_penalty ELSE 0 END) AS pf_penalty,
        SUM(CASE WHEN b.excess_demand_penalty > 0 THEN b.excess_demand_penalty ELSE 0 END) AS xd_penalty,
        SUM(CASE WHEN d.violation_status IN ('VIOLATION','VIOLATION_BILLED') THEN 1 ELSE 0 END) AS violations,
        SUM(CASE WHEN d.comparison_status = 'COMPARABLE' THEN 1 ELSE 0 END) AS demand_comparable,
        SUM(CASE WHEN d.exceeds_contract = 1 THEN 1 ELSE 0 END) AS above_cd,
        SUM(CASE WHEN pf.savings_status LIKE 'ESTIMATE%' THEN pf.estimated_savings ELSE 0 END) AS savings,
        SUM({COST}) AS cost, SUM(b.kwh) AS kwh_all, SUM(b.kvah) AS kvah_all
        {FROM} WHERE {w} AND b.plant_code IS NOT NULL GROUP BY b.plant_code""", p).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["pair_bills"] and d["kvah"]:
            d["pf"], d["pf_method"] = round(d["kwh"] / d["kvah"], 4), "kWh/kVAh (energy-weighted)"
        elif d["pf_bills"]:
            d["pf"], d["pf_method"] = round(d["mean_pf"], 4), "mean of billed PFs"
        else:
            d["pf"], d["pf_method"] = None, None
        d["violation_frequency"] = round(d["violations"] / d["demand_comparable"], 3) if d["demand_comparable"] else None
        out.append(d)
    return out


# --------------------------------------------------------------------------- 3.6 management analytics
def monthly(conn, f: Filters) -> list:
    """Per billing month: energy-weighted PF, bills, cost, cost per kWh / kVAh, actual penalties, estimated savings."""
    w, p = f.where()
    rows = conn.execute(f"""SELECT b.billing_month AS month, COUNT(*) AS bills, SUM(b.quality = 'UNVERIFIED') AS unverified,
        SUM(CASE WHEN {VALID_PAIR} THEN b.kwh END) AS kwh, SUM(CASE WHEN {VALID_PAIR} THEN b.kvah END) AS kvah,
        AVG(b.pf_billed) AS mean_pf, SUM({COST}) AS cost, SUM(CASE WHEN {COST} IS NOT NULL AND b.kwh > 0 THEN {COST} END) AS cost_k, SUM(CASE WHEN {COST} IS NOT NULL AND b.kwh > 0 THEN b.kwh END) AS kwh_c,
        SUM(CASE WHEN {COST} IS NOT NULL AND b.kvah > 0 THEN {COST} END) AS cost_v, SUM(CASE WHEN {COST} IS NOT NULL AND b.kvah > 0 THEN b.kvah END) AS kvah_c,
        SUM(CASE WHEN b.pf_penalty > 0 THEN b.pf_penalty ELSE 0 END) AS pf_penalty,
        SUM(CASE WHEN b.excess_demand_penalty > 0 THEN b.excess_demand_penalty ELSE 0 END) AS xd_penalty,
        SUM(CASE WHEN pf.savings_status LIKE 'ESTIMATE%' THEN pf.estimated_savings ELSE 0 END) AS savings,
        SUM(CASE WHEN {COST} IS NULL THEN 1 ELSE 0 END) AS no_cost
        {FROM} WHERE {w} AND b.billing_month IS NOT NULL GROUP BY b.billing_month ORDER BY b.billing_month""", p).fetchall()
    out, prev = [], None
    for r in rows:
        d = dict(r)
        d["pf"] = _ratio(d["kwh"], d["kvah"]) if d["kvah"] else (round(d["mean_pf"], 4) if d["mean_pf"] is not None else None)
        d["pf_method"] = "kWh/kVAh" if d["kvah"] else "mean of billed PFs"
        d["cost_per_kwh"] = round(d["cost_k"] / d["kwh_c"], 3) if d["kwh_c"] else None
        d["cost_per_kvah"] = round(d["cost_v"] / d["kvah_c"], 3) if d["kvah_c"] else None
        d["cost_change_pct"] = round((d["cost"] - prev) / prev * 100, 1) if prev and d["cost"] is not None else None
        prev = d["cost"] if d["cost"] else prev
        out.append(d)
    return out


def group_performance(conn, f: Filters, by: str) -> list:
    """State-wise or electricity-board-wise performance. by: 'state' | 'discom'."""
    col = {"state": "b.state", "discom": "b.discom_code"}[by]
    w, p = f.where()
    rows = conn.execute(f"""SELECT COALESCE({col}, 'Not identified') AS grp, COUNT(*) AS bills, COUNT(DISTINCT b.plant_code) AS plants,
        SUM(CASE WHEN {VALID_PAIR} THEN b.kwh END) AS kwh, SUM(CASE WHEN {VALID_PAIR} THEN b.kvah END) AS kvah, AVG(b.pf_billed) AS mean_pf,
        SUM(CASE WHEN b.pf_penalty > 0 THEN b.pf_penalty ELSE 0 END) AS pf_penalty,
        SUM(CASE WHEN b.excess_demand_penalty > 0 THEN b.excess_demand_penalty ELSE 0 END) AS xd_penalty,
        SUM(CASE WHEN d.violation_status IN ('VIOLATION','VIOLATION_BILLED') THEN 1 ELSE 0 END) AS violations,
        SUM(CASE WHEN pf.savings_status LIKE 'ESTIMATE%' THEN pf.estimated_savings ELSE 0 END) AS savings, SUM({COST}) AS cost,
        SUM(b.quality = 'UNVERIFIED') AS unverified
        {FROM} WHERE {w} GROUP BY grp ORDER BY bills DESC""", p).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pf"] = _ratio(d["kwh"], d["kvah"]) if d["kvah"] else (round(d["mean_pf"], 4) if d["mean_pf"] is not None else None)
        out.append(d)
    return out


def management(conn, f: Filters) -> dict:
    cfg = settings.get_all(conn)
    plants = plant_pf(conn, f)
    with_pf = sorted((x for x in plants if x["pf"] is not None), key=lambda x: x["pf"])
    return {
        "kpis": kpis(conn, f),
        "plants": plants,
        "plants_below_target": [x for x in with_pf if x["pf"] < cfg["target_pf"]],
        "worst": with_pf[:10],
        "best": list(reversed(with_pf[-10:])),
        "monthly": monthly(conn, f),
        "states": group_performance(conn, f, "state"),
        "boards": group_performance(conn, f, "discom"),
        "targets": {"target_pf": cfg["target_pf"], "pf_good": cfg["pf_good"], "pf_attention": cfg["pf_attention"]},
    }


# --------------------------------------------------------------------------- the plant-wise table (5.3)
def bill_table(conn, f: Filters, sort="period", direction="desc", page=1, per_page=50) -> dict:
    w, p = f.where()
    total = conn.execute(f"SELECT COUNT(*) {FROM} WHERE {w}", p).fetchone()[0]
    order = SORTS.get(sort, SORTS["period"])
    direction = "ASC" if str(direction).lower() == "asc" else "DESC"
    per_page = max(5, min(int(per_page), 500))
    page = max(1, int(page))
    rows = conn.execute(f"""SELECT b.id, b.plant_code, pl.plant_name, b.state, b.discom_code, b.period_start, b.period_end, b.billing_month, b.billing_basis,
        b.kwh, b.kvah, pf.pf_used, pf.pf_source, pf.performance_status, pf.validation_status, b.contract_demand, b.contract_demand_unit, b.max_demand,
        b.max_demand_unit, b.pf_penalty, b.pf_penalty_status, b.excess_demand_penalty, b.excess_demand_status, b.total_payable, b.total_bill_amount,
        b.bill_status, b.quality, b.source, b.bill_number, b.po_number, b.consumer_number, b.revision_count, b.data_flags, d.violation_status, d.utilization_pct,
        pf.estimated_savings, pf.savings_status
        {FROM} WHERE {w} ORDER BY {order} {direction} NULLS LAST, b.id DESC LIMIT :lim OFFSET :off""",
                        {**p, "lim": per_page, "off": (page - 1) * per_page}).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page, "pages": max(1, -(-total // per_page))}


def filter_options(conn, allowed_plants=None) -> dict:
    scope, p = "", {}
    if allowed_plants is not None:
        if not allowed_plants:
            scope = "AND 1 = 0"
        else:
            keys = [f":a{i}" for i in range(len(allowed_plants))]
            p = {f"a{i}": v for i, v in enumerate(allowed_plants)}
            scope = f"AND b.plant_code IN ({','.join(keys)})"
    out = {
        "plants": [dict(r) for r in conn.execute(
            f"SELECT b.plant_code, MIN(pl.plant_name) AS plant_name FROM electricity_bills b LEFT JOIN electricity_plants pl ON pl.plant_code = b.plant_code "
            f"WHERE b.plant_code IS NOT NULL {scope} GROUP BY b.plant_code ORDER BY b.plant_code", p)],
        "states": [r[0] for r in conn.execute(f"SELECT DISTINCT b.state FROM electricity_bills b WHERE b.state IS NOT NULL {scope} ORDER BY 1", p)],
        "discoms": [r[0] for r in conn.execute(f"SELECT DISTINCT b.discom_code FROM electricity_bills b WHERE b.discom_code IS NOT NULL {scope} ORDER BY 1", p)],
        "months": [r[0] for r in conn.execute(f"SELECT DISTINCT b.billing_month FROM electricity_bills b WHERE b.billing_month IS NOT NULL {scope} ORDER BY 1 DESC", p)],
    }
    # (value, label) pairs for the multi-select widgets
    out["state_opts"] = [(s, s) for s in out["states"]]
    out["discom_opts"] = [(s, s) for s in out["discoms"]]
    out["plant_opts"] = [(x["plant_code"], f"{x['plant_code']} — {x['plant_name'] or ''}") for x in out["plants"]]
    out["years"] = sorted({m[:4] for m in out["months"]}, reverse=True)
    return out


# --------------------------------------------------------------------------- one plant (5.4)
def plant_detail(conn, plant_code: str, f: Filters) -> dict:
    f2 = Filters(**{**f.__dict__, "plants": [plant_code]})
    info = conn.execute("SELECT * FROM electricity_plants WHERE plant_code = ?", (plant_code,)).fetchone()
    po = conn.execute("SELECT plant_name, MIN(supplier_name) AS supplier FROM electricity_pos WHERE plant_code = ?", (plant_code,)).fetchone()
    accounts = [dict(r) for r in conn.execute(
        """SELECT a.*, (SELECT COUNT(*) FROM electricity_bills b WHERE b.account_id = a.id AND b.is_primary = 1) AS bills,
           (SELECT MAX(period_end) FROM electricity_bills b WHERE b.account_id = a.id AND b.is_primary = 1) AS last_period_end
           FROM electricity_consumer_accounts a WHERE a.plant_code = ? ORDER BY a.consumer_number""", (plant_code,))]
    w, p = f2.where(with_period=False)
    months = conn.execute(f"""SELECT b.billing_month AS month, COUNT(*) AS bills,
        SUM(CASE WHEN {VALID_PAIR} THEN b.kwh END) AS kwh, SUM(CASE WHEN {VALID_PAIR} THEN b.kvah END) AS kvah, AVG(b.pf_billed) AS mean_pf,
        MAX(CASE WHEN b.data_flags NOT LIKE '%,DEMAND_IMPLAUSIBLE,%' THEN b.contract_demand END) AS cd,
        MAX(CASE WHEN b.data_flags NOT LIKE '%,DEMAND_IMPLAUSIBLE,%' THEN b.max_demand END) AS md, MAX(d.utilization_pct) AS util,
        SUM(CASE WHEN b.pf_penalty > 0 THEN b.pf_penalty ELSE 0 END) AS pf_penalty,
        SUM(CASE WHEN b.excess_demand_penalty > 0 THEN b.excess_demand_penalty ELSE 0 END) AS xd_penalty,
        SUM(CASE WHEN pf.savings_status LIKE 'ESTIMATE%' THEN pf.estimated_savings ELSE 0 END) AS savings, SUM({COST}) AS cost
        {FROM} WHERE {w} AND b.billing_month IS NOT NULL GROUP BY b.billing_month ORDER BY b.billing_month""", p).fetchall()
    trend = []
    for r in months:
        d = dict(r)
        d["pf"] = _ratio(d["kwh"], d["kvah"]) if d["kvah"] else (round(d["mean_pf"], 4) if d["mean_pf"] is not None else None)
        trend.append(d)
    bills = bill_table(conn, Filters(**{**f2.__dict__, "month_from": None, "month_to": None}), sort="period", direction="desc", per_page=120)
    cur = trend[-1] if trend else None
    prev = trend[-2] if len(trend) > 1 else None
    per_account = []
    if len(accounts) > 1:
        for a in accounts:
            r = conn.execute(f"""SELECT COUNT(*) AS bills, SUM(CASE WHEN {VALID_PAIR} THEN b.kwh END) AS kwh, SUM(CASE WHEN {VALID_PAIR} THEN b.kvah END) AS kvah,
                AVG(b.pf_billed) AS mean_pf, SUM({COST}) AS cost, SUM(CASE WHEN b.pf_penalty > 0 THEN b.pf_penalty ELSE 0 END) AS pf_penalty
                FROM electricity_bills b LEFT JOIN electricity_pf_analysis pf ON pf.bill_id = b.id
                WHERE b.account_id = ? AND b.is_primary = 1""", (a["id"],)).fetchone()
            d = dict(r)
            d["account"] = a["consumer_number"]
            d["pf"] = _ratio(d["kwh"], d["kvah"]) if d["kvah"] else (round(d["mean_pf"], 4) if d["mean_pf"] is not None else None)
            per_account.append(d)
    return {"info": dict(info) if info else None, "plant_name": (po["plant_name"] if po else None), "supplier": po["supplier"] if po else None,
            "accounts": accounts, "trend": trend, "current": cur, "previous": prev, "bills": bills["rows"], "per_account": per_account,
            "kpis": kpis(conn, f2)}


# --------------------------------------------------------------------------- one bill (viewer)
def bill_detail(conn, bill_id: int):
    b = conn.execute("SELECT * FROM electricity_bills WHERE id = ?", (bill_id,)).fetchone()
    if b is None:
        return None
    ext = conn.execute("SELECT * FROM electricity_bill_extractions WHERE id = ?", (b["extraction_id"],)).fetchone()
    return {"bill": dict(b), "extraction": dict(ext),
            "pf": dict(conn.execute("SELECT * FROM electricity_pf_analysis WHERE bill_id = ?", (bill_id,)).fetchone() or {}),
            "demand": dict(conn.execute("SELECT * FROM electricity_demand_analysis WHERE bill_id = ?", (bill_id,)).fetchone() or {})}

