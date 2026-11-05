"""Deterministic relevance classification + scoring (attention-relevance
FEAT-002 — Layer 1, the core fix).

Replaces the flat ``importance=8`` the durable attention mint used with a real
sender-class / finding-type / confidence / VIP score. The classifier reads the
compact ``communications.flags`` signal FEAT-001 captured at ingest (defaulting
to empty flags, so it is correct even when FEAT-001 has not run) plus the
sender address and — optionally — the subject line, and derives:

- a :class:`Classification` (sender class, no-reply / bulk / automated booleans,
  explainable reasons), and
- an importance (1..10) and a short honest reason string for the attention row.

Everything here is pure and deterministic: same inputs → same verdict. No ML,
no network, no body text — the classification is the *shape* of the sender
address, the envelope flags, and the finding metadata only (§7-safe).

This module also holds the shared address helpers :func:`_sender_domain` and
:func:`_local_part` so :mod:`backend.communications.detection.noise` and
:mod:`backend.ingest.mail_flags` import them from here (one home for the
address parsing, no import cycle).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

#: Sender classes (§7-safe, metadata only).
SenderClass = Literal["person", "machine", "bulk"]

#: The minimum importance a durable attention row must carry to appear on the
#: Today ``needs_you`` / ``coming_up`` surfaces. Rows below the floor stay in
#: the durable table (auditable) but are kept off Today.
NEEDS_YOU_MIN_IMPORTANCE = 4

#: A deadline further out than this many days is treated as a far-future
#: artifact (a mis-parsed/boilerplate date), not a real near-term obligation.
FAR_FUTURE_DAYS = 400

#: No-reply local-part tokens. Matched case-insensitively against the local
#: part with ``.`` and ``-`` separators stripped, so ``no-reply``/``no.reply``/
#: ``noreply`` all normalize to ``noreply`` and fire, and ``do-not-reply`` /
#: ``donotreply`` normalize to ``donotreply``.
_NO_REPLY_LOCAL_PARTS = frozenset(
    {
        "noreply",
        "donotreply",
        "mailerdaemon",
        "notifications",
        "notification",
        "alerts",
        "automated",
        "auto",
        "bounce",
        "postmaster",
        "root",
        "daemon",
    }
)

#: Known ESP (email service provider) registrable domains — a sender whose
#: registrable domain is one of these is bulk regardless of subdomain.
_ESP_REGISTRABLE_DOMAINS = frozenset(
    {
        "ccsend.com",
        "sendgrid.net",
        "sendgrid.com",
        "mailgun.com",
        "mailgun.org",
        "mcsv.net",
        "rsgsv.net",
        "sparkpostmail.com",
        "amazonses.com",
        "postmarkapp.com",
    }
)

#: Host-label fragments that mark a marketing/newsletter subdomain anywhere in
#: the host (``em.walmart.com`` → ``em``; ``news.emailmarket.shein.com`` →
#: ``news``). Matched as an exact *label* of the host, not a substring.
_MARKETING_SUBDOMAIN_LABELS = frozenset(
    {
        "mktg",
        "news",
        "em",
        "email",
        "newsletter",
        "marketing",
        "promo",
        "promos",
        "deals",
        "offers",
    }
)

#: Local-part tokens that mark a bulk/marketing sender by convention.
_BULK_LOCAL_PARTS = frozenset({"newsletters", "newsletter", "marketing"})

#: Importance tier bases per sender class.
_BASE_IMPORTANCE: dict[SenderClass, int] = {"person": 6, "machine": 2, "bulk": 1}

#: Finding kinds that add a point of importance (an ask/obligation).
_ACTIONABLE_KINDS = frozenset({"request", "deadline", "commitment"})


def _sender_domain(sender: str) -> str:
    """Return the lower-cased domain portion of a sender address.

    Works for ``Name <addr@host>`` and bare addresses. Returns the bare string
    when there is no ``@`` (e.g. a display name or phone handle).
    """
    s = str(sender or "").strip()
    if "<" in s and ">" in s:
        s = s.split("<", 1)[1].split(">", 1)[0]
    s = s.strip()
    if "@" in s:
        return s.split("@", 1)[1].lower()
    return s.lower()


def _local_part(sender: str) -> str:
    """Return the lower-cased local part of a sender address.

    Works for ``Name <addr@host>`` and bare addresses; returns ``""`` when no
    address is present (a display name / phone handle).
    """
    s = str(sender or "").strip()
    if "<" in s and ">" in s:
        s = s.split("<", 1)[1].split(">", 1)[0]
    s = s.strip()
    if "@" not in s:
        return ""
    return s.split("@", 1)[0].strip().lower()


def _normalize_local(local: str) -> str:
    """Strip ``.`` and ``-`` separators and lower-case for no-reply matching."""
    return local.replace(".", "").replace("-", "").lower()


def _registrable_domain(domain: str) -> str:
    """A best-effort registrable domain (last two labels) for ESP matching.

    ``email.ccsend.com`` → ``ccsend.com``. Deterministic and good enough for
    the fixed ESP allow-list (no public-suffix list needed).
    """
    labels = [p for p in domain.lower().split(".") if p]
    if len(labels) < 2:
        return domain.lower()
    return ".".join(labels[-2:])


def is_no_reply_sender(local: str, domain: str) -> bool:
    """Whether a ``(local, domain)`` pair is a no-reply/automated address.

    Exposed for FEAT-001's optional delegation hook (``mail_flags``) and used
    internally by :func:`classify_sender`. Matched against the normalized
    local part: any ``no-reply`` / ``do-not-reply`` variant and the fixed
    no-reply token set fire.
    """
    normalized = _normalize_local(local)
    if not normalized:
        return False
    if "noreply" in normalized or "donotreply" in normalized:
        return True
    return normalized in _NO_REPLY_LOCAL_PARTS


@dataclass(frozen=True)
class Classification:
    """The deterministic relevance verdict for one sender (metadata only)."""

    sender_class: SenderClass
    no_reply: bool
    bulk: bool
    automated: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _is_bulk_host(domain: str) -> str | None:
    """Return a bulk reason when the host is an ESP or marketing subdomain."""
    if not domain or "@" in domain:
        return None
    registrable = _registrable_domain(domain)
    if registrable in _ESP_REGISTRABLE_DOMAINS:
        return f"sender domain '{domain}' is a known bulk ESP ({registrable})"
    labels = {p for p in domain.lower().split(".") if p}
    marketing = labels & _MARKETING_SUBDOMAIN_LABELS
    if marketing:
        label = sorted(marketing)[0]
        return f"sender host carries a marketing label '{label}'"
    return None


def classify_sender(
    sender: str,
    *,
    flags: Mapping[str, object] | None = None,
    subject: str | None = None,
    is_vip: bool = False,
) -> Classification:
    """Classify a sender into person / machine / bulk with explainable reasons.

    ``flags`` is the compact FEAT-001 ``communications.flags`` dict
    (``{"bulk", "automated", "no_reply"}``); it defaults to empty so the
    classifier is correct even when FEAT-001 never ran. ``subject`` is accepted
    for signature stability (the subject is not needed by the current rules but
    keeps the call site uniform). ``is_vip`` is carried for symmetry with the
    scorer; it does not change the class.

    Precedence: bulk (strongest) → machine (no-reply) → person. A no-reply
    sender is ``machine`` unless it is also bulk.
    """
    flags = flags or {}
    reasons: list[str] = []

    local = _local_part(sender)
    domain = _sender_domain(sender)
    normalized = _normalize_local(local)

    flag_bulk = bool(flags.get("bulk"))
    flag_automated = bool(flags.get("automated"))
    flag_no_reply = bool(flags.get("no_reply"))

    no_reply = flag_no_reply or is_no_reply_sender(local, domain)
    if flag_no_reply:
        reasons.append("ingest flagged the sender no-reply")
    elif no_reply:
        reasons.append(f"sender local part '{local}' is a no-reply address")

    bulk = False
    if flag_bulk:
        bulk = True
        reasons.append("ingest flagged the sender bulk")
    if flag_automated and not flag_bulk:
        reasons.append("ingest flagged the sender automated")
    host_reason = _is_bulk_host(domain)
    if host_reason is not None:
        bulk = True
        reasons.append(host_reason)
    if normalized in _BULK_LOCAL_PARTS:
        bulk = True
        reasons.append(f"sender local part '{local}' is a bulk/marketing address")

    automated = bool(flag_automated or bulk or no_reply)

    if bulk:
        sender_class: SenderClass = "bulk"
    elif no_reply:
        sender_class = "machine"
    else:
        sender_class = "person"
        reasons.append("sender looks like a person")

    return Classification(
        sender_class=sender_class,
        no_reply=no_reply,
        bulk=bulk,
        automated=automated,
        reasons=tuple(reasons),
    )


def relevance_importance(
    kind: str,
    confidence: float,
    classification: Classification,
    *,
    is_vip: bool,
) -> int:
    """Map a finding onto a durable importance in ``1..10`` (tiered).

    Base per sender class (person 6 / machine 2 / bulk 1), ``+2`` for a VIP
    person, ``+1`` for an actionable finding kind (request/deadline/commitment),
    ``+1`` when the detector confidence is high (``>=0.9``). The result is
    clamped to ``1..10`` and then hard-capped by sender class — a machine can
    never exceed ``2`` and bulk can never exceed ``1`` — so an automated or
    marketing sender never climbs onto the Today surface no matter the finding.
    """
    sender_class = classification.sender_class
    importance = _BASE_IMPORTANCE[sender_class]
    if is_vip and sender_class == "person":
        importance += 2
    if kind in _ACTIONABLE_KINDS:
        importance += 1
    if confidence is not None and confidence >= 0.9:
        importance += 1

    importance = max(1, min(10, importance))

    if sender_class == "machine":
        importance = min(importance, 2)
    elif sender_class == "bulk":
        importance = min(importance, 1)
    return importance


def relevance_reason(
    kind: str,
    classification: Classification,
    *,
    is_vip: bool,
) -> str:
    """A short, honest, templated reason for the attention row body (§7-safe).

    Carries no body fragment — only the finding kind, the sender class, and the
    VIP/no-reply shape. Deterministic.
    """
    sender_class = classification.sender_class
    kind_label = kind if kind in _ACTIONABLE_KINDS else "signal"
    if sender_class == "bulk":
        return f"Bulk/marketing {kind_label} — down-ranked off your Today surface."
    if sender_class == "machine":
        return (
            f"Automated {kind_label} from a no-reply sender — informational, "
            "no reply expected."
        )
    if is_vip:
        return f"{kind_label.capitalize()} from a VIP contact."
    return f"{kind_label.capitalize()} from a person."


__all__ = [
    "Classification",
    "FAR_FUTURE_DAYS",
    "NEEDS_YOU_MIN_IMPORTANCE",
    "SenderClass",
    "classify_sender",
    "is_no_reply_sender",
    "relevance_importance",
    "relevance_reason",
]
