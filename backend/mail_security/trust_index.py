"""Privacy-scoped personal trust / relationship feature index (NerveShield task 5.4).

Library only; nothing in the pipeline calls it yet. It answers "is this sender
and thread familiar *for this account*?" so the expectedness check (R-013.2)
has history to compare against. It never answers "is this safe": a familiar
sender making an out-of-pattern request still needs independent verification
(R-013.4), and the features say so.

Why not the existing knowledge graph: ``backend.knowledge`` is a global entity
graph with no account namespace. Writing sender history there would merge
accounts (R-023.1) and put personal correspondence data into the general graph
(design zone P1). This index is a separate, account-scoped store. It can be
joined to the graph only through an opaque entity id the caller supplies.

Privacy:

* Every key is an HMAC-SHA256 digest under a per-account key derived from a
  caller-held master secret, so the same address in two accounts gives
  unrelated digests (no cross-account join, R-023.2).
* Stored per sender: digests, counts, first/last seen, thread digests and
  auth-alignment counts. No address, name, subject, body or Message-ID.
* Accounts must be explicitly allowed; revoked accounts are refused for both
  reads and writes, and their data can be deleted (R-023.3). Single senders
  can be forgotten.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import getaddresses
from enum import StrEnum
from typing import Any

STATE_VERSION = 1
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
_ADDR_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}$")
_MSGID_RE = re.compile(r"<([^<>\s]{1,998})>")
MAX_THREADS_PER_SENDER = 512
MAX_SENDERS_PER_ACCOUNT = 200_000


class Direction(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"  # the account owner wrote to this address


class HistoryLevel(StrEnum):
    NONE = "NONE"  # never seen
    SPARSE = "SPARSE"  # seen, but too little to rely on
    ESTABLISHED = "ESTABLISHED"


class TrustNote(StrEnum):
    """Log-safe reasons. Never a safety verdict."""

    NO_HISTORY = "NO_HISTORY"
    SPARSE_HISTORY = "SPARSE_HISTORY"
    SENDER_NEVER_ADDRESSED_BY_OWNER = "SENDER_NEVER_ADDRESSED_BY_OWNER"
    OWNER_HAS_WRITTEN_TO_SENDER = "OWNER_HAS_WRITTEN_TO_SENDER"
    DOMAIN_KNOWN_SENDER_NEW = "DOMAIN_KNOWN_SENDER_NEW"
    REPLY_TO_KNOWN_THREAD = "REPLY_TO_KNOWN_THREAD"
    REPLY_TO_UNKNOWN_THREAD = "REPLY_TO_UNKNOWN_THREAD"
    THREAD_KNOWN_SENDER_NEW_TO_THREAD = "THREAD_KNOWN_SENDER_NEW_TO_THREAD"
    AUTH_ALIGNED_HISTORY = "AUTH_ALIGNED_HISTORY"
    AUTH_NOT_ALIGNED_BEFORE = "AUTH_NOT_ALIGNED_BEFORE"
    DORMANT_SENDER = "DORMANT_SENDER"
    FAMILIARITY_IS_NOT_VERIFICATION = "FAMILIARITY_IS_NOT_VERIFICATION"
    INVALID_SENDER = "INVALID_SENDER"


class AccessDenied(Exception):
    """Account not allowed or revoked. Carries no account data."""

    def __init__(self) -> None:
        super().__init__("ACCOUNT_NOT_ALLOWED")


@dataclass(frozen=True)
class SenderObservation:
    """One message's relationship facts. Raw values are hashed on ingest."""

    sender: str
    direction: Direction
    when: datetime
    message_id: str | None = None
    in_reply_to: tuple[str, ...] = ()  # In-Reply-To + References ids
    auth_aligned: bool | None = None  # DMARC-aligned per trusted evidence, if known
    counterparts: tuple[str, ...] = ()  # for OUTBOUND: recipients written to


@dataclass(frozen=True)
class TrustPolicy:
    sparse_below: int = 3  # fewer messages than this -> SPARSE
    dormant_after_days: int = 365


@dataclass(frozen=True)
class TrustFeatures:
    """Expectedness inputs for one (account, sender, thread). Not a verdict."""

    history: HistoryLevel
    inbound_count: int
    outbound_count: int
    days_since_first_seen: int | None
    days_since_last_seen: int | None
    auth_aligned_count: int
    auth_unaligned_count: int
    reply_to_known_thread: bool | None  # None when the message is not a reply
    notes: frozenset[TrustNote]

    def to_codes(self) -> dict[str, object]:
        return {
            "history": self.history.value,
            "inbound_count": self.inbound_count,
            "outbound_count": self.outbound_count,
            "days_since_first_seen": self.days_since_first_seen,
            "days_since_last_seen": self.days_since_last_seen,
            "auth_aligned_count": self.auth_aligned_count,
            "auth_unaligned_count": self.auth_unaligned_count,
            "reply_to_known_thread": self.reply_to_known_thread,
            "notes": sorted(n.value for n in self.notes),
        }


@dataclass
class _Sender:
    domain: str
    inbound: int = 0
    outbound: int = 0
    first_seen: float | None = None
    last_seen: float | None = None
    auth_aligned: int = 0
    auth_unaligned: int = 0
    threads: list[str] = field(default_factory=list)


def normalize_address(value: str) -> str | None:
    """Lower-cased bare address from a header value, or None if not exactly one."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    pairs = [addr for _, addr in getaddresses([value]) if addr]
    if len(pairs) != 1:
        return None
    addr = pairs[0].strip().lower()
    return addr if _ADDR_RE.fullmatch(addr) else None


def message_ids(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(dict.fromkeys(m.lower() for m in _MSGID_RE.findall(value[:16384])))


class TrustIndex:
    def __init__(
        self,
        master_secret: bytes,
        *,
        allowed_accounts: Iterable[str] = (),
        policy: TrustPolicy | None = None,
    ) -> None:
        if not isinstance(master_secret, bytes) or len(master_secret) < 32:
            raise ValueError("master_secret must be at least 32 bytes")
        self._secret = master_secret
        self._policy = policy or TrustPolicy()
        self._allowed: set[str] = set()
        self._revoked: set[str] = set()
        self._senders: dict[str, dict[str, _Sender]] = {}
        self._domains: dict[str, dict[str, int]] = {}
        self._threads: dict[str, dict[str, set[str]]] = {}  # thread -> sender digests
        for account in allowed_accounts:
            self.allow(account)

    # -- access ----------------------------------------------------------------------

    def allow(self, account: str) -> None:
        if not isinstance(account, str) or not _ACCOUNT_RE.fullmatch(account):
            raise ValueError("invalid account scope")
        if account in self._revoked:
            raise AccessDenied
        self._allowed.add(account)

    def revoke(self, account: str, *, delete: bool = True) -> None:
        """Stop all processing for ``account``; delete its data unless told not to."""
        self._allowed.discard(account)
        self._revoked.add(account)
        if delete:
            self._drop(account)

    def _require(self, account: str) -> None:
        if account not in self._allowed or account in self._revoked:
            raise AccessDenied

    def _key(self, account: str) -> bytes:
        return hmac.new(
            self._secret, b"trust-index/v1/" + account.encode(), hashlib.sha256
        ).digest()

    def _digest(self, account: str, kind: str, value: str) -> str:
        return hmac.new(
            self._key(account), f"{kind}\x1f{value}".encode(), hashlib.sha256
        ).hexdigest()

    # -- write -----------------------------------------------------------------------

    def observe(self, account: str, obs: SenderObservation) -> bool:
        """Record one observation. Returns False (and stores nothing) if invalid."""
        self._require(account)
        when = obs.when.astimezone(UTC).timestamp() if obs.when.tzinfo else None
        if when is None:
            return False
        addresses = [obs.sender] if obs.direction is Direction.INBOUND else list(obs.counterparts)
        stored = False
        for raw in addresses:
            addr = normalize_address(raw)
            if addr is None:
                continue
            senders = self._senders.setdefault(account, {})
            sdig = self._digest(account, "addr", addr)
            entry = senders.get(sdig)
            if entry is None:
                if len(senders) >= MAX_SENDERS_PER_ACCOUNT:
                    continue
                entry = _Sender(domain=self._digest(account, "domain", addr.rsplit("@", 1)[1]))
                senders[sdig] = entry
                domains = self._domains.setdefault(account, {})
                domains[entry.domain] = domains.get(entry.domain, 0) + 1
            if obs.direction is Direction.INBOUND:
                entry.inbound += 1
                if obs.auth_aligned is True:
                    entry.auth_aligned += 1
                elif obs.auth_aligned is False:
                    entry.auth_unaligned += 1
            else:
                entry.outbound += 1
            entry.first_seen = when if entry.first_seen is None else min(entry.first_seen, when)
            entry.last_seen = when if entry.last_seen is None else max(entry.last_seen, when)
            threads = self._threads.setdefault(account, {})
            for mid in (obs.message_id, *obs.in_reply_to):
                ids = message_ids(f"<{mid.strip('<>')}>") if mid else ()
                for tid in ids:
                    tdig = self._digest(account, "msgid", tid)
                    threads.setdefault(tdig, set()).add(sdig)
                    if tdig not in entry.threads and len(entry.threads) < MAX_THREADS_PER_SENDER:
                        entry.threads.append(tdig)
            stored = True
        return stored

    # -- read ------------------------------------------------------------------------

    def features(
        self,
        account: str,
        sender: str,
        *,
        now: datetime,
        in_reply_to: tuple[str, ...] = (),
    ) -> TrustFeatures:
        self._require(account)
        notes = {TrustNote.FAMILIARITY_IS_NOT_VERIFICATION}
        addr = normalize_address(sender)
        reply_ids = [i for raw in in_reply_to for i in message_ids(f"<{raw.strip('<>')}>")]
        if addr is None:
            notes |= {TrustNote.INVALID_SENDER, TrustNote.NO_HISTORY}
            return TrustFeatures(HistoryLevel.NONE, 0, 0, None, None, 0, 0, None, frozenset(notes))
        sdig = self._digest(account, "addr", addr)
        entry = self._senders.get(account, {}).get(sdig)
        threads = self._threads.get(account, {})
        reply_known: bool | None = None
        if reply_ids:
            tdigs = [self._digest(account, "msgid", t) for t in reply_ids]
            known = [t for t in tdigs if t in threads]
            reply_known = bool(known)
            notes.add(
                TrustNote.REPLY_TO_KNOWN_THREAD if known else TrustNote.REPLY_TO_UNKNOWN_THREAD
            )
            if known and not any(sdig in threads[t] for t in known):
                notes.add(TrustNote.THREAD_KNOWN_SENDER_NEW_TO_THREAD)
        if entry is None:
            notes.add(TrustNote.NO_HISTORY)
            ddig = self._digest(account, "domain", addr.rsplit("@", 1)[1])
            if self._domains.get(account, {}).get(ddig):
                notes.add(TrustNote.DOMAIN_KNOWN_SENDER_NEW)
            return TrustFeatures(
                HistoryLevel.NONE, 0, 0, None, None, 0, 0, reply_known, frozenset(notes)
            )
        total = entry.inbound + entry.outbound
        level = (
            HistoryLevel.ESTABLISHED if total >= self._policy.sparse_below else HistoryLevel.SPARSE
        )
        if level is HistoryLevel.SPARSE:
            notes.add(TrustNote.SPARSE_HISTORY)
        notes.add(
            TrustNote.OWNER_HAS_WRITTEN_TO_SENDER
            if entry.outbound
            else TrustNote.SENDER_NEVER_ADDRESSED_BY_OWNER
        )
        if entry.auth_aligned:
            notes.add(TrustNote.AUTH_ALIGNED_HISTORY)
        if entry.auth_unaligned:
            notes.add(TrustNote.AUTH_NOT_ALIGNED_BEFORE)
        now_ts = now.astimezone(UTC).timestamp()
        first = _days(now_ts, entry.first_seen)
        last = _days(now_ts, entry.last_seen)
        if last is not None and last >= self._policy.dormant_after_days:
            notes.add(TrustNote.DORMANT_SENDER)
        return TrustFeatures(
            level,
            entry.inbound,
            entry.outbound,
            first,
            last,
            entry.auth_aligned,
            entry.auth_unaligned,
            reply_known,
            frozenset(notes),
        )

    # -- deletion --------------------------------------------------------------------

    def forget_sender(self, account: str, sender: str) -> bool:
        self._require(account)
        addr = normalize_address(sender)
        if addr is None:
            return False
        sdig = self._digest(account, "addr", addr)
        entry = self._senders.get(account, {}).pop(sdig, None)
        if entry is None:
            return False
        domains = self._domains.get(account, {})
        domains[entry.domain] = domains.get(entry.domain, 1) - 1
        if domains[entry.domain] <= 0:
            domains.pop(entry.domain)
        threads = self._threads.get(account, {})
        for tdig in list(threads):
            threads[tdig].discard(sdig)
            if not threads[tdig]:
                del threads[tdig]
        return True

    def delete_account(self, account: str) -> None:
        """Remove every derived record for ``account`` (access unchanged)."""
        self._drop(account)

    def _drop(self, account: str) -> None:
        self._senders.pop(account, None)
        self._domains.pop(account, None)
        self._threads.pop(account, None)

    # -- persistence -----------------------------------------------------------------

    def export_state(self) -> dict[str, Any]:
        """Digest-only snapshot for a protected store (zone P1)."""
        return {
            "version": STATE_VERSION,
            "accounts": {
                account: {
                    "senders": {
                        sdig: {
                            "domain": e.domain,
                            "inbound": e.inbound,
                            "outbound": e.outbound,
                            "first_seen": e.first_seen,
                            "last_seen": e.last_seen,
                            "auth_aligned": e.auth_aligned,
                            "auth_unaligned": e.auth_unaligned,
                            "threads": list(e.threads),
                        }
                        for sdig, e in sorted(senders.items())
                    },
                }
                for account, senders in sorted(self._senders.items())
            },
            "revoked": sorted(self._revoked),
        }

    def import_state(self, state: dict[str, Any]) -> None:
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            raise ValueError("unsupported trust index state")
        self._revoked |= set(state.get("revoked", []))
        for account, data in state["accounts"].items():
            if not _ACCOUNT_RE.fullmatch(account) or account in self._revoked:
                continue
            senders: dict[str, _Sender] = {}
            domains: dict[str, int] = {}
            threads: dict[str, set[str]] = {}
            for sdig, raw in data["senders"].items():
                e = _Sender(
                    domain=str(raw["domain"]),
                    inbound=int(raw["inbound"]),
                    outbound=int(raw["outbound"]),
                    first_seen=raw["first_seen"],
                    last_seen=raw["last_seen"],
                    auth_aligned=int(raw["auth_aligned"]),
                    auth_unaligned=int(raw["auth_unaligned"]),
                    threads=[str(t) for t in raw["threads"]][:MAX_THREADS_PER_SENDER],
                )
                senders[sdig] = e
                domains[e.domain] = domains.get(e.domain, 0) + 1
                for t in e.threads:
                    threads.setdefault(t, set()).add(sdig)
            self._senders[account] = senders
            self._domains[account] = domains
            self._threads[account] = threads


def _days(now: float, then: float | None) -> int | None:
    return None if then is None else max(0, int((now - then) // 86400))


__all__ = [
    "AccessDenied",
    "Direction",
    "HistoryLevel",
    "SenderObservation",
    "TrustFeatures",
    "TrustIndex",
    "TrustNote",
    "TrustPolicy",
    "message_ids",
    "normalize_address",
]
