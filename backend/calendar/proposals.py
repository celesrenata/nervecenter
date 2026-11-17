"""In-memory proposal store for Ask calendar actions.

A proposal is a calendar event the Ask pipeline understood from a request and
is waiting for the user to confirm (``NERVE_ASK_CALENDAR_CONFIRM_FIRST``).
Proposals live 15 minutes, are bound to the signed-in person (``sub``) that
asked, and are never visible to anyone else (a wrong ``sub`` looks like an
expired one). The API runs as a single replica, so a process-local store is
enough; nothing here is durable and nothing here holds a secret.

The store also remembers events Ask created, so the undo route can delete only
what Ask itself wrote, only for the person who asked.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

PROPOSAL_TTL = timedelta(minutes=15)
STATE_KEY = "ask_proposals"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Proposal:
    """One pending calendar event. Times are tz-aware."""

    id: str
    sub: str
    title: str
    start: datetime
    end: datetime
    tz: str
    account_id: str
    all_day: bool = False
    location: str | None = None
    accounts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    expires_at: datetime = field(default_factory=_utcnow)
    #: Set after a successful confirm so a repeat confirm is idempotent.
    result: dict[str, Any] | None = None


@dataclass
class CreatedEvent:
    """An event Ask wrote to Google, kept so it can be undone."""

    event_id: str
    sub: str
    account_id: str
    event: Any  # ProviderEvent
    expires_at: datetime


class ProposalStore:
    def __init__(
        self,
        ttl: timedelta = PROPOSAL_TTL,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.RLock()
        self._proposals: dict[str, Proposal] = {}
        self._created: dict[str, CreatedEvent] = {}

    def now(self) -> datetime:
        return self._clock()

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, p in self._proposals.items() if p.expires_at <= now]:
            del self._proposals[key]
        for key in [k for k, c in self._created.items() if c.expires_at <= now]:
            del self._created[key]

    # -- proposals ---------------------------------------------------------
    def new_id(self) -> str:
        return f"prop-{uuid4().hex[:16]}"

    def put(self, proposal: Proposal) -> Proposal:
        with self._lock:
            self._purge()
            proposal.expires_at = self._clock() + self.ttl
            self._proposals[proposal.id] = proposal
            return proposal

    def get(self, proposal_id: str, sub: str) -> Proposal | None:
        """The proposal for this person, or ``None`` (unknown, expired, not theirs)."""
        with self._lock:
            self._purge()
            proposal = self._proposals.get(proposal_id)
            if proposal is None or not sub or proposal.sub != sub:
                return None
            return proposal

    def cancel(self, proposal_id: str, sub: str) -> bool:
        with self._lock:
            if self.get(proposal_id, sub) is None:
                return False
            del self._proposals[proposal_id]
            return True

    # -- created events (undo) ---------------------------------------------
    def record_created(
        self, event_id: str, sub: str, account_id: str, event: Any
    ) -> CreatedEvent:
        with self._lock:
            self._purge()
            created = CreatedEvent(
                event_id, sub, account_id, event, self._clock() + self.ttl
            )
            self._created[event_id] = created
            return created

    def get_created(self, event_id: str, sub: str) -> CreatedEvent | None:
        with self._lock:
            self._purge()
            created = self._created.get(event_id)
            if created is None or not sub or created.sub != sub:
                return None
            return created

    def forget_created(self, event_id: str) -> None:
        with self._lock:
            self._created.pop(event_id, None)


def get_proposal_store(state: Any) -> ProposalStore:
    """The process store, bound lazily onto ``app.state``."""
    store = getattr(state, STATE_KEY, None)
    if not isinstance(store, ProposalStore):
        store = ProposalStore()
        setattr(state, STATE_KEY, store)
    return store
