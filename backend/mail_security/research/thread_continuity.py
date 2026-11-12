"""Spike 8.2: conversation continuity / thread-hijack detection (offline research).

Question: can we tell a genuine reply in an existing conversation from a
message that only *claims* to continue it (spoofed ``References`` /
``In-Reply-To``, lookalike domain, changed ``Reply-To``, a compromised
sender who suddenly changes payment details)?

Ground rules (R-004, R-013, R-024):

* **Thread headers are attacker-controlled.** ``Message-ID``,
  ``In-Reply-To`` and ``References`` prove nothing by themselves; an
  attacker who saw one message of a thread can cite it. A message is
  ``CONTINUOUS`` only when (a) a cited parent exists in this account's own
  ledger of previously received mail, (b) its sender's registrable domain is
  already a participant of that thread, and (c) its authentication has not
  downgraded relative to what that participant had before.
* **Privacy.** The ledger keeps per-account HMAC fingerprints of message ids,
  registrable domains, an authentication flag and an opaque payee
  fingerprint. No subject, body, local part or address is stored.
* **Abstain, never bless.** Results are ``NOT_A_REPLY``, ``UNKNOWN``
  (ledger cannot answer), ``CONTINUOUS``, ``SUSPECT`` or ``BROKEN``. None of
  them means "safe".
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from backend.mail_security._domains import domain_of, is_confusable_domain
from backend.mail_security.inspections import _edit_distance_le_1, _registrable

_REPLY_SUBJECT = re.compile(r"^\s*(re|aw|sv|antw|fw|fwd|wg)\s*(\[\d+\])?\s*:", re.IGNORECASE)


class Status(StrEnum):
    NOT_A_REPLY = "NOT_A_REPLY"
    UNKNOWN = "UNKNOWN"
    CONTINUOUS = "CONTINUOUS"
    SUSPECT = "SUSPECT"
    BROKEN = "BROKEN"


class Signal(StrEnum):
    UNKNOWN_PARENT = "UNKNOWN_PARENT"
    REPLY_SUBJECT_WITHOUT_REFERENCES = "REPLY_SUBJECT_WITHOUT_REFERENCES"
    LOOKALIKE_OF_PARTICIPANT = "LOOKALIKE_OF_PARTICIPANT"
    NEW_PARTICIPANT_DOMAIN = "NEW_PARTICIPANT_DOMAIN"
    REPLY_TO_DIVERGES = "REPLY_TO_DIVERGES"
    AUTH_DOWNGRADE = "AUTH_DOWNGRADE"
    PAYMENT_DETAILS_CHANGED = "PAYMENT_DETAILS_CHANGED"


WEIGHTS: dict[Signal, int] = {
    Signal.UNKNOWN_PARENT: 1,
    Signal.REPLY_SUBJECT_WITHOUT_REFERENCES: 1,
    Signal.LOOKALIKE_OF_PARTICIPANT: 3,
    Signal.NEW_PARTICIPANT_DOMAIN: 1,
    Signal.REPLY_TO_DIVERGES: 2,
    Signal.AUTH_DOWNGRADE: 2,
    Signal.PAYMENT_DETAILS_CHANGED: 3,
}
BROKEN_AT = 3


def fingerprint(salt: bytes, message_id: str) -> str:
    """Per-account HMAC of a normalized Message-ID (never stored raw)."""
    norm = message_id.strip().strip("<>").lower()
    return hmac.new(salt, norm.encode(), hashlib.sha256).hexdigest()[:32]


@dataclass(frozen=True)
class Observed:
    """One message, reduced to the evidence this spike uses."""

    message_id: str
    from_addr: str
    subject: str = ""
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    reply_to: str | None = None
    #: True = independent PASS (DKIM/DMARC aligned), False = FAIL, None = unknown.
    auth_pass: bool | None = None
    #: Mailing-list or ARC-forwarded delivery (benign reasons for auth loss).
    forwarded: bool = False
    #: Opaque fingerprint of payment details found in the message, if any.
    payee_fp: str | None = None
    received_at: datetime | None = None


@dataclass
class _Entry:
    thread: str
    sender: str
    reply_to: str | None
    auth_pass: bool | None
    payee_fp: str | None


@dataclass
class Ledger:
    """Per-account memory of received mail (fingerprints + domains only)."""

    salt: bytes
    _by_fp: dict[str, _Entry] = field(default_factory=dict)
    _threads: dict[str, list[_Entry]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self._by_fp)

    def _parent_fps(self, msg: Observed) -> list[str]:
        ids = [*(msg.references or ()), *([msg.in_reply_to] if msg.in_reply_to else [])]
        return [fingerprint(self.salt, i) for i in ids if i]

    def add(self, msg: Observed) -> None:
        sender = _registrable(domain_of(msg.from_addr) or "")
        reply_to = _registrable(domain_of(msg.reply_to) or "") if msg.reply_to else None
        thread = None
        for fp in self._parent_fps(msg):
            if fp in self._by_fp:
                thread = self._by_fp[fp].thread
                break
        own = fingerprint(self.salt, msg.message_id)
        thread = thread or own
        entry = _Entry(thread, sender, reply_to, msg.auth_pass, msg.payee_fp)
        self._by_fp[own] = entry
        self._threads.setdefault(thread, []).append(entry)

    def thread_of(self, msg: Observed) -> list[_Entry] | None:
        for fp in self._parent_fps(msg):
            if fp in self._by_fp:
                return self._threads[self._by_fp[fp].thread]
        return None


@dataclass(frozen=True)
class Result:
    status: Status
    score: int
    signals: tuple[Signal, ...]


def _lookalike(domain: str, participants: Iterable[str]) -> bool:
    if is_confusable_domain(domain):
        return True
    label = domain.split(".")[0]
    return any(
        domain != p
        and (
            _edit_distance_le_1(label, p.split(".")[0])
            or label.replace("0", "o").replace("1", "l") == p.split(".")[0]
        )
        for p in participants
    )


def assess(msg: Observed, ledger: Ledger) -> Result:
    has_refs = bool(msg.in_reply_to or msg.references)
    reply_subject = bool(_REPLY_SUBJECT.match(msg.subject or ""))
    if not has_refs and not reply_subject:
        return Result(Status.NOT_A_REPLY, 0, ())
    signals: list[Signal] = []
    if reply_subject and not has_refs:
        signals.append(Signal.REPLY_SUBJECT_WITHOUT_REFERENCES)
    thread = ledger.thread_of(msg) if has_refs else None
    sender = _registrable(domain_of(msg.from_addr) or "")
    if thread is None:
        if has_refs:
            signals.append(Signal.UNKNOWN_PARENT)
        score = sum(WEIGHTS[s] for s in signals)
        # Without a known parent we cannot judge continuity: abstain unless
        # the evidence we do have is already strong.
        status = Status.UNKNOWN if score < BROKEN_AT else Status.BROKEN
        if len(ledger) == 0:
            status = Status.UNKNOWN
        return Result(status, score, tuple(signals))

    participants = {e.sender for e in thread}
    if sender not in participants:
        if _lookalike(sender, participants):
            signals.append(Signal.LOOKALIKE_OF_PARTICIPANT)
        else:
            signals.append(Signal.NEW_PARTICIPANT_DOMAIN)
    if msg.reply_to:
        rt = _registrable(domain_of(msg.reply_to) or "")
        known_rt = participants | {e.reply_to for e in thread if e.reply_to}
        if rt and rt != sender and rt not in known_rt:
            signals.append(Signal.REPLY_TO_DIVERGES)
    prior_pass = any(e.sender == sender and e.auth_pass is True for e in thread)
    if prior_pass and msg.auth_pass is not True and not msg.forwarded:
        signals.append(Signal.AUTH_DOWNGRADE)
    prior_payees = {e.payee_fp for e in thread if e.payee_fp}
    if msg.payee_fp and prior_payees and msg.payee_fp not in prior_payees:
        signals.append(Signal.PAYMENT_DETAILS_CHANGED)
    score = sum(WEIGHTS[s] for s in signals)
    if score >= BROKEN_AT:
        status = Status.BROKEN
    elif score:
        status = Status.SUSPECT
    else:
        status = Status.CONTINUOUS
    return Result(status, score, tuple(signals))


__all__ = [
    "BROKEN_AT",
    "WEIGHTS",
    "Ledger",
    "Observed",
    "Result",
    "Signal",
    "Status",
    "assess",
    "fingerprint",
]
