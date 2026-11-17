"""In-memory store for Ask calendar-deletion proposals.

Same rules as :mod:`backend.calendar.proposals`: 15 minute life, bound to the
signed-in person (``sub``), a wrong ``sub`` looks like an expired proposal,
process-local, no secrets. The provider events needed to act are kept here,
server side; the browser only ever sends row ids back.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

PROPOSAL_TTL = timedelta(minutes=15)
STATE_KEY = "ask_delete_proposals"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class DeletionProposal:
    id: str
    sub: str
    person: str
    scope: str  # all_future | next | date | ambiguous
    #: view rows (JSON-safe); ``selected`` is the default checkbox state
    rows: list[dict[str, Any]] = field(default_factory=list)
    #: row id -> provider event (the series master or the single event)
    events: dict[str, Any] = field(default_factory=dict)
    cutoff: datetime = field(default_factory=_utcnow)
    expires_at: datetime = field(default_factory=_utcnow)
    result: dict[str, Any] | None = None


class DeletionStore:
    def __init__(
        self, ttl: timedelta = PROPOSAL_TTL, clock: Callable[[], datetime] = _utcnow
    ) -> None:
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.RLock()
        self._items: dict[str, DeletionProposal] = {}

    def now(self) -> datetime:
        return self._clock()

    def new_id(self) -> str:
        return f"del-{uuid4().hex[:16]}"

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, p in self._items.items() if p.expires_at <= now]:
            del self._items[key]

    def put(self, proposal: DeletionProposal) -> DeletionProposal:
        with self._lock:
            self._purge()
            proposal.expires_at = self._clock() + self.ttl
            self._items[proposal.id] = proposal
            return proposal

    def get(self, proposal_id: str, sub: str) -> DeletionProposal | None:
        with self._lock:
            self._purge()
            proposal = self._items.get(proposal_id)
            if proposal is None or not sub or proposal.sub != sub:
                return None
            return proposal

    def cancel(self, proposal_id: str, sub: str) -> bool:
        with self._lock:
            if self.get(proposal_id, sub) is None:
                return False
            del self._items[proposal_id]
            return True


def get_delete_store(state: Any) -> DeletionStore:
    store = getattr(state, STATE_KEY, None)
    if not isinstance(store, DeletionStore):
        store = DeletionStore()
        setattr(state, STATE_KEY, store)
    return store
