"""Commitment-plane repository: commitments and followups.

Commitments are the durable "I promised this person I'd do X" records (F+J);
followups are lighter reminders that may or may not be tied to a person.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from ..models.personal import CommitmentRow, FollowupRow

#: Commitment / followup states (frozen by spec §6).
VALID_STATES = ("open", "done", "dropped", "expired")


def _to_dict(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


class CommitmentsRepository:
    """Durable access to :class:`CommitmentRow` and :class:`FollowupRow`."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    # -- commitments --------------------------------------------------------

    async def create_commitment(
        self,
        *,
        person_id: str,
        text: str,
        due_at: datetime | None = None,
        state: str = "open",
        evidence: dict[str, Any] | None = None,
        source_communication_id: str | None = None,
    ) -> CommitmentRow:
        async with self._sessions() as session:
            row = CommitmentRow(
                person_id=person_id,
                text=text,
                due_at=due_at,
                state=state,
                evidence=evidence or {},
                source_communication_id=source_communication_id,
            )
            session.add(row)
            await session.commit()
            return row

    async def get_commitment(self, commitment_id: str) -> CommitmentRow | None:
        async with self._sessions() as session:
            return await session.get(CommitmentRow, commitment_id)

    async def list_commitments(
        self,
        *,
        person_id: str | None = None,
        state: str | None = None,
    ) -> list[CommitmentRow]:
        async with self._sessions() as session:
            stmt = select(CommitmentRow).order_by(CommitmentRow.due_at)
            if person_id is not None:
                stmt = stmt.where(CommitmentRow.person_id == person_id)
            if state is not None:
                stmt = stmt.where(CommitmentRow.state == state)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def set_commitment_state(
        self, commitment_id: str, state: str
    ) -> CommitmentRow | None:
        """Transition a commitment to ``state`` (open/done/dropped/expired)."""

        if state not in VALID_STATES:
            raise ValueError(f"invalid commitment state: {state!r}")
        async with self._sessions() as session:
            row = await session.get(CommitmentRow, commitment_id)
            if row is None:
                return None
            row.state = state
            await session.commit()
            return row

    # -- followups ----------------------------------------------------------

    async def create_followup(
        self,
        *,
        text: str,
        person_id: str | None = None,
        due_at: datetime | None = None,
        state: str = "open",
        linked_refs: list[Any] | None = None,
    ) -> FollowupRow:
        async with self._sessions() as session:
            row = FollowupRow(
                person_id=person_id,
                text=text,
                due_at=due_at,
                state=state,
                linked_refs=linked_refs or [],
            )
            session.add(row)
            await session.commit()
            return row

    async def list_followups(
        self, *, state: str | None = None
    ) -> list[FollowupRow]:
        async with self._sessions() as session:
            stmt = select(FollowupRow)
            if state is not None:
                stmt = stmt.where(FollowupRow.state == state)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @staticmethod
    def to_dict(row: Any) -> dict[str, Any]:
        return _to_dict(row)


__all__ = ["CommitmentsRepository", "VALID_STATES"]
