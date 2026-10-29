"""Relay durable-seen repository (Personal-Ops Recovery §6 — workstream H).

Backs the ``relay_seen_messages`` table — the **authoritative** cross-restart
dedup gate for the Apple Messages relay. The bounded in-memory ``relay_seq``
cursor (``ingest_cursors``) stays as a secondary hint; this table is what
survives a process restart and turns a re-posted ``(device_id, message_id)``
into a no-op.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..models.personal import RelaySeenMessageRow


class RelaySeenRepository:
    """Durable access to :class:`RelaySeenMessageRow` (per-device seen messages)."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    async def is_seen(
        self, connection_id: str, device_id: str, message_id: str
    ) -> bool:
        """True when ``(connection_id, device_id, message_id)`` was already ingested."""
        async with self._sessions() as session:
            stmt = select(RelaySeenMessageRow.id).where(
                RelaySeenMessageRow.connection_id == connection_id,
                RelaySeenMessageRow.device_id == device_id,
                RelaySeenMessageRow.message_id == message_id,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None

    async def mark_seen(
        self,
        connection_id: str,
        device_id: str,
        message_id: str,
        content_ref: str | None = None,
    ) -> RelaySeenMessageRow:
        """Record that a relay message was durably ingested (idempotent).

        The ``relay_seen_messages`` triple is unique, so re-marking the same
        message is a no-op. Call this *after* the communication write
        succeeds, mirroring the cursor's "advance after store" semantics.
        """
        async with self._sessions() as session:
            stmt = select(RelaySeenMessageRow).where(
                RelaySeenMessageRow.connection_id == connection_id,
                RelaySeenMessageRow.device_id == device_id,
                RelaySeenMessageRow.message_id == message_id,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is not None:
                return row
            row = RelaySeenMessageRow(
                connection_id=connection_id,
                device_id=device_id,
                message_id=message_id,
                content_ref=content_ref,
            )
            session.add(row)
            await session.commit()
            return row

    async def count_seen(self, connection_id: str, device_id: str) -> int:
        """How many distinct messages this device has durably ingested."""
        async with self._sessions() as session:
            stmt = select(RelaySeenMessageRow.id).where(
                RelaySeenMessageRow.connection_id == connection_id,
                RelaySeenMessageRow.device_id == device_id,
            )
            result = await session.execute(stmt)
            return len(result.all())


__all__ = ["RelaySeenRepository"]
