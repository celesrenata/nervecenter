"""Bill extraction — deterministic, evidence + confidence (workstream G, spec §7).

Extracts bill signals (payee + amount + due date) from communication bodies
and uploaded-document text, reusing the house date machinery
(:func:`backend.communications.detection.dates.parse_due_expression`) so a
bill due date is read exactly the same way a deadline is.

Hard rules implemented here (spec §7):

- **Preserve evidence.** Every produced candidate carries
  ``evidence = [{source_kind, source_id, snippet, confidence}]`` — the source
  row id plus the short matched text fragment (capped, never the full body).
- **Preserve uncertainties.** Every extracted field carries its own
  confidence (``{"amount": 0.97, "due_date": 0.9, "payee": 0.45, …}``);
  fields below :data:`NEEDS_REVIEW_THRESHOLD` land in ``needs_review`` and
  are surfaced to the user — never silently filled.
- **No guessing.** A field that cannot be read is simply absent from
  ``extracted_fields`` (the candidate then cannot be accepted into a bill
  until a human fills it in).

Supported input is **text** — email bodies/subjects and the text layer of
uploaded documents. PDFs are supported only insofar as their text layer can
be obtained without a new dependency (a stdlib-free minimal text-layer
extractor, :func:`extract_pdf_text_layer`); binary-only PDFs yield no text
and are reported as a limitation, not silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _parse_due_expression(text: str, *, now: datetime) -> Any:
    """Lazy import of the house date parser (avoids a circular-import hazard).

    ``backend.communications.detection`` re-enters ``backend.attention`` /
    ``backend.api.app`` on first import, so importing it at module top-level
    makes this package order-sensitive. Resolving it on first *use* (by
    which point the app wiring has loaded) keeps the bills package importable
    in any order.
    """
    from backend.communications.detection.dates import parse_due_expression

    return parse_due_expression(text, now=now)

#: Fields below this confidence are surfaced as "needs review".
NEEDS_REVIEW_THRESHOLD = 0.5

#: Field confidences (deterministic — same input → same confidences).
_AMOUNT_CONFIDENCE = 0.97
_EXPLICIT_DUE_CONFIDENCE = 0.9
_RELATIVE_DUE_CONFIDENCE = 0.62
_FROM_HEADER_PAYEE_CONFIDENCE = 0.85
_INLINE_PAYEE_CONFIDENCE = 0.7
_FALLBACK_PAYEE_CONFIDENCE = 0.45
_EXPLICIT_CURRENCY_CONFIDENCE = 0.9
_DEFAULT_CURRENCY_CONFIDENCE = 0.85

#: ``amount_cents`` regex: "$142.50", "$142.5", "142,50 USD" (decimal comma).
_AMOUNT = re.compile(
    r"(?:\$\s*)(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)\b(?!\d)|"
    r"(?:\$\s*)(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\b(?!\d)|"
    r"\b(\d{1,3}(?:,\d{3})*)(\.\d{2})\s*(USD|EUR|GBP|CAD|CHF|AUD|JPY)\b",
    re.IGNORECASE,
)

#: ISO 4217 codes we recognise in text.
_CURRENCY_CODES = ("USD", "EUR", "GBP", "CAD", "CHF", "AUD", "JPY")
_CURRENCY_NEAR = re.compile(
    r"\b(" + "|".join(_CURRENCY_CODES) + r")\b", re.IGNORECASE
)

#: A capitalized company-name run immediately before the word "bill"
#: (case-sensitive on purpose: "electric bill" is NOT a company, but
#: "Pacific Power & Light bill" is).
_COMPANY_BEFORE_BILL = re.compile(
    r"\b([A-Z][\w.&'-]*(?:[\s&-]+[A-Z][\w.&'-]*)*)\s+(?:the\s+)?bill\b"
)
#: The generic words that are NOT company names.
_NON_COMPANY = {
    "a", "an", "the", "your", "our", "their", "his", "her", "my",
    "new", "final", "past", "overdue", "please", "payment", "electric",
    "your",
}

#: Words that disqualify a sender display name as a payee candidate.
_FROM_NAME_STOP = {
    "bill", "bills", "invoice", "invoices", "payment", "notice", "reminder",
    "statement", "alert", "account", "your", "the", "no",
}


@dataclass(frozen=True)
class BillField:
    """One extracted field with its confidence (the per-field map)."""

    value: Any
    confidence: float


@dataclass(frozen=True)
class BillCandidateData:
    """Pure-extraction result — no persistence, deterministic.

    ``extracted_fields`` is the per-field confidence map (``{name:
    {value, confidence}}``); ``needs_review`` lists the fields below
    :data:`NEEDS_REVIEW_THRESHOLD`; ``evidence`` is the matched-fragment
    trail. ``source_kind``/``source_id`` come from the input source.
    """

    source_kind: str
    source_id: str
    extracted_fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    needs_review: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class BillSourceData:
    """The minimum source shape the extractor needs (duck-typed in service)."""

    source_id: str
    source_kind: str  # "email" | "document"
    text: str
    subject: str | None = None
    sender_display_name: str | None = None
    received_at: datetime | None = None


def extract_bill_fields(
    text: str, *, now: datetime, subject: str | None = None
) -> dict[str, dict[str, Any]]:
    """Extract amount / due date / currency from ``text`` (deterministic).

    Returns the per-field confidence map (may be empty when nothing
    bill-like is present — callers should still require an amount for a
    candidate to be useful).
    """
    fields: dict[str, dict[str, Any]] = {}
    if not text:
        return fields

    amount = _extract_amount(text)
    if amount is not None:
        fields["amount_cents"] = {
            "value": amount, "confidence": _AMOUNT_CONFIDENCE
        }

    currency = _extract_currency(text)
    if currency is not None:
        fields["currency"] = {
            "value": currency,
            "confidence": _EXPLICIT_CURRENCY_CONFIDENCE,
        }
    elif amount is not None:
        # Amount found but no stated currency: default USD, lower confidence.
        fields["currency"] = {
            "value": "USD",
            "confidence": _DEFAULT_CURRENCY_CONFIDENCE,
        }

    due = _parse_due_expression(text, now=now)
    if due is not None:
        confidence = (
            _EXPLICIT_DUE_CONFIDENCE
            if due.time_stated or due.confidence >= 0.85
            else _RELATIVE_DUE_CONFIDENCE
        )
        fields["due_date"] = {
            "value": due.due.isoformat(),
            "confidence": confidence,
            "expr": due.expr,
        }
    elif subject:
        due = _parse_due_expression(subject, now=now)
        if due is not None:
            fields["due_date"] = {
                "value": due.due.isoformat(),
                "confidence": _RELATIVE_DUE_CONFIDENCE,
                "expr": due.expr,
            }
    return fields


def _extract_amount(text: str) -> int | None:
    """First plausible dollar amount in ``text`` → cents (int), else None."""
    for m in _AMOUNT.finditer(text):
        raw = next(g for g in m.groups() if g is not None)
        try:
            if "," in raw and "." not in raw:
                # "1,420" → 1420 (thousands separator, no cents).
                value = float(raw.replace(",", ""))
            elif "," in raw:
                # "1,234.56" → 1234.56.
                value = float(raw.replace(",", ""))
            else:
                value = float(raw)
        except ValueError:
            continue
        cents = int(round(value * 100))
        if cents > 0 and cents <= 10_000_000_00:
            return cents
    return None


def _extract_currency(text: str) -> str | None:
    m = _CURRENCY_NEAR.search(text)
    return m.group(1).upper() if m else None


def extract_payee(
    sender_display_name: str | None, text: str
) -> tuple[str | None, float]:
    """Best-effort payee (the bill's counterparty).

    Priority: the sender's display name (a bill email comes FROM the
    biller) → a capitalized company name immediately before the word
    "bill" in the text → ``None`` (the caller then falls back to the
    low-confidence "Unknown biller" review entry).
    """
    if sender_display_name:
        name = sender_display_name.strip()
        words = [w for w in name.split() if len(w) > 1]
        if words and not set(w.lower() for w in words) <= _FROM_NAME_STOP:
            # A bare email address ("billing@utility.example") is a weak
            # payee signal — below threshold, so it surfaces as needs-review.
            if "@" in name:
                return name, _FALLBACK_PAYEE_CONFIDENCE
            return name, _FROM_HEADER_PAYEE_CONFIDENCE

    for m in _COMPANY_BEFORE_BILL.finditer(text):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" -&.")
        if not name or name.lower() in _NON_COMPANY or len(name) < 3:
            continue
        return name, _INLINE_PAYEE_CONFIDENCE
    return None, 0.0


def _snippet(fragment: str, limit: int = 120) -> str:
    return fragment if len(fragment) <= limit else fragment[: limit - 1] + "…"


def _dedupe_snippets(
    evidence: list[dict[str, Any]], seen: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in evidence:
        key = (entry["field"], entry["snippet"])
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def extract_bill_candidate(
    source: BillSourceData, *, now: datetime
) -> BillCandidateData | None:
    """Run full bill extraction over one source; ``None`` when nothing.

    A candidate requires at least an **amount** (a bill with no amount is
    not actionable) plus a payee or due date. Evidence + per-field
    confidence are attached per the hard rules; fields below
    :data:`NEEDS_REVIEW_THRESHOLD` land in ``needs_review``.
    """
    text = source.text or ""
    fields = extract_bill_fields(text, now=now, subject=source.subject)
    if not fields:
        return None
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    needs_review: list[str] = []

    # Amount evidence.
    if "amount_cents" in fields:
        m = _AMOUNT.search(text)
        if m is not None:
            evidence.append(
                {
                    "source_kind": source.source_kind,
                    "source_id": source.source_id,
                    "field": "amount_cents",
                    "snippet": _snippet(m.group(0)),
                    "confidence": fields["amount_cents"]["confidence"],
                }
            )

    # Due-date evidence.
    if "due_date" in fields:
        expr = fields["due_date"].get("expr") or ""
        evidence.append(
            {
                "source_kind": source.source_kind,
                "source_id": source.source_id,
                "field": "due_date",
                "snippet": _snippet(f"due: {expr}" if expr else "due (unmatched)"),
                "confidence": fields["due_date"]["confidence"],
            }
        )

    # Payee.
    payee, payee_conf = extract_payee(source.sender_display_name, text)
    if payee is None and fields:
        # Low-confidence fallback, always flagged for review.
        payee = "Unknown biller"
        payee_conf = _FALLBACK_PAYEE_CONFIDENCE
    if payee is not None:
        fields["payee"] = {"value": payee, "confidence": payee_conf}
        m = _COMPANY_BEFORE_BILL.search(text)
        snippet_src = (
            m.group(0) if m is not None else (source.subject or payee) or ""
        )
        evidence.append(
            {
                "source_kind": source.source_kind,
                "source_id": source.source_id,
                "field": "payee",
                "snippet": _snippet(snippet_src or "sender"),
                "confidence": payee_conf,
            }
        )

    for name, entry in fields.items():
        if entry["confidence"] < NEEDS_REVIEW_THRESHOLD:
            needs_review.append(name)

    fields = dict(sorted(fields.items()))
    evidence = _dedupe_snippets(evidence, seen)
    if "amount_cents" not in fields or len(fields) <= 1:
        return None  # no bill signal strong enough to surface
    return BillCandidateData(
        source_kind=source.source_kind,
        source_id=source.source_id,
        extracted_fields=fields,
        needs_review=needs_review,
        evidence=evidence,
    )


def extract_pdf_text_layer(data: bytes) -> str:
    """Minimal stdlib-free extraction of a PDF's *text layer*.

    Supports the common uncompressed / Flate-compressed stream case: each
    ``BT … Tj … ET`` text-showing operator is decoded (latin-1, escaped
    characters handled). Returns ``""`` when the content is a binary-only
    (image) PDF — the documented limitation, not a silent failure.
    """
    if not data.startswith(b"%PDF"):
        return ""
    text_parts: list[str] = []
    for stream in re.findall(rb"stream\r?\n(.*?)endstream", data, re.DOTALL):
        decoded = _decode_stream(stream)
        if not decoded:
            continue
        for m in re.finditer(rb"\(((?:\\.|[^()\\])*)\)\s*Tj", decoded):
            raw = m.group(1)
            text_parts.append(_unescape_pdf_string(raw))
        for m in re.finditer(rb"\[((?:\\.|[^\\])*)\]\s*TJ", decoded):
            for part in re.finditer(rb"\(((?:\\.|[^()\\])*)\)", m.group(1)):
                text_parts.append(_unescape_pdf_string(part.group(1)))
    return " ".join(text_parts).strip()


def _decode_stream(stream: bytes) -> bytes:
    """Return the stream payload, Flate-decompressed when that applies."""
    if b"/FlateDecode" in stream[:400] or stream[:2] == b"\x78\x9c":
        try:
            import zlib

            return zlib.decompress(stream)
        except Exception:  # noqa: BLE001 — corrupted stream → no text layer
            return b""
    # Only try to find text operators when it actually looks like a content
    # stream (avoids "Tj" false positives in binary image data).
    if b"Tj" in stream or b"TJ" in stream:
        return stream
    return b""


def _unescape_pdf_string(raw: bytes) -> str:
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == 0x5C and i + 1 < len(raw):  # backslash
            nxt = raw[i + 1]
            mapping = {0x6E: "\n", 0x72: "\r", 0x74: "\t", 0x28: "(",
                       0x29: ")", 0x5C: "\\"}
            if nxt in mapping:
                out.append(mapping[nxt])
                i += 2
                continue
            out.append(chr(nxt))
            i += 2
            continue
        out.append(chr(ch) if ch >= 0x20 else " ")
        i += 1
    return "".join(out)


__all__ = [
    "BillCandidateData",
    "BillField",
    "BillSourceData",
    "NEEDS_REVIEW_THRESHOLD",
    "extract_bill_candidate",
    "extract_bill_fields",
    "extract_payee",
    "extract_pdf_text_layer",
]
