"""Communication-plane repository: people, identities, communications,
conversations.

Raw content stays behind ``content_ref`` (§7); the durable rows carry only the
ref, the sensitivity label, and a JSONB flag bag.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from ..models.personal import (
    CommunicationRow,
    ConversationRow,
    PersonIdentityRow,
    PersonRow,
)


def _to_dict(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


class CommunicationsRepository:
    """Durable access to the communication-plane rows."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    # -- people -------------------------------------------------------------

    async def create_person(
        self,
        *,
        id: str | None = None,
        display_name: str,
        vip: bool = False,
        default_sensitivity: str | None = None,
        notes: dict[str, Any] | None = None,
        boundaries: dict[str, Any] | None = None,
    ) -> PersonRow:
        """Insert a new person row.

        ``id`` pins the row to an existing canonical person id (in-memory
        promotion); when omitted a fresh UUID is generated on the ORM side.
        """
        async with self._sessions() as session:
            row = PersonRow(
                id=id,
                display_name=display_name,
                vip=vip,
                default_sensitivity=default_sensitivity,
                notes=notes or {},
                boundaries=boundaries or {},
            )
            session.add(row)
            await session.commit()
            return row

    async def get_person(self, person_id: str) -> PersonRow | None:
        async with self._sessions() as session:
            return await session.get(PersonRow, person_id)

    async def list_people(self) -> list[PersonRow]:
        async with self._sessions() as session:
            stmt = select(PersonRow).order_by(PersonRow.display_name)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def upsert_person_identity(
        self,
        *,
        person_id: str,
        channel: str,
        handle: str,
        resolved: bool = False,
    ) -> PersonIdentityRow:
        """Upsert a channel identity; keyed by (person_id, channel, handle)."""

        async with self._sessions() as session:
            stmt = select(PersonIdentityRow).where(
                PersonIdentityRow.person_id == person_id,
                PersonIdentityRow.channel == channel,
                PersonIdentityRow.handle == handle,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                row = PersonIdentityRow(
                    person_id=person_id,
                    channel=channel,
                    handle=handle,
                    resolved=resolved,
                )
                session.add(row)
            else:
                row.resolved = resolved
            await session.commit()
            return row

    async def list_identities(
        self, person_id: str
    ) -> list[PersonIdentityRow]:
        """All channel identities for one person, by channel then handle."""

        async with self._sessions() as session:
            stmt = (
                select(PersonIdentityRow)
                .where(PersonIdentityRow.person_id == person_id)
                .order_by(
                    PersonIdentityRow.channel, PersonIdentityRow.handle
                )
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # -- communications -----------------------------------------------------

    async def create_communication(
        self,
        *,
        person_id: str,
        channel: str,
        direction: str,
        content_ref: str,
        subject: str | None = None,
        sensitivity: str | None = None,
        received_at: datetime | None = None,
        flags: dict[str, Any] | None = None,
    ) -> CommunicationRow:
        async with self._sessions() as session:
            row = CommunicationRow(
                person_id=person_id,
                channel=channel,
                direction=direction,
                content_ref=content_ref,
                subject=subject,
                sensitivity=sensitivity,
                received_at=received_at or datetime.now(),
                flags=flags or {},
            )
            session.add(row)
            await session.commit()
            return row

    async def list_communications(
        self,
        *,
        person_id: str | None = None,
        limit: int = 200,
    ) -> list[CommunicationRow]:
        async with self._sessions() as session:
            stmt = select(CommunicationRow).order_by(
                CommunicationRow.received_at.desc()
            )
            if person_id is not None:
                stmt = stmt.where(CommunicationRow.person_id == person_id)
            stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_communications_by_ids(
        self, ids: list[str]
    ) -> list[CommunicationRow]:
        """Communications by id (used to classify commitment source
        direction — promises made key off an *outgoing* source)."""
        if not ids:
            return []
        async with self._sessions() as session:
            stmt = select(CommunicationRow).where(
                CommunicationRow.id.in_(ids)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # -- conversations ------------------------------------------------------

    async def upsert_conversation(
        self,
        *,
        person_id: str,
        channel: str,
        last_message_at: datetime | None = None,
        unread_count: int = 0,
    ) -> ConversationRow:
        """Upsert a conversation; keyed by (person_id, channel)."""

        async with self._sessions() as session:
            stmt = select(ConversationRow).where(
                ConversationRow.person_id == person_id,
                ConversationRow.channel == channel,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                row = ConversationRow(
                    person_id=person_id,
                    channel=channel,
                    last_message_at=last_message_at,
                    unread_count=unread_count,
                )
                session.add(row)
            else:
                row.last_message_at = (
                    last_message_at or row.last_message_at
                )
                row.unread_count = unread_count
            await session.commit()
            return row

    @staticmethod
    def to_dict(row: Any) -> dict[str, Any]:
        return _to_dict(row)


__all__ = ["CommunicationsRepository"]
