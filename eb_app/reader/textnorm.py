"""
textnorm.py
===========
Text normalisation and number/date/unit parsing used by the field extractor.

Numbers keep their sign and precision as printed; nothing is rounded here. Indian digit grouping
(1,23,456.78) and trailing credit markers (123.00 Cr) are understood. A value that cannot be parsed is
returned as None — never guessed.
"""

import re
import unicodedata
from datetime import date, datetime

_NON_ALNUM = re.compile(r"[^a-z0-9\u0900-\u0d7f]+")          # keep Latin letters, digits and the Indic scripts
_NON_ASCII_ALNUM = re.compile(r"[^a-z0-9]+")
_MATRA_I = re.compile("\u093f([\u0915-\u0939\u0958-\u095f])")   # Devanagari 'ि' written BEFORE its consonant in visual-order PDF text
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Writing systems we can recognise from Unicode ranges (used to decide when OCR/AI review is needed).
SCRIPT_RANGES = {
    "devanagari": (0x0900, 0x097F), "bengali": (0x0980, 0x09FF), "gurmukhi": (0x0A00, 0x0A7F),
    "gujarati": (0x0A80, 0x0AFF), "odia": (0x0B00, 0x0B7F), "tamil": (0x0B80, 0x0BFF),
    "telugu": (0x0C00, 0x0C7F), "kannada": (0x0C80, 0x0CFF), "malayalam": (0x0D00, 0x0D7F),
}


# typographic hyphens/minus signs that PDF generators use inside dates and negative numbers
_DASHES = {ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d"}


def fix_devanagari_order(text: str) -> str:
    """PDF generators with legacy fonts emit the short-i vowel sign before the consonant ('िबल' for 'बिल'); put it back."""
    return _MATRA_I.sub(lambda m: m.group(1) + "\u093f", text or "")


def norm(text: str) -> str:
    """Lower-case, strip punctuation/spaces: 'Billed P.F.' -> 'billedpf'. Indic letters are KEPT (Hindi labels are matched),
    '#' becomes 'hash' so 'Cons#' (KSEB) is not confused with 'Cons' (consumption)."""
    t = fix_devanagari_order(text or "").replace("#", "hash")
    t = unicodedata.normalize("NFC", t).lower().translate(_DEVANAGARI_DIGITS)
    return _NON_ALNUM.sub("", t)


def norm_ascii(text: str) -> str:
    """Like norm() but Latin letters/digits only: the English part of a bilingual label."""
    return _NON_ASCII_ALNUM.sub("", unicodedata.normalize("NFKD", (text or "").replace("#", "hash")).lower())


def script_counts(text: str) -> dict:
    out: dict = {}
    for ch in text:
        o = ord(ch)
        for name, (lo, hi) in SCRIPT_RANGES.items():
            if lo <= o <= hi:
                out[name] = out.get(name, 0) + 1
                break
    return out


_NUM = re.compile(r"(?<![\w.])([-+]?\(?)\s*(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)(\)?)(?:\s*(cr|dr)\b\.?)?(?![\w])", re.I)
# OCR often reads the thousands comma of '3,234.47' as a full stop: '3.234.47'. Digits-dot-three digits-dot-two digits cannot be a date or a decimal.
_DOT_THOUSANDS = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{3})\.(\d{2})(?![\d.])")


def parse_numbers(text: str) -> list:
    """All numbers in a string as floats, in order. '(1,234.50)' and '1,234.50 Dr' are negative; 'Cr' is positive."""
    out = []
    text = re.sub(r"[`\u20b9]", " ", (text or "")).translate(_DEVANAGARI_DIGITS).translate(_DASHES)       # some bills print the rupee sign as a backtick
    text = _DOT_THOUSANDS.sub(r"\1,\2.\3", text)
    text = re.sub(r"(?i)\b(rs|ps|inr)\.?(?=\d)", r"\1 ", text)                          # 'Rs.765' -> 'Rs 765'
    for m in _NUM.finditer(text):
        sign, digits, close, crdr = m.group(1) or "", m.group(2), m.group(3) or "", (m.group(4) or "").lower()
        try:
            val = float(digits.replace(",", ""))
        except ValueError:
            continue
        if "-" in sign or ("(" in sign and ")" in close) or crdr == "dr":
            val = -val
        out.append(val)
    return out


def first_number(text: str):
    nums = parse_numbers(text)
    return nums[0] if nums else None


_ONLY_NUMBER = re.compile(r"^[\s:\-]*[-+(]?\s*[\d,]*\.?\d+\)?\s*(cr|dr)?\.?\s*(/-)?\s*$", re.I)
_CURRENCY_PREFIX = re.compile(r"^\s*(?:rs\.?|ps\.?|inr|paise|\u20b9|`)\s*", re.I)


def is_numeric_cell(text: str) -> bool:
    t = _DOT_THOUSANDS.sub(r"\1,\2.\3", _CURRENCY_PREFIX.sub("", (text or "").translate(_DEVANAGARI_DIGITS)))
    return bool(_ONLY_NUMBER.match(t))


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE_PATTERNS = [
    re.compile(r"(?<!\d)(\d{1,2})[-/. ]([A-Za-z]{3,9})[-/. ,]+(\d{2,4})(?!\d)"),           # 01-Sep-2026, 1 Sept 26
    re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)"),                       # 2026-09-01
    re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})(?!\d)"),                      # 01/09/2026 (day first — Indian bills)
    re.compile(r"(?<!\d)([A-Za-z]{3,9})[-/. ]+(\d{1,2})[,]?[-/. ]+(\d{4})(?!\d)"),         # Sep 01, 2026
]


def _mk(y, m, d):
    try:
        if y < 100:
            y += 2000
        return date(y, m, d)
    except ValueError:
        return None


_DATE_ANY = re.compile(
    r"(?<!\d)(?:(\d{1,2})[-/. ]([A-Za-z]{3,9})[-/. ,]+(\d{2,4})"            # 01-Sep-2026, 1 Sept 26
    r"|(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"                                # 2026-09-01
    r"|(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})"                               # 01/09/2026 (day first, Indian convention)
    r"|([A-Za-z]{3,9})[-/. ]+(\d{1,2})[,]?[-/. ]+(\d{4}))(?!\d)")            # Sep 01, 2026


def parse_dates(text: str) -> list:
    """All dates in a string, in order of appearance, without overlaps. Numeric dates are read day-first."""
    t = (text or "").translate(_DEVANAGARI_DIGITS).translate(_DASHES)
    out, seen = [], set()
    for m in _DATE_ANY.finditer(t):
        g = m.groups()
        d = None
        if g[0] is not None:
            mon = _MONTHS.get(g[1][:3].lower())
            d = _mk(int(g[2]), mon, int(g[0])) if mon else None
        elif g[3] is not None:
            d = _mk(int(g[3]), int(g[4]), int(g[5]))
        elif g[6] is not None:
            d = _mk(int(g[8]), int(g[7]), int(g[6]) if False else int(g[6]))
            d = _mk(int(g[8]), int(g[7]), int(g[6]))
        elif g[9] is not None:
            mon = _MONTHS.get(g[9][:3].lower())
            d = _mk(int(g[11]), mon, int(g[10])) if mon else None
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


_UNIT_PATTERNS = [
    ("kVAh", re.compile(r"kv\s*ah\b", re.I)), ("kVARh", re.compile(r"kv\s*ar\s*h\b", re.I)),
    ("kWh", re.compile(r"kw\s*h\b", re.I)), ("kVA", re.compile(r"\bkv\s*a\b", re.I)),
    ("kW", re.compile(r"\bkw\b", re.I)), ("HP", re.compile(r"\bhp\b", re.I)), ("MVA", re.compile(r"\bmva\b", re.I)),
]


def detect_unit(text: str):
    for name, pat in _UNIT_PATTERNS:
        if pat.search(text or ""):
            return name
    return None
