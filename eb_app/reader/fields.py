"""
fields.py
=========
The semantic dictionary of the reader: what each field is called on Indian electricity bills.

This is DATA, not layout: there are no coordinates, no page numbers and no per-board templates. A label
synonym is matched against the text of any cell wherever it sits on the page; the layout engine then finds the
value by its spatial relationship to that label. Adding support for a new wording means adding a synonym here.

Synonyms are stored normalised (see textnorm.norm): lower-case letters and digits only.
  * synonyms of 4+ characters match as a substring of a short label cell ("billperiod" in "01Bill Period")
  * synonyms of 3 characters or fewer ("pf", "md", "cd") must equal a whole word of the label, so "pf" never
    matches "kvapfc" or "ipfs".
Similar-looking labels are NOT assumed to be the same thing (contract demand != sanctioned load != recorded MD
!= billing demand); each has its own field and its own words.
"""

import difflib
import re
from dataclasses import dataclass
from functools import lru_cache

from .textnorm import norm, norm_ascii

_WORDS = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class FieldSpec:
    id: str
    kind: str                    # number | date | daterange | text | id
    labels: tuple = ()           # label synonyms (normalised)
    weak: tuple = ()             # lower-trust synonyms (scored x0.85): usually, but not always, this field
    unit_rows: tuple = ()        # a row whose label cell is exactly this unit ('kwh') carries the value at its end
    cols: tuple = ()             # column-header synonyms for table intersection
    rows: tuple = ()             # row-label synonyms for table intersection (earlier = preferred)
    exclude: tuple = ()          # a label containing any of these (normalised substring) is not this field
    unit_family: str = ""        # energy | demand | none
    lo: float = None             # plausible range for number fields
    hi: float = None
    critical: bool = False
    relations: tuple = ("inline", "right", "below")


def _n(*words):
    return tuple(norm(w) for w in words)


FIELDS = {f.id: f for f in [
    # ------------------------------------------------------------------ identity / dates
    FieldSpec("consumer_number", "id", critical=True, relations=("inline", "right", "below"),
              labels=_n("सर्विस क्रमांक", "सेवा क्रमांक", "सर्विस नं", "उपभोक्ता क्रमांक", "उपभोक्ता संख्या", "उपभोक्ता सं", "खाता संख्या", "कनेक्शन संख्या", "अकाउंट सं", "Consumer No", "Consumer Number", "Consumer ID", "Consumer Account No", "Consumer Code", "Con No",
                        "Service No", "Service Connection No", "Service Connection Number", "Servie Connection Number", "Service Connect No", "SC No", "USC No", "K No", "Account No", "Account Number",
                        "Acc Id", "Account ID", "CA Number", "CA No", "RR No", "IVRS No", "Connection No", "Cons#",
                        "Consumer No.", "Customer No", "Customer ID", "Customer Number", "Contract Account", "Contract Account No",
                        "UKSC No", "HT SC No", "Service Number", "Cons Number", "Consumer Id No", "Consumer#", "Account"),
              exclude=_n("name", "address", "category", "type", "load", "demand", "care", "helpline", "mobile", "email",
                         "consumption", "consumed")),
    FieldSpec("bill_number", "id", critical=False, labels=_n("Bill No", "Bill Number", "Invoice No", "Invoice Number", "Bill ID",
                                                          "Bill Sl No", "Bill Serial No", "Bill No.", "Receipt No"),
              exclude=_n("date", "period", "amount", "month", "last")),
    FieldSpec("bill_date", "date", critical=True, labels=_n("बिल दिनांक", "बिल तिथि", "बिल की तारीख", "बिल जारी करने की तिथि", "Bill Date", "Date of Bill", "Bill Dt", "Date of Issue", "Issue Date",
                                                           "Bill Generation Date", "Bill Issue Date", "Billed Date", "Invoice Date",
                                                           "Bill Dated", "Date of Billing", "Billing Date", "Bill Generated On"),
              weak=_n("Dated"),
              exclude=_n("due", "reading", "payment", "previous", "present", "last", "connection", "disconnection", "dc", "payable",
                         "before", "supply", "energisation")),
    FieldSpec("due_date", "date", labels=_n("देय तिथि", "देय दिनांक", "भुगतान की अंतिम तिथि", "अंतिम तिथि", "Due Date", "Due Dt", "Payable On or Before", "Last Date of Payment", "Pay Before",
                                          "Last Payment Date", "Payment Due Date", "Due Date for Payment", "Pay By", "Due On",
                                          "Last Date for Payment", "Please Pay By"),
              exclude=_n("late", "after")),
    FieldSpec("billing_period", "daterange", critical=True,
              labels=_n("बिलिंग अवधि", "बिल अवधि", "बिल माह", "उपभोग अवधि", "Bill Period", "Billing Period", "Period of Bill", "Billing Cycle", "Bill for the Period", "Period",
                        "Consumption Period", "Bill From To", "Billing Month Period", "Reading Period", "For the Period",
                        "Bill Period From", "Duration of Meter Reading", "Meter Reading Period", "Period of Consumption",
                        "Billing Duration", "Bill From")),
    FieldSpec("prev_reading_date", "date", cols=_n("Reading Date", "Reading Details", "Date of Reading", "Date"), rows=_n("Previous", "Prev", "Last", "Old", "Initial"), labels=_n("Previous Reading Date", "Date of Previous Reading", "Prev Reading Date",
                                                    "Last Reading Date", "Previous Reading On", "Prev Rdg Date", "Previous Meter Reading Date",
                                                    "Reading Date Previous", "Prev. Rdg. Date", "Previous Read Date")),
    FieldSpec("present_reading_date", "date", cols=_n("Reading Date", "Reading Details", "Date of Reading", "Date"), rows=_n("Current", "Present", "New", "Final"), labels=_n("Present Reading Date", "Current Reading Date", "Date of Present Reading",
                                                       "Date of Current Reading", "Reading Date", "Pres Rdg Date", "Present Read Date",
                                                       "Reading Taken On", "Pres. Rdg. Date", "Current Read Date"),
              exclude=_n("previous", "prev", "last")),
    FieldSpec("meter_number", "id", labels=_n("Meter No", "Meter Number", "Meter Sl No", "Meter Serial No", "Meter ID",
                                              "Meter Slno", "Meter Sr No", "Meter No.", "Meter Serial Number", "Mtr No"),
              exclude=_n("reading", "rent", "constant", "factor")),
    FieldSpec("consumer_name", "text", labels=_n("Consumer Name", "Name of Consumer", "Name of the Consumer", "Customer Name",
                                                 "Name of Customer", "Consumer", "Name"),
              exclude=_n("no", "number", "id", "code", "account", "category")),
    # ------------------------------------------------------------------ consumption (energy)
    FieldSpec("kwh", "number", unit_family="energy", critical=True, lo=0, hi=5e7,
              cols=_n("KWH", "KWh", "kW h", "Active Energy KWh", "KWH Import", "Import KWh", "Units KWh", "kWh Units", "Active KWH", "KWHr",
                      "Consumption", "Total Units", "Metered Units", "Units Consumed", "Net Units"),
              rows=_n("Total Consumption", "Net Consumption", "Consumption", "Units Consumed", "Net Units", "Total Units",
                      "Net Energy", "Billed Units", "Units", "Difference", "Net Consumed", "Total Energy", "Energy Consumed"),
              labels=_n("KWH Consumption", "Energy Consumption KWh", "Units Consumed KWh", "Total KWh", "Net KWh", "Consumption KWh",
                        "KWh Units", "Import KWh", "Active Energy", "Energy Consumed KWh", "KWH Consumed", "Units Consumed (KWH)",
                        "Billed Units KWh", "Consumption After MF", "Consumption After MF DT Loss"),
              weak=_n("Unit Consumed", "Units Consumed", "Consumption"), unit_rows=_n("kwh"),
              exclude=_n("kvah", "kvarh", "rate", "per", "charge", "average", "avg", "ave", "pf", "powerfactor", "demand", "load", "reactive",
                         "lag", "lead", "rkvah", "kvar")),
    FieldSpec("kvah", "number", unit_family="energy", critical=True, lo=0, hi=5e7,
              cols=_n("KVAH", "KVAh", "kVA h", "Apparent Energy", "KVAH Import", "Import KVAh", "Units KVAh", "KVAHr"),
              rows=_n("Total Consumption", "Net Consumption", "Consumption", "Units Consumed", "Net Units", "Total Units",
                      "Net Energy", "Billed Units", "Units", "Difference", "Net Consumed", "Total Energy", "Energy Consumed"),
              labels=_n("KVAH Consumption", "Energy Consumption KVAh", "Units Consumed KVAh", "Total KVAh", "Net KVAh", "Consumption KVAh",
                        "KVAh Units", "Apparent Energy", "Energy Consumed KVAh", "KVAH Consumed", "Units Consumed (KVAH)",
                        "Billed Units KVAh"), unit_rows=_n("kvah"),
              exclude=_n("kwh", "kvarh", "rate", "per", "charge", "average", "avg", "ave", "pf", "powerfactor", "demand", "load", "reactive",
                         "lag", "lead", "rkvah", "kvar")),
    FieldSpec("billed_units", "number", unit_family="energy", lo=0, hi=5e7,
              cols=_n("Billed Units", "Units Billed", "Billing Units", "Billed"),
              rows=_n("Total Consumption", "Net Consumption", "Consumption", "Total", "Units", "Net"),
              labels=_n("Billed Units", "Units Billed", "Billing Units", "Net Billed Units", "Chargeable Units", "Units for Billing",
                        "Energy Billed", "Billable Units", "Billed Units in KWh", "Net Units Billed"),
              exclude=_n("kwh", "kvah", "rate", "charge", "amount", "rs")),
    FieldSpec("reading_kwh_current", "number", unit_family="energy", lo=0, hi=1e10,
              cols=_n("KWH", "KWh", "Active Energy KWh", "KWH Meter", "KWH Import"), rows=_n("Current", "Present", "Final Reading", "Pres Reading", "Closing")),
    FieldSpec("reading_kwh_previous", "number", unit_family="energy", lo=0, hi=1e10,
              cols=_n("KWH", "KWh", "Active Energy KWh", "KWH Meter", "KWH Import"), rows=_n("Previous", "Prev Reading", "Past Reading", "Initial Reading", "Opening")),
    FieldSpec("kvarh_lag", "number", unit_family="energy", lo=0, hi=5e7,
              cols=_n("RKVAH Lag", "KVARH Lag", "kVARh (Lag)", "RKVAH(LAG)", "Reactive Lag", "KVARH LAG"),
              rows=_n("Total Consumption", "Net Consumption", "Consumption", "Units", "Total", "Difference"),
              labels=_n("RKVAH Lag", "KVARH Lag", "Reactive Energy Lag", "Total kVArh Lag", "RKVAH (LAG)")),
    FieldSpec("kvarh_lead", "number", unit_family="energy", lo=0, hi=5e7,
              cols=_n("RKVAH Lead", "KVARH Lead", "kVARh (Lead)", "RKVAH(LEAD)", "Reactive Lead", "KVARH LEAD"),
              rows=_n("Total Consumption", "Net Consumption", "Consumption", "Units", "Total", "Difference"),
              labels=_n("RKVAH Lead", "KVARH Lead", "Reactive Energy Lead", "kVArh Lead", "RKVAH (LEAD)")),
    FieldSpec("multiplying_factor", "number", lo=0.0001, hi=1e6,
              labels=_n("Multiplying Factor", "Multiplication Factor", "Meter Constant", "Constant", "Multiplier", "MF", "M F", "Meter MF",
                        "Mult Factor", "CT PT Ratio", "Overall MF")),
    # ------------------------------------------------------------------ power factor
    FieldSpec("pf_billed", "number", critical=True, lo=0.0, hi=1.0001, unit_family="none",
              labels=_n("बिल्ड पावर फैक्टर", "Billed PF", "PF Billed", "Billed Power Factor", "Power Factor Billed", "PF for Billing", "PF as per Bill",
                        "Billing PF", "Billed P.F."),
              exclude=_n("incentive", "penal", "penalty", "charge", "surcharge", "rebate", "amount", "rs", "limit", "below", "kvarh", "rkvah")),
    FieldSpec("pf", "number", critical=True, lo=0.0, hi=1.0001, unit_family="none",
              labels=_n("पावर फैक्टर", "शक्ति गुणांक", "PF", "P.F.", "P F", "Power Factor", "Avg PF", "Average PF", "Average Power Factor", "Avg Power Factor", "Ave PF",
                        "Monthly PF", "PF Average", "Avg. P.F.", "Average P.F.", "Weighted PF", "Cos Phi", "Power Factor Average",
                        "Consumption PF", "Actual PF", "Recorded PF", "Calculated PF", "Avg PF kWh/kVAh"),
              weak=_n("Assessed PF"),
              exclude=_n("incentive", "penal", "penalty", "charge", "surcharge", "rebate", "amount", "rs", "limit", "below", "kvarh",
                         "rkvah", "disincentive", "benefit", "threshold", "slab", "rate", "unit")),
    # ------------------------------------------------------------------ demand / load
    FieldSpec("contract_demand", "number", critical=True, unit_family="demand", lo=0.5, hi=200000,
              labels=_n("संविदा मांग", "अनुबंधित मांग", "संविदित मांग", "Contract Demand", "Contracted Demand", "Contracted MD", "Contract MD", "Agreed Demand", "Sanctioned Demand",
                        "Contract Demand KVA", "Contracted Load", "Contract Load", "Load CD", "Load / CD", "Sanctioned CD", "Sanction CD", "Contracted Maximum Demand",
                        "Contract Demand (KVA)", "Contracted MD KVA HP", "Contracted Demand KW", "Contract Demand in KVA"),
              exclude=_n("excess", "exceed", "interest", "penal", "charge", "ref", "oth", "deposit", "rate", "75of", "130of", "per")),
    FieldSpec("sanctioned_load", "number", unit_family="demand", lo=0.1, hi=200000,
              labels=_n("स्वीकृत भार", "सवीकृत भार", "Sanctioned Load", "Sanction Load", "Sanc Load", "Sanctioned Load KW", "Sanct Load", "Load Sanctioned", "Sanctioned KW",
                        "Sanctioned Connected Load", "Sanctioned Load (kW/KVA)", "Sanction Load(KW/HP)"),
              weak=_n("Load (kva)", "Load KVA", "Load KW"),
              exclude=_n("deposit", "charge", "rate")),
    FieldSpec("connected_load", "number", unit_family="demand", lo=0.1, hi=200000,
              labels=_n("Connected Load", "Conn Load", "Connected Load KW", "Total Connected Load"),
              exclude=_n("deposit", "charge", "rate")),
    FieldSpec("max_demand", "number", critical=True, unit_family="demand", lo=0.0, hi=200000,
              cols=_n("KVA MD", "MD KVA", "KW MD", "MD KW", "KVA", "MD", "Maximum Demand", "Max Demand", "Recorded MD", "Demand KVA"),
              rows=_n("Total Consumption", "Consumption", "Current", "Present", "Reading", "Difference", "Recorded", "Max"),
              labels=_n("अधिकतम मांग", "Recorded MD", "Maximum Demand", "Max Demand", "MD Recorded", "Actual MD", "KVA MD", "MD KVA",
                        "Highest Demand", "Max MD", "Demand Recorded", "MD Reading", "Maximum Demand KVA", "Recorded Maximum Demand",
                        "MD (KVA)", "MD", "MDI", "Peak Demand", "Maximum Demand Recorded", "KW MD", "Maximum Demand Indicator",
                        "Max Dmd Recorded with MF", "Max Demand Recorded with MF", "Max Dmd Recorded With Multiplying Factor"),
              weak=_n("Recorded Demand", "Actual Demand", "Max Dmd Recorded"), unit_rows=_n("kva"),
              exclude=_n("contract", "billed", "billing", "sanction", "charge", "penal", "excess", "average", "avg", "ave", "rate", "assess",
                         "75of", "130of")),
    FieldSpec("billing_demand", "number", unit_family="demand", lo=0.0, hi=200000,
              labels=_n("बिल्ड डिमांड", "बिल की गई मांग", "बिलिंग मांग", "Billing Demand", "Billed Demand", "Demand Billed", "Chargeable Demand", "Billing MD", "Demand for Billing",
                        "Billed MD", "Billable Demand", "Demand Charged On", "Billed Maximum Demand", "KVA Billed"),
              exclude=_n("penal", "units", "unit", "kvah", "kwh")),
]}

# --------------------------------------------------------------------------- charge-line categories
# Matched (in this order, first hit wins) against the label of a "label ... amount" row.
# (category, include substrings, exclude substrings)
CHARGE_RULES = [
    ("fixed_charges", _n("नियतप्रभार", "नियत प्रभार", "स्थिर प्रभार", "नियत/मांग प्रभार"), ()),
    ("energy_charges", _n("ऊर्जा प्रभार", "उर्जा प्रभार", "ऊर्जाप्रभार"), ()),
    ("electricity_duty", _n("विद्युत शुल्क", "बिजली शुल्क"), ()),
    ("fuel_adjustment", _n("ईंधन अधिभार", "ईधन अधिभार", "एफपीपीएएस"), ()),
    ("late_payment", _n("विलंब भुगतान", "विलम्ब भुगतान", "देरी से भुगतान"), ()),
    ("arrears", _n("बकाया", "पिछला बकाया"), ()),
    ("pf_adjustment", _n("pfpenal", "pfincentive", "pfdisincentive", "pfsurcharge", "pfrebate", "pfadjust", "powerfactorpenal",
                         "powerfactorincentive", "powerfactorsurcharge", "lowpf", "pfcharge", "penaltyforlowpf", "rkvahsurcharge",
                         "rkvahcharge", "pfpenalty", "pfbenefit", "pfrelated"), ()),
    ("excess_demand_penalty", _n("excessdemand", "excessmd", "excesscd", "excesscontract", "mdpenalty", "demandpenal", "exloadmd",
                                "exceedingcontract", "penaltyforexcess", "penalmd", "mdexcess", "overdrawal", "excesskva", "demandchargespenal"),
     ()),
    ("late_payment", _n("latepayment", "delayedpayment", "delaypayment", "dpsc", "lpsc", "belatedpayment", "latefee", "interestondelayed",
                       "surchargeonlate", "currentinterest", "interestonarrear", "interestarrear", "penalinterest", "interestcharges"), ()),
    ("arrears", _n("arrear", "previousbalance", "previousdues", "outstanding", "balancebf", "previousunpaid", "pastdues", "unpaidbalance",
                  "previousbill"), _n("interest",)),
    ("fixed_charges", _n("fixedcharge", "customercharge", "fixedchg", "serviceconnectioncharge", "meterrent", "meterrental", "minimumcharge"), ()),
    ("demand_charges", _n("demandcharge", "mdcharge", "kvacharge", "contractdemandcharge", "demandchg", "maximumdemandcharge"), _n("penal", "excess")),
    ("fuel_adjustment", _n("fueladjustment", "fueladj", "fuelcost", "fuelsurcharge", "fppca", "fpppa", "ppca", "powerpurchasecost", "fuelandpower", "fuelcharge",
                          "fuelsurchargeadjust", "fac", "fsa", "fca", "monthlyfuel", "fuelpower", "powerpurchaseadjust"), ()),
    ("electricity_duty", _n("electricityduty", "eduty", "electricitytax", "taxonsale", "dutyonenergy", "elecduty", "electricityduty", "eduty"), ()),
    ("wheeling", _n("wheeling",), ()),
    ("energy_charges", _n("energycharge", "ec", "energychg", "unitcharge", "consumptioncharge", "energybill", "todtariffec", "todcharge",
                         "energyamount"), _n("surcharge", "duty", "tax", "fuel", "lighting", "colony", "wheeling")),
    ("taxes_surcharges", _n("surcharge", "gst", "cess", "greenenergy", "taxes", "educationcess", "cgst", "sgst", "tcs", "pslcharge"), ()),
    ("adjustments_credits", _n("adjustment", "credit", "rebate", "subsidy", "roundoff", "roundingoff", "promptpayment", "waiver",
                              "refund", "incentive"), ()),
]

TOTAL_BILL_LABELS = _n("वर्तमान बिल राशि", "चालू बिल राशि", "Total Current Bill", "Current Bill", "Current Month Bill", "Current Bill Amount", "Total Current Charges",
                       "Current Charges", "Bill Amount", "Total Bill", "Current Month Charges", "Current Demand",
                       "Total Charges", "Total Energy Bill", "Net Current Bill", "Total Amount", "Sub Total", "Total(add 1 to 9)", "Total Add")
TOTAL_PAYABLE_LABELS = _n("कुल देय राशि", "शुद्ध देय राशि", "देय राशि", "Net Bill Payable", "Total Bill Payable", "Total Bill Amt", "Net Payable", "Total Amount Payable", "Amount Payable", "Payable Amount", "Total Payable", "Net Amount Payable",
                          "Grand Total", "Total Bill Amount", "Total Amount Due", "Amount Due", "Gross Total", "Net Bill Amount",
                          "Net Amount", "Bill Amount Payable", "Total Payable Amount", "Payable By Due Date", "Amount Payable by Due Date",
                          "Pay This Amount", "Total Dues", "Net Demand", "Payable on or before", "Amount to be Paid", "Total Bill Amount Rounded")
TOTAL_PAYABLE_EXCLUDE = _n("after", "late", "delay", "words", "interest", "if paid after", "ifpaidafter", "rupees", "last")

# DISCOM detection from anywhere in the document text: (regex on lower-case text, code, state)
DISCOMS = [
    (r"\bbescom\b|bangalore electricity supply", "BESCOM", "Karnataka"),
    (r"\bmescom\b|mangalore electricity", "MESCOM", "Karnataka"),
    (r"\bhescom\b|\bgescom\b|\bcescmysore\b|\bcesc mysore", "KARNATAKA-OTHER", "Karnataka"),
    (r"mahadiscom|msedcl|maharashtra state electricity|महाराष्ट्र राज्य विद्युत|महावितरण", "MSEDCL", "Maharashtra"),
    (r"adani electricity mumbai|\baeml\b", "ADANI-MUMBAI", "Maharashtra"),
    (r"\bbest\b.{0,40}undertaking|brihanmumbai electric", "BEST", "Maharashtra"),
    (r"torrent power", "TORRENT", "Gujarat/Maharashtra"),
    (r"tata power", "TATA-POWER", "Maharashtra/Odisha/Delhi"),
    (r"tsspdcl|tgspdcl|southern power distribution company of t|telangana", "TGSPDCL", "Telangana"),
    (r"apcpdcl|apepdcl|apspdcl|andhra pradesh", "APDISCOM", "Andhra Pradesh"),
    (r"kseb|kerala state electricity", "KSEB", "Kerala"),
    (r"tangedco|tnpdcl|tneb|tamil ?nadu (generation|electricity|power)", "TNPDCL", "Tamil Nadu"),
    (r"dhbvn|dakshin haryana|दक्षिण हरियाणा", "DHBVN", "Haryana"),
    (r"uhbvn|uttar haryana|उत्तर हरियाणा", "UHBVN", "Haryana"),
    (r"bses|rajdhani|yamuna power", "BSES", "Delhi"),
    (r"pspcl|punjab state power", "PSPCL", "Punjab"),
    (r"uppcl|purvanchal|madhyanchal|paschimanchal|dakshinanchal|uttar pradesh power|पूर्वांचल|मध्यांचल|पश्चिमांचल|दक्षिणांचल|उत्तर प्रदेश पावर", "UPPCL", "Uttar Pradesh"),
    (r"jvvnl|avvnl|jdvvnl|jaipur vidyut|ajmer vidyut|jodhpur vidyut|rajasthan|जयपुर विद्युत|अजमेर विद्युत|जोधपुर विद्युत|राजस्थान", "RAJASTHAN-DISCOM", "Rajasthan"),
    (r"dgvcl|ugvcl|mgvcl|pgvcl|gujarat vij|guvnl", "GUVNL", "Gujarat"),
    (r"wbsedcl|west bengal state electricity", "WBSEDCL", "West Bengal"),
    (r"cesc limited|cesc ltd", "CESC", "West Bengal"),
    (r"\bapdcl\b|assam power distribution", "APDCL", "Assam"),
    (r"mppkvvcl|mpmkvvcl|mppkv|madhya pradesh (poorv|paschim|madhya)", "MP-DISCOM", "Madhya Pradesh"),
    (r"\bc?s?pdcl\b|chhattisgarh state power|छत्तीसगढ़ राज्य विद्युत|छत्तीसगढ राज्य विद्युत", "CSPDCL", "Chhattisgarh"),
    (r"jbvnl|jharkhand bijli", "JBVNL", "Jharkhand"),
    (r"sbpdcl|nbpdcl|bihar", "BIHAR-DISCOM", "Bihar"),
    (r"tp central odisha|tpcodl|tpwodl|tpnodl|tpsodl|odisha", "TP-ODISHA", "Odisha"),
]


# --------------------------------------------------------------------------- matching
def words_of(text: str) -> list:
    return _WORDS.findall((text or "").lower())


def _is_indic(s: str) -> bool:
    return any("\u0900" <= ch <= "\u0d7f" for ch in s)


@lru_cache(maxsize=400_000)
def _score_cached(text: str, synonyms: tuple, exclude: tuple) -> float:
    full = norm(text)
    if not full or len(full) > 90:
        return 0.0
    en_syn = tuple(s for s in synonyms if not _is_indic(s))
    in_syn = tuple(s for s in synonyms if _is_indic(s))
    best_in = 0.0
    for s in in_syn:                                   # Hindi etc.: substring of the whole label counts, however much English sits beside it
        if s in full:
            best_in = max(best_in, 1.0 if full == s else 0.92)
    n = norm_ascii(text)                               # English synonyms are judged on the English part only
    if not n or len(n) > 70:
        return best_in if not any(x and x in full for x in exclude) else 0.0
    if any(x and (x in n or (_is_indic(x) and x in full)) for x in exclude):
        return 0.0
    synonyms = en_syn
    toks = {norm_ascii(w) for w in words_of(text)}
    best = 0.0
    for s in synonyms:
        if not s:
            continue
        if len(s) <= 3:                                   # short abbreviations: whole word only
            if n == s:
                best = max(best, 1.0)
            elif s in toks and len(toks) <= 4:
                best = max(best, 0.85)
            continue
        if n == s:
            return 1.0
        if s in n:
            extra = len(n) - len(s)
            best = max(best, 0.95 if extra <= 3 else 0.9 if extra <= 8 else 0.8 if extra <= 18 else 0.6)
        elif len(s) >= 7 and abs(len(n) - len(s)) <= 2:
            # tolerate one or two OCR slips: 'powerfactor' read as 'powerfactoe' (same length +-2, never a dropped word)
            if difflib.SequenceMatcher(None, n, s).ratio() >= 0.9:
                best = max(best, 0.75)
    return max(best, best_in)


def label_score(text: str, synonyms, exclude=()) -> float:
    """How well does this cell text read as one of the synonyms? 0 = not at all, 1 = exact."""
    return _score_cached(text, tuple(synonyms), tuple(exclude))


def any_known_label(text: str) -> bool:
    """True if the cell reads as a label of ANY field (used to stop a search at the next label)."""
    for f in FIELDS.values():
        if label_score(text, f.labels, f.exclude) >= 0.8:
            return True
    return False
