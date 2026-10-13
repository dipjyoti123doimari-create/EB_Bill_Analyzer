"""
identification.py
=================
Decides which Oracle POs count as electricity POs, using administrator-managed
rules stored in electricity_id_rules (never hardcoded):

  ITEM_CODE            item code on a PO line, e.g. EXP00084 (at least one required)
  SUPPLIER_INCLUDE     if any exist, the supplier name must contain one of them
  SUPPLIER_EXCLUDE     supplier name contains this text  -> not eligible
  DESCRIPTION_EXCLUDE  PO comments / line description contain this text -> not eligible
  PLANT_EXCLUDE        plant (organization) code equals this -> not eligible

Matching is case-insensitive substring matching, except PLANT_EXCLUDE (exact code).
Excluded POs are still stored (eligible = 0 plus the reason), so changing a rule
can be re-applied without asking Oracle again and nothing is silently lost.
"""

from dataclasses import dataclass, field

from . import storage

RULE_TYPES = ("ITEM_CODE", "SUPPLIER_INCLUDE", "SUPPLIER_EXCLUDE", "DESCRIPTION_EXCLUDE", "PLANT_EXCLUDE")


@dataclass
class RuleSet:
    item_codes: list[str] = field(default_factory=list)
    supplier_include: list[str] = field(default_factory=list)
    supplier_exclude: list[str] = field(default_factory=list)
    description_exclude: list[str] = field(default_factory=list)
    plant_exclude: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, conn) -> "RuleSet":
        rs = cls()
        mapping = {"ITEM_CODE": rs.item_codes, "SUPPLIER_INCLUDE": rs.supplier_include,
                   "SUPPLIER_EXCLUDE": rs.supplier_exclude, "DESCRIPTION_EXCLUDE": rs.description_exclude,
                   "PLANT_EXCLUDE": rs.plant_exclude}
        for row in conn.execute("SELECT rule_type, value FROM electricity_id_rules WHERE active = 1"):
            mapping[row["rule_type"]].append(row["value"].strip())
        return rs

    def evaluate(self, po: dict) -> tuple[bool, str | None]:
        """Return (eligible, exclusion_reason) for a PO record."""
        supplier = (po.get("supplier_name") or "").lower()
        text = " ".join(filter(None, [po.get("comments"), po.get("line_description")])).lower()
        plant = (po.get("plant_code") or "").strip().upper()

        if self.supplier_include and not any(s.lower() in supplier for s in self.supplier_include):
            return False, "Supplier not in the include list"
        for s in self.supplier_exclude:
            if s.lower() in supplier:
                return False, f"Supplier excluded: {s}"
        for d in self.description_exclude:
            if d.lower() in text:
                return False, f"Description excluded: {d}"
        if plant and plant in {p.upper() for p in self.plant_exclude}:
            return False, f"Plant excluded: {plant}"
        return True, None


def add_rule(conn, rule_type: str, value: str, note: str | None = None) -> int:
    value = (value or "").strip()
    if rule_type not in RULE_TYPES:
        raise ValueError("unknown rule type")
    if not value or len(value) > 120:
        raise ValueError("the rule value is empty or too long")
    cur = conn.execute("INSERT INTO electricity_id_rules (rule_type, value, active, note, created_at) VALUES (?,?,1,?,?) "
                       "ON CONFLICT(rule_type, value) DO UPDATE SET active = 1, note = COALESCE(excluded.note, note)",
                       (rule_type, value, note, storage.now_iso()))
    return cur.lastrowid


def set_active(conn, rule_id: int, active: bool) -> None:
    row = conn.execute("SELECT rule_type, value FROM electricity_id_rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise ValueError("unknown rule")
    if row["rule_type"] == "ITEM_CODE" and not active:
        others = conn.execute("SELECT COUNT(*) FROM electricity_id_rules WHERE rule_type = 'ITEM_CODE' AND active = 1 AND id <> ?", (rule_id,)).fetchone()[0]
        if not others:
            raise ValueError("at least one active ITEM_CODE rule is needed, otherwise no PO can be recognised as an electricity PO")
    conn.execute("UPDATE electricity_id_rules SET active = ? WHERE id = ?", (1 if active else 0, rule_id))


def reapply(conn) -> dict:
    """Re-evaluate every stored PO against the current rules (no Oracle call). Returns counts of POs whose eligibility changed."""
    rs = RuleSet.load(conn)
    changed_in = changed_out = 0
    for po in conn.execute("SELECT * FROM electricity_pos").fetchall():
        eligible, reason = rs.evaluate(dict(po))
        if eligible and (po["cancel_flag"] or "").upper() == "Y":
            eligible, reason = False, "PO is cancelled"
        if int(eligible) != po["eligible"] or reason != po["exclusion_reason"]:
            conn.execute("UPDATE electricity_pos SET eligible = ?, exclusion_reason = ? WHERE po_header_id = ?", (int(eligible), reason, po["po_header_id"]))
            if eligible and not po["eligible"]:
                changed_in += 1
            elif not eligible and po["eligible"]:
                changed_out += 1
    return {"now_eligible": changed_in, "now_excluded": changed_out}
