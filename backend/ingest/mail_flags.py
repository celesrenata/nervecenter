"""Deterministic bulk/automated mail classification flags (attention-relevance
FEAT-001 — the signal source FEAT-002's relevance scoring consumes).

At ingest the mail transports now carry four envelope/transport classification
headers — ``List-Unsubscribe``, ``List-Id``, ``Precedence``,
``Auto-Submitted`` — alongside the minimal RFC-822 set. This module derives a
compact, **non-secret** classification from those headers plus the sender
address so the detection/scoring layer reads a precomputed signal instead of
re-parsing raw headers.

§7 secret-free: :func:`classify_mail_flags` returns ONLY booleans and never
returns or logs a raw header value or any body text — the classification is
the *presence/shape* of envelope headers and the sender address pattern, never
their stored values. The function is pure and deterministic: same inputs →
same flags.

FEAT-001 stands alone — it carries a minimal local no-reply/bulk check. When
the FEAT-002 relevance classifier is present it is delegated the sender pattern
(behind a best-effort import so this module never hard-depends on it).
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = ["classify_mail_flags"]

#: ``Precedence`` header values that mark a bulk/automated (non-personal) send.
_BULK_PRECEDENCE = frozenset({"bulk", "list", "junk"})

#: Sender local-part tokens that mark a do-not-reply automated address. Matched
#: as a substring of the local part (so ``no-reply``/``noreply``/``donotreply``
#: all fire); kept minimal and deterministic.
_NO_REPLY_LOCAL_MARKERS = ("noreply", "no-reply", "no_reply", "donotreply", "do-not-reply")

#: Sender local-part tokens that mark an automated/bulk sender by convention.
_BULK_LOCAL_MARKERS = ("mailer-daemon", "bounce", "bounces", "notifications", "notification")


def _split_sender(sender: str) -> tuple[str, str]:
    """Return the lower-cased ``(local_part, domain)`` of a sender address.

    Handles both ``Name <addr@host>`` and a bare ``addr@host``; returns empty
    strings when no address is present (a display name / phone handle). Never
    returns the raw header value — only its already-split, lower-cased parts.
    """
    s = str(sender or "").strip()
    if "<" in s and ">" in s:
        s = s.split("<", 1)[1].split(">", 1)[0].strip()
    if "@" not in s:
        return "", ""
    local, _sep, domain = s.partition("@")
    return local.strip().lower(), domain.strip().lower()


def _local_no_reply(local: str) -> bool:
    """Minimal local-part no-reply check (FEAT-001 stands-alone fallback)."""
    return any(marker in local for marker in _NO_REPLY_LOCAL_MARKERS)


def _delegated_no_reply(local: str, domain: str) -> bool | None:
    """Delegate the sender no-reply pattern to the FEAT-002 relevance
    classifier when it is present; ``None`` when it is not wired yet.

    Kept behind a best-effort import so FEAT-001 never hard-depends on
    FEAT-002 (the two features are independently ordered). Any failure or a
    missing module falls through to the local check.
    """
    try:
        from backend.communications.detection import relevance as _relevance  # type: ignore
    except Exception:  # noqa: BLE001 - FEAT-002 not present yet / optional
        return None
    checker = getattr(_relevance, "is_no_reply_sender", None)
    if checker is None:
        return None
    try:
        return bool(checker(local, domain))
    except Exception:  # noqa: BLE001 - never let a delegate break ingest
        return None


def classify_mail_flags(headers: Mapping[str, str], sender: str) -> dict:
    """Derive the compact bulk/automated classification flags for one message.

    Returns ONLY the three booleans ``{"bulk", "automated", "no_reply"}``
    derived from the four classification headers and the sender pattern:

    - ``bulk`` — a ``List-Unsubscribe`` or ``List-Id`` header is present, or
      ``Precedence`` is one of ``bulk``/``list``/``junk``, or the sender
      local part is a known bulk/notification address.
    - ``automated`` — ``Auto-Submitted`` starts with ``auto-`` (anything but
      the RFC 3834 ``no`` value), or the message is bulk.
    - ``no_reply`` — the sender local part is a do-not-reply address (delegated
      to the FEAT-002 relevance classifier when present, else the local check).

    §7: never returns or logs a raw header value or body text — only the
    booleans above. Pure and deterministic.
    """
    # Case-insensitive header lookup over the (small) allow-listed set.
    lower = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    list_unsubscribe = "list-unsubscribe" in lower and bool(lower["list-unsubscribe"].strip())
    list_id = "list-id" in lower and bool(lower["list-id"].strip())
    precedence = lower.get("precedence", "").strip().lower()
    auto_submitted = lower.get("auto-submitted", "").strip().lower()

    local, domain = _split_sender(sender)

    no_reply = _delegated_no_reply(local, domain)
    if no_reply is None:
        no_reply = _local_no_reply(local)

    bulk = (
        list_unsubscribe
        or list_id
        or precedence in _BULK_PRECEDENCE
        or any(marker in local for marker in _BULK_LOCAL_MARKERS)
    )
    automated = bulk or auto_submitted.startswith("auto-")

    return {"bulk": bool(bulk), "automated": bool(automated), "no_reply": bool(no_reply)}
