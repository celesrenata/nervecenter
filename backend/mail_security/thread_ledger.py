"""Conversation continuity / thread-hijack signal, SHADOW only (NS-ADR-067, G8 item 2).

Runtime port of the Phase 8 spike 8.2 logic (``research/thread_continuity.py``
is research code and must never be imported by the runtime). It answers one
question for an INBOUND message that claims to continue a conversation: does
the claim hold against this account's own memory of that conversation?

Ground rules:

* **Thread headers are attacker-controlled.** ``Message-ID`` /
  ``In-Reply-To`` / ``References`` prove nothing by themselves. A message is
  ``CONTINUOUS`` only when a cited parent is in this account's own ledger and
  its sender's registrable domain already participates in that thread.
* **The owner's sent mail is the trusted side of a thread.** Outgoing mail
  (Gmail SENT) is recorded with its recipients' domains as participants, so a
  reply to something the owner sent can be judged at all.
* **Privacy.** The ledger stores per-account HMAC fingerprints of message ids
  and registrable domains only: no subject, body, local part or address. The
  HMAC key (``NERVE_MAIL_SECURITY_LEDGER_KEY``) is separate from the
  object-store key; without it the ledger is inert.
* **Abstain, never bless. SHADOW only.** Statuses are ``NOT_A_REPLY``,
  ``UNKNOWN``, ``CONTINUOUS``, ``SUSPECT`` and ``BROKEN``. None means "safe";
  ``SUSPECT``/``BROKEN`` only add a review-queue reason and never change a
  disposition, a gate decision or an attention item.

Not ported (no input exists yet): ``AUTH_DOWNGRADE`` (the trusted-receiver
boundary is empty, so no message has independent authentication) and
``PAYMENT_DETAILS_CHANGED`` (no payee extraction).
"""
from __future__ import annotations

import base64
import binascii
import email.utils
import hashlib
import hmac
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from backend.mail_security._domains import domain_of, is_confusable_domain
from backend.mail_security.inspections import _edit_distance_le_1, _registrable

ENV_LEDGER_KEY = "NERVE_MAIL_SECURITY_LEDGER_KEY"
LEDGER_POLICY_VERSION = "mail-thread-ledger@1"

#: Bounds on attacker-controlled header input.
MAX_REFERENCES = 50
MAX_PARTICIPANTS = 50

_REPLY_SUBJECT = re.compile(r"^\s*(re|aw|sv|antw|fw|fwd|wg)\s*(\[\d+\])?\s*:", re.IGNORECASE)
_MSGID = re.compile(r"<[^<>\s]{1,998}>")


class Status(StrEnum):
    NOT_A_REPLY = "NOT_A_REPLY"
    UNKNOWN = "UNKNOWN"
    CONTINUOUS = "CONTINUOUS"
    SUSPECT = "SUSPECT"
    BROKEN = "BROKEN"


#: Statuses that surface as a SHADOW review-queue reason.
FLAGGED = frozenset({Status.SUSPECT, Status.BROKEN})


class Signal(StrEnum):
    UNKNOWN_PARENT = "UNKNOWN_PARENT"
    REPLY_SUBJECT_WITHOUT_REFERENCES = "REPLY_SUBJECT_WITHOUT_REFERENCES"
    LOOKALIKE_OF_PARTICIPANT = "LOOKALIKE_OF_PARTICIPANT"
    NEW_PARTICIPANT_DOMAIN = "NEW_PARTICIPANT_DOMAIN"
    REPLY_TO_DIVERGES = "REPLY_TO_DIVERGES"


WEIGHTS: dict[Signal, int] = {
    Signal.UNKNOWN_PARENT: 1,
    Signal.REPLY_SUBJECT_WITHOUT_REFERENCES: 1,
    Signal.LOOKALIKE_OF_PARTICIPANT: 3,
    Signal.NEW_PARTICIPANT_DOMAIN: 1,
    Signal.REPLY_TO_DIVERGES: 2,
}
BROKEN_AT = 3


# -- key + fingerprints ---------------------------------------------------------


def ledger_key_from_env(env: Mapping[str, str] | None = None) -> bytes | None:
    """The 32-byte HMAC key, or ``None`` (inert) when unset or malformed."""
    raw = ((env if env is not None else os.environ).get(ENV_LEDGER_KEY) or "").strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    return key if len(key) == 32 else None


def account_salt(key: bytes, account_scope: str) -> bytes:
    """Per-account salt: fingerprints never correlate across accounts."""
    return hmac.new(key, f"ledger-salt:{account_scope}".encode(), hashlib.sha256).digest()


def fingerprint(salt: bytes, message_id: str) -> str:
    """HMAC of a normalized Message-ID (never stored raw)."""
    norm = message_id.strip().strip("<>").strip().lower()
    return hmac.new(salt, norm.encode(), hashlib.sha256).hexdigest()[:32]


# -- observed message -------------------------------------------------------------


def _domain(addr: str | None) -> str | None:
    if not addr:
        return None
    d = _registrable(domain_of(addr) or "")
    return d or None


def _ids(value: str | None) -> list[str]:
    found = _MSGID.findall(str(value or ""))
    if not found and value and value.strip():
        found = [value.strip()]
    return found[:MAX_REFERENCES]


@dataclass(frozen=True)
class Observed:
    """One message reduced to the envelope evidence the ledger uses."""

    message_id: str
    outgoing: bool
    sender_domain: str | None
    reply_to_domain: str | None
    participant_domains: tuple[str, ...]
    parent_ids: tuple[str, ...]
    reply_subject: bool

    @property
    def has_refs(self) -> bool:
        return bool(self.parent_ids)


def observe(headers: Mapping[str, str], *, subject: str, outgoing: bool) -> Observed | None:
    """Reduce headers to :class:`Observed`; ``None`` without a Message-ID."""
    mids = _ids(headers.get("Message-ID"))
    if not mids:
        return None
    _, from_addr = email.utils.parseaddr(str(headers.get("From") or ""))
    _, reply_to = email.utils.parseaddr(str(headers.get("Reply-To") or ""))
    parents = [*_ids(headers.get("References")), *_ids(headers.get("In-Reply-To"))]
    participants: list[str] = []
    if outgoing:
        raw = [str(headers.get("To") or ""), str(headers.get("Cc") or "")]
        for _name, addr in email.utils.getaddresses([r for r in raw if r]):
            d = _domain(addr)
            if d and d not in participants:
                participants.append(d)
    return Observed(
        message_id=mids[0],
        outgoing=outgoing,
        sender_domain=_domain(from_addr),
        reply_to_domain=_domain(reply_to),
        participant_domains=tuple(participants[:MAX_PARTICIPANTS]),
        parent_ids=tuple(dict.fromkeys(parents))[:MAX_REFERENCES],
        reply_subject=bool(_REPLY_SUBJECT.match(subject or "")),
    )


# -- assessment ---------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """A ledger row as the assessment sees it (domains only)."""

    thread_fp: str
    outgoing: bool
    sender_domain: str | None
    reply_to_domain: str | None
    participant_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    status: Status
    score: int
    signals: tuple[Signal, ...]


def _lookalike(domain: str, participants: Iterable[str]) -> bool:
    if is_confusable_domain(domain):
        return True
    label = domain.split(".")[0]
    for p in participants:
        p_label = p.split(".")[0]
        if domain != p and (
            _edit_distance_le_1(label, p_label)
            or label.replace("0", "o").replace("1", "l") == p_label
        ):
            return True
    return False


def thread_participants(thread: Iterable[Entry]) -> set[str]:
    """Senders of every entry plus the recipients of the owner's own mail."""
    out: set[str] = set()
    for e in thread:
        if e.sender_domain:
            out.add(e.sender_domain)
        if e.outgoing:
            out.update(e.participant_domains)
    return out


def assess(msg: Observed, thread: list[Entry] | None, *, ledger_empty: bool) -> Result:
    """Judge an INBOUND message against its known thread (``None`` = no known parent)."""
    if not msg.has_refs and not msg.reply_subject:
        return Result(Status.NOT_A_REPLY, 0, ())
    signals: list[Signal] = []
    if msg.reply_subject and not msg.has_refs:
        signals.append(Signal.REPLY_SUBJECT_WITHOUT_REFERENCES)
    if thread is None:
        if msg.has_refs:
            signals.append(Signal.UNKNOWN_PARENT)
        score = sum(WEIGHTS[s] for s in signals)
        # Without a known parent continuity cannot be judged: abstain.
        status = Status.UNKNOWN if score < BROKEN_AT or ledger_empty else Status.BROKEN
        return Result(status, score, tuple(signals))

    participants = thread_participants(thread)
    sender = msg.sender_domain or ""
    if sender not in participants:
        if sender and _lookalike(sender, participants):
            signals.append(Signal.LOOKALIKE_OF_PARTICIPANT)
        else:
            signals.append(Signal.NEW_PARTICIPANT_DOMAIN)
    if msg.reply_to_domain:
        known = participants | {e.reply_to_domain for e in thread if e.reply_to_domain}
        if msg.reply_to_domain != sender and msg.reply_to_domain not in known:
            signals.append(Signal.REPLY_TO_DIVERGES)
    score = sum(WEIGHTS[s] for s in signals)
    if score >= BROKEN_AT:
        status = Status.BROKEN
    elif score:
        status = Status.SUSPECT
    else:
        status = Status.CONTINUOUS
    return Result(status, score, tuple(signals))


# -- process-wide binding (mirrors ingestion_seam.set_mail_security_repository) -------

_ledger_repository: Any = None


def set_thread_ledger_repository(repository: Any) -> None:
    global _ledger_repository
    _ledger_repository = repository


def get_thread_ledger_repository() -> Any:
    return _ledger_repository


__all__ = [
    "BROKEN_AT",
    "ENV_LEDGER_KEY",
    "FLAGGED",
    "LEDGER_POLICY_VERSION",
    "WEIGHTS",
    "Entry",
    "Observed",
    "Result",
    "Signal",
    "Status",
    "account_salt",
    "assess",
    "fingerprint",
    "get_thread_ledger_repository",
    "ledger_key_from_env",
    "observe",
    "set_thread_ledger_repository",
    "thread_participants",
]
