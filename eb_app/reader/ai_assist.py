"""
ai_assist.py
============
Step E of the reading pipeline: an OPTIONAL, layout-aware AI fallback for fields the deterministic reader and OCR could not find.

It is OFF by default and cannot run unless BOTH are set:
    EB_AI_ASSIST=1                       an administrator turned the feature on
    EB_AI_PROVIDER=package.module:Class  an APPROVED provider class that implements AIProvider
Nothing is ever sent to a service that was not configured this way, and no provider is bundled with the application.

Safeguards built into the call path
  * only TEXT is shared (never the original PDF/image), and personal data is redacted first (mobile numbers, e-mail, PAN, GSTIN, Aadhaar)
  * the AI may only fill fields that are MISSING; it never overrides or "corrects" a value the reader found
  * everything it returns is marked NEEDS_REVIEW with method 'ai:<provider>' and the quoted source text as evidence
  * a returned value that does not literally appear in the shared text is rejected (no invented numbers)
  * any failure, timeout or odd answer is ignored: the bill simply stays as the reader left it
"""

import importlib
import logging
import os
import re
from typing import Protocol

from .model import Document
from .textnorm import parse_dates, parse_numbers

log = logging.getLogger(__name__)

ASSISTABLE = ("consumer_number", "bill_date", "billing_period", "kwh", "kvah", "pf", "contract_demand", "max_demand", "total_payable")


class AIProvider(Protocol):
    name: str

    def extract(self, page_texts: list, fields: list) -> dict:
        """Return {field: {"value": ..., "quote": "<exact text copied from the bill that supports the value>"}} for any of `fields`."""


def enabled() -> bool:
    return os.environ.get("EB_AI_ASSIST") == "1" and bool(os.environ.get("EB_AI_PROVIDER"))


def load_provider() -> "AIProvider | None":
    spec = os.environ.get("EB_AI_PROVIDER", "")
    if not enabled() or ":" not in spec:
        return None
    try:
        mod, cls = spec.split(":", 1)
        return getattr(importlib.import_module(mod), cls)()
    except Exception as exc:                                    # a mis-configured provider must never break reading
        log.error("AI provider %r could not be loaded: %s", spec, exc)
        return None


_REDACT = [
    (re.compile(r"\b[6-9]\d{9}\b"), "[MOBILE]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[EMAIL]"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[PAN]"),
    (re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z\d]\b"), "[GSTIN]"),
    # Aadhaar: the spaced print format, or digits after the word. A bare 12-digit run is NOT redacted: MSEDCL consumer numbers look the same.
    (re.compile(r"\b[2-9]\d{3}[ -]\d{4}[ -]\d{4}\b"), "[AADHAAR]"),
    (re.compile(r"(?i)(aadhaar|aadhar|uid)\D{0,12}\d{12}\b"), "[AADHAAR]"),
]


def redact(text: str) -> str:
    for pat, repl in _REDACT:
        text = pat.sub(repl, text)
    return text


def page_texts(doc: Document) -> list:
    """Reading-order text of each page (rows top to bottom, cells left to right), redacted."""
    from .layout import build_rows
    out = []
    for p in doc.pages:
        lines = [" | ".join(c.text.strip() for c in row) for row in build_rows(p.cells)]
        out.append(redact("\n".join(lines)))
    return out


def _supported(field: str, value, quote: str, texts: list) -> bool:
    """The answer must be literally present in the text that was shared; otherwise it is a fabrication."""
    blob = "\n".join(texts)
    if not quote or quote not in blob:
        return False
    if isinstance(value, (int, float)):
        return any(abs(v - float(value)) < 1e-6 for v in parse_numbers(quote))
    if field == "billing_period":
        return isinstance(value, (list, tuple)) and len(value) == 2 and len(parse_dates(quote)) >= 2
    if field == "bill_date":
        return bool(parse_dates(quote))
    return str(value) in quote


def assist(doc: Document, missing: list, provider: "AIProvider | None" = None) -> dict:
    """Ask the approved provider for the given missing fields. Returns {field: record} ready to merge, or {} (always safe)."""
    provider = provider or load_provider()
    wanted = [f for f in missing if f in ASSISTABLE]
    if provider is None or not wanted:
        return {}
    texts = page_texts(doc)
    try:
        answer = provider.extract(texts, wanted) or {}
    except Exception as exc:
        log.warning("AI provider %s failed: %s", getattr(provider, "name", "?"), exc)
        return {}
    out = {}
    for field, a in answer.items():
        if field not in wanted or not isinstance(a, dict) or a.get("value") is None:
            continue
        if not _supported(field, a["value"], str(a.get("quote", "")), texts):
            log.warning("AI answer for %s rejected: not supported by the quoted text", field)
            continue
        out[field] = {"value": a["value"], "raw": str(a.get("quote", ""))[:80], "unit": None, "page": 1, "bbox": [0, 0, 0, 0],
                      "label": f"ai:{getattr(provider, 'name', 'provider')}", "relation": "ai", "method": f"ai:{getattr(provider, 'name', 'provider')}",
                      "ocr_conf": 1.0, "confidence": 0.6, "status": "FOUND", "cross_checked": False, "alternatives": [],
                      "evidence": f"AI-assisted; quoted from the bill: '{str(a.get('quote', ''))[:120]}'", "verification": "NEEDS_REVIEW"}
    return out
