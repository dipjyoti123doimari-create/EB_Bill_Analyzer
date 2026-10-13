"""Synthetic electricity bills (digital PDFs) with exactly known values, for the Phase 3-6 tests.

The layout is deliberately plain 'label ..... value' rows, so the tests check the analysis, scheduling, alerts and web layers
without depending on any particular DISCOM template (template handling is covered by the Phase 2 reader tests and real bills).
"""
import pathlib

import helpers


def bill_items(consumer="123456789012", bill_no="B-1001", bill_date="03-10-2026", start="01-09-2026", end="30-09-2026", due="15-10-2026",
               kwh=90000, kvah=100000, pf=0.90, cd=100, md=92, cd_unit="kVA", energy=700000.0, demand_charges=45000.0, pf_penalty=None,
               xd_penalty=None, total=None, discom_line="Maharashtra State Electricity Distribution Company Ltd (MSEDCL)", extra=None):
    total = total if total is not None else round(energy + demand_charges + (pf_penalty or 0) + (xd_penalty or 0) + 12000, 2)
    rows = [
        ("Consumer Number", consumer), ("Bill Number", bill_no), ("Bill Date", bill_date), ("Billing Period", f"{start} to {end}"), ("Due Date", due),
        ("Contract Demand", f"{cd} {cd_unit}"), ("Maximum Demand", f"{md} {cd_unit}"),
        ("Units Consumed kWh", f"{kwh}"), ("Units Consumed kVAh", f"{kvah}"), ("Power Factor", f"{pf}"),
        ("Energy Charges", f"{energy:.2f}"), ("Demand Charges", f"{demand_charges:.2f}"),
    ]
    if pf_penalty is not None:
        rows.append(("PF Penalty", f"{pf_penalty:.2f}"))
    if xd_penalty is not None:
        rows.append(("Excess Demand Penalty", f"{xd_penalty:.2f}"))
    rows += list(extra or [])
    rows += [("Electricity Duty", "12000.00"), ("Total Payable", f"{total:.2f}")]
    items = [(40, 40, discom_line, 10)]
    for i, (label, value) in enumerate(rows):
        y = 80 + 18 * i
        items += [(40, y, label), (330, y, value)]
    return helpers.pad(items)


def make_bill(path, **kw):
    return helpers.make_pdf(pathlib.Path(path), bill_items(**kw))
