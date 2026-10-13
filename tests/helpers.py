"""Test helpers: build small synthetic bill PDFs with a real text layer at exact positions (so expected values are known)."""
import pathlib

import pymupdf


FILLER = [(20, 790 + 9 * i, t) for i, t in enumerate([
    "This is a computer generated electricity bill and does not require a signature",
    "Please pay before the due date to avoid disconnection and surcharge as per rules",
    "For complaints contact the section office or the customer care helpline number"])]


def pad(items):
    """Add boilerplate text at the foot of the page, like a real bill has, so the page has a trustworthy text layer."""
    return list(items) + FILLER


def make_pdf(path, items, size=(595, 842), pages=None):
    """items: [(x, y, text)] or [(x, y, text, fontsize)] on page 1; pages: optional list of such lists for further pages."""
    doc = pymupdf.open()
    for page_items in [items] + list(pages or []):
        page = doc.new_page(width=size[0], height=size[1])
        for it in page_items:
            x, y, text = it[0], it[1], it[2]
            fs = it[3] if len(it) > 3 else 9
            page.insert_text((x, y), text, fontsize=fs, fontname="helv")
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()
    return pathlib.Path(path)


def hdr_row(y, cells):
    """[(x, text)] -> items on one row."""
    return [(x, y, t) for x, t in cells]


def tsspdcl_like():
    """A TSSPDCL-style HT bill: KWH / KVAH / KVA column headers, 'Total Consumption' row, slab arithmetic, 'Gross Total 237560.00'."""
    it = []
    it += hdr_row(60, [(41, "Contracted MD (KVA/HP)"), (170, "160"), (299, "Consumer Number"), (428, "CBC3023")])
    it += hdr_row(100, [(41, "DESCRIPTIONS"), (187, "KWH"), (287, "KVAH"), (392, "KVA")])
    it += hdr_row(122, [(41, "Reading On 30-SEP-26"), (174, "1304142.00"), (275, "1468695.00")])
    it += hdr_row(144, [(41, "Total Consumption"), (185, "14097"), (287, "21297"), (385, "91.7132")])
    it += hdr_row(190, [(41, "DESCRIPTIONS"), (194, "RATE"), (353, "KVA/UNITS"), (497, "AMOUNT Rs.")])
    it += hdr_row(212, [(41, "Demand Charges Normal"), (190, "Rs. 500"), (370, "128"), (516, "64000.00")])
    it += hdr_row(234, [(41, "Energy Charges"), (191, "Ps. 765"), (365, "21297"), (511, "162922.05")])
    it += [(475, 280, "Gross Total 237560.00")]
    return it


def ksEB_like():
    """KSEB-style: an 'Average' header strip (must NOT be used) plus the real energy sections and a zone demand table."""
    it = []
    it += hdr_row(40, [(23, "Cons#"), (64, "1356950001519"), (134, "Bill Date"), (175, "03-Oct-2026")])
    it += hdr_row(70, [(33, "Contract"), (262, "MD (kVA)"), (320, "Average"), (395, "PF")])
    it += hdr_row(80, [(23, "Demand(kVA)"), (262, "MD (kVA)"), (308, "Consumption (kWh)"), (395, "PF")])
    it += hdr_row(92, [(41, "75.0"), (269, "47.28"), (336, "6112"), (393, "0.96")])
    it += hdr_row(130, [(22, "1. Energy Consumption(KWh)"), (281, "3. Energy Consumption(KVArh) Lag")])
    it += hdr_row(142, [(22, "Zone"), (69, "FR"), (134, "IR"), (187, "MF"), (238, "Units")])
    it += hdr_row(154, [(29, "1"), (66, "252245.00"), (129, "247952.00"), (181, "1.000"), (259, "4293")])
    it += hdr_row(166, [(29, "2"), (66, "49892.00"), (129, "48882.00"), (181, "1.000"), (259, "1010")])
    it += hdr_row(178, [(196, "Total"), (259, "6311")])
    it += hdr_row(200, [(22, "2. Energy Consumption(KVAh)"), (279, "4. Demand (KVA)"), (402, "Readings"), (540, "Units")])
    it += hdr_row(214, [(29, "Zone"), (69, "FR"), (134, "IR"), (187, "MF"), (238, "Units"), (292, "1"), (437, "47.842"), (570, "47.84")])
    it += hdr_row(226, [(29, "1"), (66, "267882.00"), (129, "263418.00"), (181, "1.000"), (259, "4464"), (292, "2"), (437, "48.668"), (570, "48.67")])
    it += hdr_row(238, [(194, "Total"), (259, "6575"), (292, "3"), (437, "41.541"), (570, "41.54")])
    it += hdr_row(262, [(22, "Ave.PF=KWh/KVAh"), (171, "0.96")])
    it += hdr_row(300, [(374, "Net Payable"), (548, "66835.00")])
    it += hdr_row(320, [(256, "Date of Previous Reading"), (364, "31-Aug-2026")])
    it += hdr_row(332, [(256, "Date of Present Reading"), (365, "30-Sep-2026")])
    return it
