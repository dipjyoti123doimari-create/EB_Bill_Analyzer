"""
layout.py
=========
Geometry on top of Cells: rows, label/value groups and the spatial relations a human uses when reading a bill
("the number to the right of the label", "the number under the header", "where this row meets that column").

Everything is purely positional and works identically for native-PDF text and OCR output.
"""

import re
from dataclasses import dataclass, field

from .model import Cell, Page
from .textnorm import is_numeric_cell, norm


def row_tolerance(a: Cell, b: Cell) -> float:
    return 0.55 * max(min(a.h, b.h), 1.0)


def same_row(a: Cell, b: Cell) -> bool:
    return abs(a.yc - b.yc) <= row_tolerance(a, b)


def x_overlap(a: Cell, b: Cell) -> float:
    """Horizontal overlap as a share of the narrower cell (0..1)."""
    inter = min(a.x1, b.x1) - max(a.x0, b.x0)
    return max(inter, 0.0) / max(min(a.w, b.w), 1.0)


def build_rows(cells: list) -> list:
    """Cluster cells into visual rows (top to bottom), each row sorted left to right."""
    rows: list = []
    for c in sorted(cells, key=lambda c: (c.yc, c.x0)):
        placed = False
        for row in reversed(rows[-6:]):                 # a cell can only belong to a recent row
            ref = row[0]
            if abs(c.yc - sum(x.yc for x in row) / len(row)) <= row_tolerance(c, ref):
                row.append(c)
                placed = True
                break
        if not placed:
            rows.append([c])
    for row in rows:
        row.sort(key=lambda c: c.x0)
    rows.sort(key=lambda r: sum(c.yc for c in r) / len(r))
    return rows


@dataclass
class Group:
    """A label (text cell, possibly several joined) followed by the numeric cells that sit to its right
    before the next label starts. Two-column bills simply yield two groups on the same row."""
    label: Cell
    numbers: list = field(default_factory=list)      # numeric Cells in left-to-right order
    texts: list = field(default_factory=list)        # non-numeric cells that follow (units, dates, words)
    page: int = 1

    @property
    def last_number(self):
        return self.numbers[-1] if self.numbers else None


_LABEL_THEN_AMOUNT = re.compile(r"^(.*?[A-Za-z\)\]])\s*[:=\-]?\s*(?:Rs\.?|INR|\u20b9|`)?\s*([-+]?\(?\d[\d,]*\.\d{1,3}\)?(?:\s*(?:Cr|Dr))?)\s*$", re.I)


def _split_label_amount(c: Cell):
    """'Gross Total 237560.00' (one cell) -> (label cell, amount cell) with proportional boxes, else None."""
    m = _LABEL_THEN_AMOUNT.match(c.text.strip())
    if not m or len(norm(m.group(1))) < 3:
        return None
    t = c.text.strip()
    cut = len(m.group(1)) / max(len(t), 1)
    xm = c.x0 + (c.x1 - c.x0) * cut
    return (Cell(m.group(1).strip(), c.x0, c.y0, xm, c.y1, c.conf, c.src, c.page),
            Cell(m.group(2).strip(), xm, c.y0, c.x1, c.y1, c.conf, c.src, c.page))


def groups_in_row(row: list) -> list:
    groups: list = []
    for ci, c in enumerate(row):
        if re.fullmatch(r"\d{1,2}\.?", c.text.strip()) and groups and ci + 1 < len(row):
            nxt = row[ci + 1]
            prev_end = groups[-1].numbers[-1].x1 if groups[-1].numbers else groups[-1].label.x1
            if not is_numeric_cell(nxt.text) and len(norm(nxt.text)) > 3 and (nxt.x0 - c.x1) < 0.6 * max(c.x0 - prev_end, 1.0):
                continue                                          # serial number of the next line item ('22 | Total Amount Due | -2572.81')
        split = None if is_numeric_cell(c.text) else _split_label_amount(c)
        if split:
            groups.append(Group(split[0], [split[1]], [], c.page))
            continue
        if is_numeric_cell(c.text):
            if groups:
                groups[-1].numbers.append(c)
            continue
        if groups and re.fullmatch(r"\s*\(.{0,40}\)\s*", c.text):
            # '(e+f+g)', '(0.02 * 3)': an arithmetic note belongs to the label before it, it is not a new label
            if re.search(r"[a-z]\s*[+\-*/]\s*[a-z]", c.text, re.I) or re.search(r"\d+(?:\.\d+)?\s*[+\-*/x]\s*\d+", c.text):
                groups[-1].texts.append(c)
                continue
        # A short unit/word right after a number (e.g. 'HP', 'Ps./U', '%') belongs to the number, not a new label.
        if groups and groups[-1].numbers and len(norm(c.text)) <= 5 and not any(ch.isdigit() for ch in c.text) and c.x0 - groups[-1].numbers[-1].x1 < 3 * c.h:
            groups[-1].texts.append(c)
            continue
        groups.append(Group(c, [], [], c.page))
    return groups


class PageIndex:
    """Fast spatial queries over one page."""

    def __init__(self, page: Page):
        self.page = page
        self.cells = sorted(page.cells, key=lambda c: (c.yc, c.x0))
        self.rows = build_rows(self.cells)
        self.row_of = {}
        for i, row in enumerate(self.rows):
            for c in row:
                self.row_of[id(c)] = i
        self.groups = [g for row in self.rows for g in groups_in_row(row)]
        self.vlabels = self._stacked_labels()

    def _stacked_labels(self) -> list:
        """'Contract' printed above 'Demand(kVA)' reads as 'Contract Demand(kVA)'. Virtual label cells only;
        they are never used as values."""
        out = []
        for i in range(len(self.rows) - 1):
            for a in self.rows[i]:
                if is_numeric_cell(a.text) or len(a.text.strip()) > 28 or len(norm(a.text)) < 4:
                    continue
                for b in self.rows[i + 1]:
                    gap = b.y0 - a.y1
                    if is_numeric_cell(b.text) or len(b.text.strip()) > 28 or gap > 0.9 * a.h or gap < -0.5 * a.h:
                        continue
                    if x_overlap(a, b) < 0.5:
                        continue
                    out.append(Cell(f"{a.text.strip()} {b.text.strip()}", min(a.x0, b.x0), a.y0, max(a.x1, b.x1), b.y1,
                                    min(a.conf, b.conf), a.src, a.page, anchor=b))
                    if i + 2 < len(self.rows):                       # third line of the same header
                        for c3 in self.rows[i + 2]:
                            gap3 = c3.y0 - b.y1
                            if is_numeric_cell(c3.text) or len(c3.text.strip()) > 28 or gap3 > 0.9 * b.h or gap3 < -0.5 * b.h:
                                continue
                            if x_overlap(b, c3) < 0.4:
                                continue
                            out.append(Cell(f"{a.text.strip()} {b.text.strip()} {c3.text.strip()}", min(a.x0, b.x0, c3.x0), a.y0,
                                            max(a.x1, b.x1, c3.x1), c3.y1, min(a.conf, b.conf, c3.conf), a.src, a.page, anchor=c3))
        return out

    def row_index(self, c: Cell) -> int:
        i = self.row_of.get(id(c))
        if i is None and c.anchor is not None:
            i = self.row_of[id(c.anchor)]
        return i

    # --- relations ------------------------------------------------------------------------------
    def right_of(self, label: Cell, limit: int = 4, stop_at_text: bool = True) -> list:
        """Cells on the same row to the right of `label`, nearest first. Stops at the next text label."""
        out = []
        row = self.rows[self.row_index(label)]
        for c in row:
            if c is label or c.x0 < label.x1 - 2:
                continue
            if stop_at_text and not is_numeric_cell(c.text) and len(norm(c.text)) > 2 and out:
                break
            out.append(c)
            if len(out) >= limit:
                break
        return out

    def below(self, ref: Cell, max_rows: int = 3, need_overlap: float = 0.25) -> list:
        """Cells under `ref` (nearest row first) whose horizontal span overlaps it. A text cell that sits
        between ref and a later candidate blocks it (it is a different label)."""
        i = self.row_index(ref)
        out = []
        for j in range(i + 1, min(i + 1 + max_rows, len(self.rows))):
            hits = [c for c in self.rows[j] if x_overlap(ref, c) >= need_overlap or abs(c.xc - ref.xc) <= 0.6 * max(ref.w, c.w)]
            if not hits:
                continue
            nums = [c for c in hits if is_numeric_cell(c.text)]
            pick = min(nums or hits, key=lambda c: abs(c.xc - ref.xc))
            out.append(pick)
            if not is_numeric_cell(pick.text):
                break
        return out

    def unit_tag_near(self, value: Cell):
        """A unit written next to/below a number: '63.08' with '(KW)' beneath it. Returns the tag text or None."""
        i = self.row_index(value)
        for j in (i, i + 1):
            if j is None or j >= len(self.rows):
                continue
            for c in self.rows[j]:
                if c is value or is_numeric_cell(c.text):
                    continue
                if abs(c.xc - value.xc) <= max(value.w, c.w) * 0.8 and re.fullmatch(r"\(?\s*(kva|kw|hp|kvah|kwh|mva)\s*\)?", c.text.strip(), re.I):
                    return c.text.strip("() ").upper()
        return None

    def row_label_cells(self, value_row_index: int, left_of: float) -> list:
        return [c for c in self.rows[value_row_index] if c.x1 <= left_of + 2 and not is_numeric_cell(c.text)]
