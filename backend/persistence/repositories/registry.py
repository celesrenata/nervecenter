"""Repository registry — the single seam later workstreams use.

:class:`RepositoryRegistry` is constructed from a session factory (and, for
the secrets codec, an optional :class:`~.connections_repo.SecretCodec`) and
exposes each per-plane repository as a property. Workstreams B and C obtain
their repositories from here and must not edit app wiring beyond what F owns.

The registry holds no engine of its own; it is bound to the session factory
that the app wiring builds from the durable engine, so it is rebuilt fresh on
every startup and carries no cross-process state.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .attention_repo import AttentionRepository
from .bills_repo import BillsRepository
from .calendar_repo import CalendarRepository
from .communications_repo import CommunicationsRepository
from .commitments_repo import CommitmentsRepository
from .connections_repo import ConnectionsRepository, SecretCodec
from .import_repo import ImportRepository
from .relay_seen_repo import RelaySeenRepository
from .routines_repo import RoutinesRepository
from .wellness_repo import WellnessRepository


class RepositoryRegistry:
    """Single object later workstreams use to obtain repositories.

    Parameters
    ----------
    session_factory:
        An ``async_sessionmaker[AsyncSession]`` bound to the durable engine
        (see :func:`backend.persistence.connection.make_session_factory`).
    secret_codec:
        Optional :class:`SecretCodec` for the connections repository. When
        omitted, an identity codec is used (tests / no durable vault yet).
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        secret_codec: SecretCodec | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_codec = secret_codec
        #: Serializes the durable mirror writes made on this registry's
        #: session factory. The pipeline's commitment mirror and the follow-up
        #: service's follow-up / attention mirrors are separate background
        #: coroutines; on a single-connection engine (the SQLite
        #: ``StaticPool`` durable harness) their ``session.commit()`` calls
        #: must not interleave, so every mirror serializes on this lock.
        #: (Doubles of the registry without a lock run unwrapped.)
        self._durable_lock = asyncio.Lock()

    @property
    def session_factory(self) -> Any:
        """The underlying session factory (for workstreams needing raw access)."""
        return self._session_factory

    @property
    def durable_lock(self) -> asyncio.Lock:
        """The lock every durable mirror on this registry serializes on
        (extend-only: mirrors that do not know this registry never touch
        it; one durable plane = one lock)."""
        return self._durable_lock

    @property
    def secret_codec(self) -> Any:
        """The :class:`~.connections_repo.SecretCodec` this registry is bound
        to (``None`` when the registry degrades to the identity codec).

        Exposed so the control plane, the mail worker, and the calendar sync
        can verify they are all operating over the *same* codec — the core
        of the workstream F contract that one registry is the only durable
        secret boundary.
        """
        return self._secret_codec

    @property
    def connections(self) -> ConnectionsRepository:
        return ConnectionsRepository(
            self._session_factory, secret_codec=self._secret_codec
        )

    @property
    def communications(self) -> CommunicationsRepository:
        return CommunicationsRepository(self._session_factory)

    @property
    def commitments(self) -> CommitmentsRepository:
        return CommitmentsRepository(self._session_factory)

    @property
    def attention(self) -> AttentionRepository:
        return AttentionRepository(self._session_factory)

    @property
    def imports(self) -> ImportRepository:
        return ImportRepository(self._session_factory)

    @property
    def calendar(self) -> CalendarRepository:
        """Durable calendar accounts + events (workstream E)."""
        return CalendarRepository(self._session_factory)

    @property
    def bills(self) -> BillsRepository:
        """Durable bills + bill candidates (workstream G)."""
        return BillsRepository(self._session_factory)

    @property
    def routines(self) -> RoutinesRepository:
        """Durable routines + check-ins + sleep records (workstream H)."""
        return RoutinesRepository(self._session_factory)

    @property
    def wellness(self) -> WellnessRepository:
        """Durable wellness entries + preferences (workstream I)."""
        return WellnessRepository(self._session_factory)

    @property
    def relay_seen(self) -> RelaySeenRepository:
        """Durable per-device relay seen messages — the authoritative dedup gate (workstream H)."""
        return RelaySeenRepository(self._session_factory)


__all__ = ["RepositoryRegistry"]
