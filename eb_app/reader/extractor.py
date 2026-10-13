"""
extractor.py
============
Format-independent field extraction.

For every field the extractor produces a list of *candidates*, each with the evidence that produced it
(page, bounding box, label text, relation used). Relations a human reader uses:

  inline          "Bill No : 0000123"                 value in the same cell as the label
  right           "Billed PF   0.7690"               value to the right on the same row
  below           header strip with values underneath
  table           where a column header ("KWH") meets a row label ("Total Consumption")
  section_total   "1. Energy Consumption (KWh)" ... then a "Total" row inside that section

Candidates are then ranked and the final answer is chosen *jointly* where electrical identities exist
(kWh / kVAh must reproduce the billed power factor). Nothing is invented: when no candidate exists the field is
reported as NOT_FOUND, never as zero.
"""

import re
from dataclasses import dataclass, field

from . import fields as F
from .layout import PageIndex, x_overlap
from .model import Cell, Document
from .textnorm import detect_unit, first_number, is_numeric_cell, norm, parse_dates, parse_numbers

REL_WEIGHT = {"inline": 1.0, "right": 0.93, "table": 0.95, "below": 0.86, "section_total": 0.9}
_NUM_UNIT = re.compile(r"^\s*[:=]?\s*([-+(]?[\d,]*\.?\d+\)?)\s*[\(\[]?\s*([A-Za-z/%.]{0,10})\s*[\)\]]?\s*(?:\s*(?:lead|lag|leading|lagging)\b.*|[\[\(].*)?$", re.I)
_SLASH_PAIR = re.compile(r"^\s*[:=]?\s*(\d[\d,]*\.?\d*)\s*/\s*(\d[\d,]*\.?\d*)\s*$")
_GLUED_LOAD = re.compile(r"^(sanc(?:tioned)?\.?\s*load|contract\s*demand|cd)\s*(\d{1,6}(?:\.\d+)?)\s*(kw|kva|hp|k)?\b", re.I)
_KWH_KVAH_RATIO = re.compile(r"kwh\s*/\s*kvah\s*[=:]\s*([\d,]+(?:\.\d+)?)\s*/\s*([\d,]+(?:\.\d+)?)", re.I)
_BILL_DATE_IN_TEXT = re.compile(r"\b(?:bill|invoice).{0,90}?\bdate[d]?\s*[:\-]\s*(\d{1,2}[-/. ][A-Za-z0-9]{2,9}[-/. ]\d{2,4})", re.I)
_GLUED = re.compile(r"^(pf|p\.f\.?|md)\s*(\d{1,4}\.\d{1,4})", re.I)
_PF_IN_TEXT = re.compile(r"(?:recorded|billed|average|avg\.?|actual|monthly)\s*(?:pf|p\.f\.?|power\s*factor)\s*[:=\-]?\s*(\d?\.\d{1,4}|\d{2}(?:\.\d+)?\s*%)", re.I)
_SCALE = {"VA": (0.001, "kVA"), "W": (0.001, "kW"), "WATT": (0.001, "kW"), "WATTS": (0.001, "kW")}
_ID_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/._]{2,30}(?:\*\d)?")


@dataclass
class Candidate:
    field: str
    value: object
    raw: str
    page: int
    bbox: tuple
    label: str
    relation: str
    score: float
    unit: str | None = None
    ocr_conf: float = 1.0
    src: str = "native"
    note: str = ""
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- value parsing
def _number_from_cell(cell: Cell):
    """(value, unit) when the cell is a number, optionally with a short unit ('107.00 HP', '75.0 kVA')."""
    t = (cell.text or "").strip()
    if is_numeric_cell(t):
        return first_number(t), None
    m = _NUM_UNIT.match(t)
    if m:
        n = first_number(m.group(1))
        unit_text = m.group(2)
        if n is not None and (not unit_text or detect_unit(unit_text) or unit_text.lower() in ("hp", "kw", "kva", "mva", "%", "va", "w", "watt", "watts", "lead", "lag")):
            up = unit_text.upper()
            if up in _SCALE:                       # '75000VA' -> 75 kVA, '133633 Watts' -> 133.633 kW
                factor, name = _SCALE[up]
                return n * factor, name
            return n, detect_unit(unit_text) or (up if unit_text else None)
    return None, None


def _value_from_cell(cell: Cell, kind: str, pair_index: int = 0):
    """Parse a cell as the requested kind. Returns (value, unit, raw) or None.
    pair_index selects which half of an 'a/b' cell to return (0 = first)."""
    t = (cell.text or "").strip()
    if not t:
        return None
    if kind == "number":
        pm = _SLASH_PAIR.match(t)
        if pm:
            return (first_number(pm.group(1 + pair_index)), None, t)
        n, unit = _number_from_cell(cell)
        return None if n is None else (n, unit, t)
    if kind == "date":
        d = parse_dates(t)
        return (d[0], None, t) if d else None
    if kind == "daterange":
        d = parse_dates(t)
        return ((d[0], d[1]), None, t) if len(d) >= 2 else None
    if kind == "id":
        if parse_dates(t) and len(t) <= 12:
            return None
        m = _ID_TOKEN.search(t)
        if m and sum(ch.isdigit() for ch in m.group(0)) >= 3:
            token = re.split(r"(?<=\d)(?=[A-Z][a-z]{2,})", m.group(0).strip(".-_/"))[0]      # 'CRD5314Old' -> 'CRD5314'
            return (token, None, t)
        return None
    if kind == "text":
        if is_numeric_cell(t) or len(t) < 3:
            return None
        return (t, None, t)
    return None


def _inline_value(cell: Cell, kind: str, spec_labels):
    """Value that follows the label inside the same cell ('Bill No : 000123', 'Billed PF 0.769')."""
    t = cell.text
    parts = re.split(r"[:=]", t, maxsplit=1)
    tail = parts[1] if len(parts) == 2 else None
    if tail is None:
        dm = re.match(r"^\s*([A-Za-z][A-Za-z ./#]{2,30}?)\s*[-\u2013]\s*(\d[\w/.\-]{4,})\s*$", t)        # 'Account-1350852000'
        if dm and F.label_score(dm.group(1), spec_labels) >= 0.8:
            tail = dm.group(2)
    if tail is None:
        # label then number with no separator, e.g. 'PF 0.96' / 'Contract Demand(KVA) 100'
        m = re.match(r"^(.*?[A-Za-z\)\.])\s+([-+]?[\d][\d,./\- ]*\S*)\s*$", t)
        tail = m.group(2) if m and F.label_score(m.group(1), spec_labels) >= 0.8 else None
    if tail is None:
        gm = re.match(r"^(.*?[A-Za-z])\s*(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})\s*$", t)               # 'BILL DATE15/09/26'
        if gm and F.label_score(gm.group(1), spec_labels) >= 0.8:
            tail = gm.group(2)
    if not tail or not tail.strip():
        return None
    tmp = Cell(tail.strip(), cell.x0, cell.y0, cell.x1, cell.y1, cell.conf, cell.src, cell.page)
    return _value_from_cell(tmp, kind)


def _plausible(spec: F.FieldSpec, value) -> float:
    if spec.kind != "number" or value is None:
        return 1.0
    lo, hi = spec.lo, spec.hi
    if lo is not None and value < lo:
        return 0.35
    if hi is not None and value > hi:
        return 0.35
    return 1.0


_TOTAL_HEADERS = ("totalconsumption", "consumption", "totalunits", "total", "netconsumption", "netunits")


def _under_total_header(pi, row_index: int, nums: list):
    """Row with 4+ numbers (reading, reading, difference, MF, consumption, total...): the number standing under the table's
    Total/Consumption header, looked for in the 4 rows above. None if the table has no such header."""
    if len(nums) < 4:
        return None
    heads = [h for r in pi.rows[max(0, row_index - 10):row_index] for h in r if norm(h.text) in _TOTAL_HEADERS]
    for h in sorted(heads, key=lambda h: -h.x0):                    # the right-most such header first (Total after Consumption)
        under = [c for c in nums if x_overlap(h, c) >= 0.3 or abs(c.xc - h.xc) <= max(h.w, c.w)]
        if under:
            return min(under, key=lambda c: abs(c.xc - h.xc))
    return None


def _blocks_column(H: Cell, row: list) -> bool:
    """Does `row` start a new table *in the column range of header H*? (header-like text there, no numbers there)."""
    if any(c is H for c in row):
        return False
    here = [c for c in row if x_overlap(H, c) >= 0.2 or abs(c.xc - H.xc) <= max(H.w, c.w)]
    texts = [c for c in here if not is_numeric_cell(c.text) and len(c.text.strip()) <= 16]
    nums = [c for c in here if is_numeric_cell(c.text)]
    return bool(texts) and not nums and _is_header_row(row)


def _is_header_row(row: list) -> bool:
    """A row of several short text cells and (almost) no numbers: the header of a table."""
    texts = [c for c in row if not is_numeric_cell(c.text) and len(c.text.strip()) <= 16]
    nums = [c for c in row if is_numeric_cell(c.text)]
    return len(texts) >= 3 and len(nums) <= 1


def _label_ls(text: str, synonyms, exclude) -> float:
    """Score a cell as a label. If the cell also carries a value ('Bill Period: 01-09-2026 To 30-09-2026')
    the text before the colon is scored too, so the value does not count against the label."""
    s = F.label_score(text, synonyms, exclude)
    if ":" in text:
        head = text.split(":", 1)[0]
        if len(norm(head)) >= 3:
            s = max(s, F.label_score(head, synonyms, exclude))
    return s


# --------------------------------------------------------------------------- candidate generation
class Extractor:
    def __init__(self, doc: Document):
        self.doc = doc
        self.pages = [PageIndex(p) for p in doc.pages if p.cells]
        self.cands: dict = {k: [] for k in F.FIELDS}
        self.total_bill: list = []
        self.total_payable: list = []
        self.charge_lines: list = []
        self.unlabeled_rows: list = []

    # --- helpers -----------------------------------------------------------------------------
    AVG_SENSITIVE = {"kwh", "kvah", "max_demand", "contract_demand", "billing_demand"}

    def _under_average(self, label_cell) -> bool:
        """True when the label sits under an 'Average' / 'Avg' header cell (a six-month average is not this month's value)."""
        pi = getattr(self, "_pi", None)
        if pi is None:
            return False
        i = pi.row_index(label_cell)
        if i is None or i == 0:
            return False
        for c in pi.rows[i - 1]:
            n = norm(c.text)
            if n in ("average", "avg", "averageof", "avgof") and abs(c.xc - label_cell.xc) <= (c.w + label_cell.w):
                return True
        return False

    _MONTH_YEAR = re.compile(r"^\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s\-/.,']*\d{2,4}\s*$", re.I)

    def _in_history_row(self, cell) -> bool:
        """The value sits on a row that starts with a month label ('Mar-2026 | 9588 | 9646 | 75.76'): past-months table."""
        pi = getattr(self, "_pi", None)
        if pi is None or id(cell) not in pi.row_of:
            return False
        ri = pi.row_of[id(cell)]
        if not any(self._MONTH_YEAR.match(c.text) for c in pi.rows[ri] if c.x1 <= cell.x0 + 2):
            return False
        near = [r for r in pi.rows[max(0, ri - 8):ri + 9] if any(self._MONTH_YEAR.match(c.text) for c in r)]
        return len(near) >= 3                                         # one month row is the bill itself; several are a history

    def _mk(self, spec, value, unit, raw, cell, label_cell, rel, ls, rank_penalty=0.0, note="", extra=None):
        if spec.kind == "number" and spec.id in self.AVG_SENSITIVE | {"billed_units", "pf", "pf_billed"} and rel in ("below", "right", "table") \
                and self._in_history_row(cell):
            ls, note = ls * 0.35, (note + " in a past-months history row").strip()
        if spec.id in self.AVG_SENSITIVE and rel in ("below", "table") and self._under_average(label_cell):
            ls, note = ls * 0.4, (note + " under an 'Average' header").strip()
        if spec.id in ("pf", "pf_billed") and isinstance(value, (int, float)) and 1.0001 < value <= 100:
            if ("." in str(raw) or "%" in str(raw)) and value >= 30:
                value, note = round(value / 100.0, 6), (note + " printed as a percentage").strip()
        if unit is None and spec.unit_family == "demand":
            unit = detect_unit(label_cell.text) or detect_unit(raw)
        # an energy figure (kWh/kVAh) in a demand field is almost certainly the wrong cell
        if spec.id in ("pf", "pf_billed"):
            m_dir = re.search(r"\b(lead(?:ing)?|lag(?:ging)?)\b", f"{raw} {label_cell.text}", re.I)
            if m_dir:
                extra = dict(extra or {}, direction="leading" if m_dir.group(1).lower().startswith("lead") else "lagging")
        score_scale = 0.15 if (spec.unit_family == "demand" and (unit or "").upper() in ("KWH", "KVAH", "KVARH")) else 1.0
        score = ls * REL_WEIGHT[rel] * _plausible(spec, value) * (0.6 + 0.4 * min(cell.conf, label_cell.conf)) * score_scale - rank_penalty
        return Candidate(spec.id, value, raw, cell.page, (cell.x0, cell.y0, cell.x1, cell.y1), label_cell.text.strip()[:80],
                         rel, max(score, 0.0), unit, min(cell.conf, label_cell.conf), cell.src, note, extra or {})

    def run(self) -> "Extractor":
        for pi in self.pages:
            self._pi = pi
            self._label_relations(pi)
            self._table_intersections(pi)
            self._section_totals(pi)
            self._zone_max(pi)
            self._reading_date_pairs(pi)
            self._payable_by_due_date(pi)
            self._charge_and_total_lines(pi)
        return self

    # --- label based relations ----------------------------------------------------------------
    def _label_relations(self, pi: PageIndex) -> None:
        for spec in F.FIELDS.values():
            if not spec.labels:
                continue
            for cell in pi.cells + pi.vlabels:
                ls = _label_ls(cell.text, spec.labels, spec.exclude)
                if cell.anchor is not None:
                    ls *= 0.96
                if spec.weak:
                    ls = max(ls, 0.85 * _label_ls(cell.text, spec.weak, spec.exclude))
                if ls < 0.75:
                    continue
                if "inline" in spec.relations:
                    v = _inline_value(cell, spec.kind, spec.labels + spec.weak)
                    if v:
                        self.cands[spec.id].append(self._mk(spec, v[0], v[1], v[2], cell, cell, "inline", ls))
                if "right" in spec.relations:
                    skipped = 0
                    pair_idx = 1 if (spec.id == "contract_demand" and "cd" in norm(cell.text) and "load" in norm(cell.text)) else 0
                    right_cells = pi.right_of(cell, limit=9, stop_at_text=False)
                    if spec.id in self.AVG_SENSITIVE and spec.kind == "number":
                        pick = _under_total_header(pi, pi.row_index(cell), [x for x in right_cells if is_numeric_cell(x.text)])
                        if pick is not None:
                            n_pick, u_pick = _number_from_cell(pick)
                            self.cands[spec.id].append(self._mk(spec, n_pick, u_pick, pick.text, pick, cell, "table", ls,
                                                                note="number under the table's Total/Consumption column"))
                    for k, c in enumerate(right_cells[:5]):
                        v = _value_from_cell(c, spec.kind, pair_idx)
                        if v and spec.kind == "number" and re.fullmatch(r"\d{1,2}\.?", c.text.strip()) and k + 1 < len(right_cells) \
                                and not is_numeric_cell(right_cells[k + 1].text) and len(norm(right_cells[k + 1].text)) > 3 \
                                and (right_cells[k + 1].x0 - c.x1) < 0.6 * max(c.x0 - cell.x1, 1.0):
                            continue                                    # a serial hugging the NEXT label ('22 | Total Amount Due'), not our value
                        if v:
                            unit = v[1]
                            nxt = pi.right_of(c, limit=1, stop_at_text=False)
                            if unit is None and nxt and not is_numeric_cell(nxt[0].text) and len(nxt[0].text.strip()) <= 6:
                                unit = detect_unit(nxt[0].text)                       # only a real unit; never stray words like '@ RS.'
                            run = [x for x in pi.right_of(cell, limit=9, stop_at_text=False) if is_numeric_cell(x.text)]
                            series = spec.id in self.AVG_SENSITIVE | {"billed_units"} and len(run) >= 4 and spec.kind == "number"
                            self.cands[spec.id].append(self._mk(spec, v[0], unit, v[2], c, cell, "right", ls * (0.5 if series else 1.0),
                                                                0.04 * k + 0.03 * skipped, note="looks like a history series" if series else ""))
                            if spec.kind != "number" or spec.lo is None or (spec.lo <= v[0] <= spec.hi) or v[0] > 100:
                                break
                            continue                                  # implausible (e.g. a footnote '8' before 0.78): look further right
                        # one short descriptive cell between label and value is tolerated ('Avg. PF | KWh/KVAh=5880/7710 | 0.76')
                        if spec.kind == "number" and not is_numeric_cell(c.text) and len(c.text.strip()) <= 40 and skipped < 1 \
                                and not F.any_known_label(c.text):
                            skipped += 1
                            continue
                        break
                if "below" in spec.relations:
                    for k, c in enumerate(pi.below(cell, max_rows=2)):
                        v = _value_from_cell(c, spec.kind)
                        if v:
                            unit = v[1] or (pi.unit_tag_near(c) if spec.unit_family == "demand" else None)
                            self.cands[spec.id].append(self._mk(spec, v[0], unit, v[2], c, cell, "below", ls, 0.05 * k))
                            if spec.id in ("billed_units", "max_demand"):      # every row of the column, not just the first
                                for c2 in pi.below(c, max_rows=3, need_overlap=0.4)[:3]:
                                    v2 = _value_from_cell(c2, spec.kind)
                                    tag2 = pi.unit_tag_near(c2) if spec.id == "max_demand" else None
                                    if v2 and v2[0] != v[0] and (spec.id == "billed_units" or tag2):
                                        self.cands[spec.id].append(self._mk(spec, v2[0], tag2, v2[2], c2, cell, "below", ls, 0.08))
                            break
                if spec.kind in ("date", "daterange") and "right" in spec.relations:
                    self._date_pair_right(pi, spec, cell, ls)
            # a column header (KWH, KVAH, PF, MD...) with a number directly beneath it and no row label in between
            if spec.cols and spec.kind == "number":
                for cell in pi.cells:
                    cs = F.label_score(cell.text, spec.cols)
                    if cs < 0.85 or is_numeric_cell(cell.text) or F.label_score(cell.text, spec.labels, spec.exclude) >= 0.75:
                        continue
                    for k, c in enumerate(pi.below(cell, max_rows=2)):
                        v = _value_from_cell(c, spec.kind)
                        if v:
                            self.cands[spec.id].append(self._mk(spec, v[0], v[1], v[2], c, cell, "below", cs * 0.55, 0.05 * k,
                                                                note=f"value under column header '{cell.text.strip()[:30]}'"))
                            break
            # a row labelled only by its unit ('kWh', 'kVA'): the last number on the row is the reading/consumption
            for unit_syn in spec.unit_rows:
                for cell in pi.cells:
                    if norm(cell.text) != unit_syn:
                        continue
                    row = pi.rows[pi.row_of[id(cell)]]
                    nums = [c for c in row if c.x0 > cell.x1 and _value_from_cell(c, "number")]
                    if nums:
                        V = nums[-1]
                        ri0 = pi.row_of[id(cell)]
                        heads = [h for r in pi.rows[max(0, ri0 - 10):ri0] for h in r
                                 if norm(h.text) in _TOTAL_HEADERS]
                        for h in heads:                               # prefer the numeric cell standing under such a header
                            under = [c for c in nums if x_overlap(h, c) >= 0.3 or abs(c.xc - h.xc) <= max(h.w, c.w)]
                            if under:
                                V = min(under, key=lambda c: abs(c.xc - h.xc))
                                break
                        n, unit = _number_from_cell(V)
                        self.cands[spec.id].append(self._mk(spec, n, unit or detect_unit(cell.text), V.text, V, cell, "right", 0.82,
                                                            0.0, note=f"row labelled '{cell.text.strip()}', last number"))
        for cell in pi.cells:                               # 'H.T C.C. Bill cum Demand Notice for the Month of September 2026, Date: 01-Oct-26'
            m = _BILL_DATE_IN_TEXT.search(cell.text)
            if m and "due" not in cell.text.lower()[:m.start(1)]:
                d = parse_dates(m.group(1))
                if d:
                    self.cands["bill_date"].append(self._mk(F.FIELDS["bill_date"], d[0], None, m.group(1), cell, cell, "inline", 0.85,
                                                            note="bill date quoted in a title line"))
        for cell in pi.cells:                               # 'SANC LOAD73.0 K'
            m = _GLUED_LOAD.match(cell.text.strip())
            if m and not is_numeric_cell(cell.text):
                lab = norm(m.group(1))
                fid = "contract_demand" if lab in ("contractdemand", "cd") else "sanctioned_load"
                unit = {"kw": "kW", "kva": "kVA", "hp": "HP", "k": "kW"}.get((m.group(3) or "").lower())
                self.cands[fid].append(self._mk(F.FIELDS[fid], first_number(m.group(2)), unit, m.group(0), cell, cell, "inline", 0.8,
                                                note="label and value printed without a gap"))
        for cell in pi.cells:                               # PDF generators that glue label and value: 'PF0.83CONSUMPTION'
            m = _GLUED.match(cell.text.strip())
            if m:
                fid = "pf" if m.group(1).lower().startswith("p") else "max_demand"
                val = first_number(m.group(2))
                self.cands[fid].append(self._mk(F.FIELDS[fid], val, None, m.group(0), cell, cell, "inline", 0.8,
                                                note="label and value printed without a gap"))
        for cell in pi.cells:                               # 'PF Penalty (Recorded PF : 0.88)'
            m = _PF_IN_TEXT.search(cell.text)
            if m:
                val = first_number(m.group(1))
                if val is not None:
                    fid = "pf_billed" if re.search(r"billed|recorded", m.group(0), re.I) else "pf"
                    self.cands[fid].append(self._mk(F.FIELDS[fid], val, None, m.group(0), cell, cell, "inline", 0.9,
                                                    note="PF quoted inside a charge description"))
        # evidence printed as text: 'KWh/KVAh=5880/7710'
        for cell in pi.cells:
            m = _KWH_KVAH_RATIO.search(cell.text)
            if m:
                for fid, g in (("kwh", 1), ("kvah", 2)):
                    val = first_number(m.group(g))
                    if val is not None:
                        self.cands[fid].append(self._mk(F.FIELDS[fid], val, None, m.group(g), cell, cell, "inline", 0.97,
                                                        note="printed as 'KWh/KVAh=a/b'"))

    def _date_pair_right(self, pi, spec, cell, ls) -> None:
        """'Bill Period : 01/09/2026   30/09/2026' with the two dates in separate cells."""
        if spec.kind != "daterange":
            return
        dates = []
        last = None
        for c in pi.right_of(cell, limit=6, stop_at_text=False):
            d = parse_dates(c.text)
            if d:
                last = c
            dates += d
        if len(dates) == 1 and last is not None:
            for c in pi.below(last, max_rows=1, need_overlap=0.1):
                dates += parse_dates(c.text)[:1]
        if len(dates) >= 2:
            self.cands[spec.id].append(self._mk(spec, (dates[0], dates[1]), None, " ".join(map(str, dates[:2])), cell, cell, "right", ls))

    # --- column header x row label --------------------------------------------------------------
    def _table_intersections(self, pi: PageIndex) -> None:
        for spec in F.FIELDS.values():
            if not (spec.cols and spec.rows):
                continue
            headers = [(c, F.label_score(c.text, spec.cols)) for c in pi.cells]
            headers = [(c, s) for c, s in headers if s >= 0.85 and not is_numeric_cell(c.text)]
            for H, hs in headers:
                hi = pi.row_of[id(H)]
                for ri in range(hi + 1, min(hi + 14, len(pi.rows))):
                    if ri > hi + 1 and _blocks_column(H, pi.rows[ri - 1]):
                        break                                       # another table starts here: do not read across it
                    row = pi.rows[ri]
                    for R in row:
                        if R.x0 >= H.x0 or is_numeric_cell(R.text):
                            continue
                        best_rank, best_rs = None, 0.0
                        for rank, syn in enumerate(spec.rows):
                            rs = F.label_score(R.text, (syn,), ())
                            if rs >= 0.9:
                                best_rank, best_rs = rank, rs
                                break
                        if best_rank is None:
                            continue
                        vals = [c for c in row if c is not R and c.x0 > R.x1 and (x_overlap(H, c) >= 0.2 or abs(c.xc - H.xc) <= 0.7 * max(H.w, c.w))]
                        vals = [c for c in vals if _value_from_cell(c, spec.kind if spec.kind in ("number", "date") else "number")]
                        if not vals:
                            continue
                        V = min(vals, key=lambda c: abs(c.xc - H.xc))
                        if spec.kind == "date":
                            d = parse_dates(V.text)
                            pen = 0.02 * best_rank + 0.0005 * (ri - hi)
                            self.cands[spec.id].append(self._mk(spec, d[0], None, V.text, V, H, "table", min(hs, best_rs), pen,
                                                                note=f"column '{H.text.strip()}' x row '{R.text.strip()}'"))
                            continue
                        n, unit = _number_from_cell(V)
                        if unit is None:
                            unit = detect_unit(H.text)
                        pen = 0.02 * best_rank + 0.0005 * (ri - hi)
                        self.cands[spec.id].append(self._mk(spec, n, unit, V.text, V, H, "table", min(hs, best_rs), pen,
                                                            note=f"column '{H.text.strip()}' x row '{R.text.strip()}'"))

    # --- "1. Energy Consumption (KWh)" ... "Total" -----------------------------------------------
    def _section_totals(self, pi: PageIndex) -> None:
        for fid in ("kwh", "kvah", "kvarh_lag", "kvarh_lead"):
            spec = F.FIELDS[fid]
            for cell in pi.cells:
                if F.label_score(cell.text, spec.labels, spec.exclude) < 0.85 or is_numeric_cell(cell.text):
                    continue
                ri = pi.row_of[id(cell)]
                right = [c for c in pi.rows[ri] if c.x0 > cell.x1 + 6 and not is_numeric_cell(c.text)]
                bound = min([c.x0 for c in right], default=pi.page.width + 1)
                for rj in range(ri + 1, min(ri + 14, len(pi.rows))):
                    row = pi.rows[rj]
                    tot = [c for c in row if norm(c.text) in ("total", "totalunits", "totalconsumption", "net") and cell.x0 - 4 <= c.x0 < bound]
                    if not tot:
                        continue
                    nums = [c for c in row if c.x0 > tot[0].x1 and c.x0 < bound and _value_from_cell(c, "number")]
                    if nums:
                        V = nums[-1]
                        self.cands[fid].append(self._mk(spec, first_number(V.text), None, V.text, V, cell, "section_total", 0.95,
                                                        note=f"'Total' under '{cell.text.strip()[:40]}'"))
                    break

    def _reading_date_pairs(self, pi: PageIndex) -> None:
        """A row holding two dates 20-45 days apart, under a header that mentions 'reading': the meter-reading period."""
        spec = F.FIELDS["billing_period"]
        for ri, row in enumerate(pi.rows):
            dated = [(c, parse_dates(c.text)) for c in row if len(c.text.strip()) <= 14]
            dated = [(c, d[0]) for c, d in dated if d]
            if len(dated) != 2:
                continue
            (c1, d1), (c2, d2) = sorted(dated, key=lambda t: t[0].x0)
            gap = (d2 - d1).days
            if not 20 <= gap <= 45:
                continue
            above = " ".join(norm(x.text) for r in pi.rows[max(0, ri - 4):ri] for x in r)
            if "reading" in above or "meter" in above:
                self.cands["billing_period"].append(self._mk(spec, (d1, d2), None, f"{c1.text} .. {c2.text}", c2, c1, "table", 0.75,
                                                             note="two meter-reading dates in one row"))

    def _zone_max(self, pi: PageIndex) -> None:
        """'4. Demand (KVA)' followed by zone rows ('1  47.842  1.000  47.84', '2 ...'): maximum demand is the highest zone."""
        spec = F.FIELDS["max_demand"]
        for cell in pi.cells:
            n = norm(cell.text)
            if is_numeric_cell(cell.text) or len(n) > 16 or not re.fullmatch(r"\d?demandk(va|w)", n):
                continue
            ri = pi.row_of[id(cell)]
            numbered = bool(re.match(r"^\s*\d\s*[.)]", cell.text))
            has_readings = any("reading" in norm(c.text) for c in pi.rows[ri] if c.x0 > cell.x1)
            if not (numbered or has_readings):
                continue                       # a plain 'Demand(kVA)' column header is not a table of per-zone demand readings
            right = [c for c in pi.rows[ri] if c.x0 > cell.x1 + 6 and not is_numeric_cell(c.text)]
            bound = min([c.x0 for c in right], default=pi.page.width + 1) if not cell.text.strip().startswith(("1", "2", "3", "4")) else pi.page.width + 1
            vals, found_rows = [], 0
            for rj in range(ri, min(ri + 9, len(pi.rows))):
                row = [c for c in pi.rows[rj] if c.x0 >= cell.x0 - 4 and c.x0 < bound]
                zone = [c for c in row if re.fullmatch(r"[1-9]", c.text.strip())]
                nums = [c for c in row if c is not cell and c.x0 > (zone[0].x1 if zone else 0) and _value_from_cell(c, "number")]
                if zone and nums and cell.x0 - 4 <= zone[0].x0 <= cell.x0 + 400:
                    vals.append((first_number(nums[-1].text), nums[-1]))
                    found_rows += 1
                elif found_rows:
                    break
            vals = [(v, c) for v, c in vals if v is not None]
            if len(vals) >= 2:
                v, c = max(vals, key=lambda t: t[0])
                self.cands["max_demand"].append(self._mk(spec, v, "kVA" if "kva" in n else "kW", c.text, c, cell, "table", 0.97,
                                                         note=f"highest of {len(vals)} zone readings under '{cell.text.strip()}'"))

    def _payable_by_due_date(self, pi: PageIndex) -> None:
        """Bills that print several payable amounts side by side ('Before PPD | upto Due Date | After Due Date'):
        the amount payable by the due date is the one under the 'upto/by/before due date' heading."""
        for cell in pi.cells:
            n = norm(cell.text)
            if is_numeric_cell(cell.text) or not re.search(r"(upto|by|before|within|on)(the)?duedate", n) or "after" in n.replace("afterppd", ""):
                continue
            if "payable" not in n and not re.search(r"ppd|rebate|discount|duedate$", n):
                continue
            for c in pi.below(cell, max_rows=4, need_overlap=0.3):
                if is_numeric_cell(c.text) and first_number(c.text) is not None:       # a printed 0 is a real payable (credit balance), not 'no value'
                    self.total_payable.append(Candidate("total_payable", first_number(c.text), c.text, c.page,
                                                        (c.x0, c.y0, c.x1, c.y1), cell.text.strip()[:80], "below", 0.9, None, c.conf, c.src,
                                                        note="amount under the 'payable by due date' heading"))
                    break

    # --- charge lines and totals -------------------------------------------------------------------
    def _charge_and_total_lines(self, pi: PageIndex) -> None:
        for row in pi.rows:
            if row and is_numeric_cell(row[0].text):
                nums = [first_number(c.text) for c in row if is_numeric_cell(c.text)]
                if 3 <= len(nums) <= 5 and all(n is not None for n in nums):
                    self.unlabeled_rows.append({"numbers": nums, "page": row[0].page})
        for g in pi.groups:
            lab = g.label
            text = lab.text.strip()
            if not g.numbers or len(norm(text)) < (2 if len(g.numbers) >= 3 else 3):
                continue
            n = norm(text)
            amount_cell = g.last_number
            amount = first_number(amount_cell.text)
            if amount is None:
                continue
            category = None
            for cat, inc, exc in F.CHARGE_RULES:
                if any(i in n and (len(i) > 3 or i in {norm(w) for w in F.words_of(text)}) for i in inc) and not any(e in n for e in exc):
                    category = cat
                    break
            self.charge_lines.append({
                "label": text[:90], "category": category, "amount": amount,
                "numbers": [first_number(c.text) for c in g.numbers], "page": lab.page,
                "bbox": [round(v, 1) for v in (lab.x0, lab.y0, amount_cell.x1, amount_cell.y1)], "src": lab.src,
                "conf": round(min(lab.conf, amount_cell.conf), 3)})
            ts = F.label_score(text, F.TOTAL_PAYABLE_LABELS, F.TOTAL_PAYABLE_EXCLUDE)
            if ts >= 0.8:
                self.total_payable.append(Candidate("total_payable", amount, amount_cell.text, lab.page,
                                                    (amount_cell.x0, amount_cell.y0, amount_cell.x1, amount_cell.y1), text[:80], "right",
                                                    ts * 0.93, None, amount_cell.conf, lab.src))
            tb = F.label_score(text, F.TOTAL_BILL_LABELS, F.TOTAL_PAYABLE_EXCLUDE + F.TOTAL_PAYABLE_LABELS)
            if tb >= 0.8:
                self.total_bill.append(Candidate("total_bill_amount", amount, amount_cell.text, lab.page,
                                                 (amount_cell.x0, amount_cell.y0, amount_cell.x1, amount_cell.y1), text[:80], "right",
                                                 tb * 0.9, None, amount_cell.conf, lab.src))
