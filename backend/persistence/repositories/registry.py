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

from typing import Any

from .attention_repo import AttentionRepository
from .calendar_repo import CalendarRepository
from .communications_repo import CommunicationsRepository
from .commitments_repo import CommitmentsRepository
from .connections_repo import ConnectionsRepository, SecretCodec
from .import_repo import ImportRepository


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

    @property
    def session_factory(self) -> Any:
        """The underlying session factory (for workstreams needing raw access)."""
        return self._session_factory

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


__all__ = ["RepositoryRegistry"]
