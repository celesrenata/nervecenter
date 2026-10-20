"""Deterministic noise gate for marketing / newsletter / social content.

Precision-first (AC-R05): a communication that is obviously marketing, a
newsletter, or social noise is *suppressed* — no findings are emitted, and
the suppression reason is recorded on the report (without storing
plaintext). The gate is deliberately conservative: it only fires on strong,
unambiguous signals (sender-domain / subject markers), never on a single
word, so personal messages are never silently dropped.

The gate inspects only metadata: the ``sender`` value, the ``source``
channel, and — when a resolver is supplied — the *subject line* only (never
the body), so no plaintext body ever influences a finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.communications.models import Communication

# Sender-domain host labels that unambiguously mark a marketing / newsletter
# sender. Matched as an exact *host label* of a two-level (host.tld) sender
# domain — see :func:`_is_marketing_domain`. This keeps the gate precision-
# first: the marketing label must be the domain's host label, so a keyword
# buried in a longer corporate domain (``offers.corp.example``) never fires,
# and a marketing word in the address local part (``noreply@x``) is not a
# signal by itself (a domain like ``noreply.example`` is marketing, but the
# bare local part is not).
_MARKETING_SENDER_DOMAINS = (
    "newsletter",
    "marketing",
    "digest",
    "news",
    "shop",
    "promos",
    "deals",
    "offers",
    "notifications",
    "socials",
)

# Subject / content markers that unambiguously mark the message as
# marketing / newsletter / social noise. These are multi-word or structural
# markers so a lone word in a personal sentence never triggers the gate.
_MARKETING_MARKERS = (
    "unsubscribe from our newsletter",
    "you are receiving this email because",
    "our latest deals",
    "exclusive offer for you",
    "limited-time offer",
    "act now before it's gone",
    "don't miss out",
    "shop now",
    "special deal for you",
    "follow us on social media",
    "join our community",
    "trending now",
    "you may have missed",
    "highlights from",
    "this week's roundup",
    "weekly digest",
    "your weekly update",
)

# A bare "unsubscribe" link is present in almost every marketing email but
# *not* in personal correspondence.
_UNSUBSCRIBE = re.compile(r"\bunsubscribe\b", re.IGNORECASE)


@dataclass(frozen=True)
class _GateHit:
    reason: str


def _sender_domain(sender: str) -> str:
    """Return the lower-cased domain portion of a sender address.

    Works for ``name <addr>`` and bare addresses. Returns the bare string
    when there is no ``@`` (e.g. a display name or phone handle) — those
    never match a marketing domain.
    """
    s = sender.strip()
    if "<" in s and ">" in s:
        s = s.split("<", 1)[1].split(">", 1)[0]
    s = s.strip()
    if "@" in s:
        return s.split("@", 1)[1].lower()
    return s.lower()


def _host_label(domain: str) -> str | None:
    """The host label of a two-level (``host.tld``) sender domain.

    ``offers.example`` → ``offers``. Returns ``None`` for anything that is not
    a plain two-label domain — display names / phone handles, apex names, and
    subdomains such as ``offers.corp.example`` — so the gate only fires on an
    *exact* marketing host label and never on a keyword buried in a longer
    corporate domain.
    """
    labels = [part for part in domain.lower().split(".") if part]
    if len(labels) != 2:
        return None
    return labels[0]


def is_marketing(communication: Communication) -> _GateHit | None:
    """Return a suppression reason when the communication is marketing /
    newsletter / social noise, else ``None``.

    The check uses only the sender metadata and the content *subject* when a
    resolver exposes one; the body is never read. Deterministic: same
    metadata → same verdict.
    """
    domain = _sender_domain(communication.sender)
    label = _host_label(domain)
    if label is not None and label in _MARKETING_SENDER_DOMAINS:
        return _GateHit(f"sender domain '{domain}' is a marketing/newsletter domain")

    # Structural markers in the subject line only (never the body). The
    # resolver is optional; when absent we only gate on sender domain.
    subject = _subject_of(communication)
    if subject:
        low = subject.lower()
        for marker in _MARKETING_MARKERS:
            if marker in low:
                return _GateHit(f"subject contains marketing marker '{marker}'")
        if _UNSUBSCRIBE.search(subject) and _MARKETING_SENDER_DOMAINS and any(
            frag in low for frag in ("newsletter", "digest", "deal", "offer", "promo")
        ):
            return _GateHit("subject has an unsubscribe link and marketing keywords")
    return None


# The normalized record carries no plaintext subject, so a caller (service or
# test) may register a subject resolver keyed by content_ref. This keeps the
# gate deterministic and metadata-only without the record storing plaintext.
_subject_resolvers: dict[str, str] = {}


def register_subject(content_ref: str, subject: str) -> None:
    """Register the subject line for a content_ref so the gate can read it.

    Only the *subject* (not the body) is kept, in-memory, for the gate. The
    normalized record itself never stores plaintext.
    """
    _subject_resolvers[str(content_ref)] = str(subject or "").strip()


def clear_subjects() -> None:
    """Test hook — drop all registered subjects."""
    _subject_resolvers.clear()


def _subject_of(communication: Communication) -> str | None:
    return _subject_resolvers.get(communication.content_ref)


#: Convenience tuple of the strong sender-domain fragments, exported so tests
#: and the noise fixtures can assert against the gate's exact vocabulary.
NOISE_SUPPRESSIONS = {
    "sender_domains": _MARKETING_SENDER_DOMAINS,
    "subject_markers": _MARKETING_MARKERS,
}


__all__ = [
    "NOISE_SUPPRESSIONS",
    "is_marketing",
    "register_subject",
    "clear_subjects",
]
