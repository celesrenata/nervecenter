"""Attention-plane repository: attention items, snoozes, notification log.

Owns the "one notification per (item, level)" dedup via
:meth:`AttentionRepository.already_notified`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, select

from ..models.personal import AttentionItemRow, NotificationLogRow, SnoozeRow

#: Attention-item states (frozen by spec §6).
VALID_STATES = ("active", "snoozed", "resolved", "dismissed")
#: Urgency levels (frozen by spec §6).
VALID_URGENCIES = ("low", "normal", "high", "critical")


def _to_dict(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


class AttentionRepository:
    """Durable access to attention items, snoozes, and the notification log."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    # -- attention items ----------------------------------------------------

    async def create_attention_item(
        self,
        *,
        id: str | None = None,
        title: str,
        importance: int = 5,
        urgency: str = "normal",
        body: str | None = None,
        deadline_at: datetime | None = None,
        source_refs: list[Any] | None = None,
        recommended_action: str | None = None,
        escalation_policy: dict[str, Any] | None = None,
        state: str = "active",
        resolution_condition: str | None = None,
    ) -> AttentionItemRow:
        if state not in VALID_STATES:
            raise ValueError(f"invalid attention state: {state!r}")
        if urgency not in VALID_URGENCIES:
            raise ValueError(f"invalid urgency: {urgency!r}")
        async with self._sessions() as session:
            if id is not None:
                existing = await session.get(AttentionItemRow, id)
                if existing is not None:
                    return existing
            row = AttentionItemRow(
                title=title,
                importance=importance,
                urgency=urgency,
                body=body,
                deadline_at=deadline_at,
                source_refs=source_refs or [],
                recommended_action=recommended_action,
                escalation_policy=escalation_policy or {},
                state=state,
                resolution_condition=resolution_condition,
            )
            if id is not None:
                row.id = id
            session.add(row)
            await session.commit()
            return row

    async def get_attention_item(
        self, item_id: str
    ) -> AttentionItemRow | None:
        async with self._sessions() as session:
            return await session.get(AttentionItemRow, item_id)

    async def list_attention_items(
        self,
        *,
        state: str | None = None,
        urgency: str | None = None,
    ) -> list[AttentionItemRow]:
        async with self._sessions() as session:
            stmt = select(AttentionItemRow).order_by(
                AttentionItemRow.deadline_at
            )
            if state is not None:
                stmt = stmt.where(AttentionItemRow.state == state)
            if urgency is not None:
                stmt = stmt.where(AttentionItemRow.urgency == urgency)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def set_state(
        self, item_id: str, state: str, *, at: datetime | None = None
    ) -> AttentionItemRow | None:
        """Transition an item; ``resolved``/``dismissed`` stamp ``resolved_at``."""

        if state not in VALID_STATES:
            raise ValueError(f"invalid attention state: {state!r}")
        async with self._sessions() as session:
            row = await session.get(AttentionItemRow, item_id)
            if row is None:
                return None
            row.state = state
            if state in ("resolved", "dismissed"):
                row.resolved_at = at or datetime.now()
            await session.commit()
            return row

    # -- snoozes ------------------------------------------------------------

    async def snooze(
        self,
        item_id: str,
        *,
        until: datetime,
        reason: str | None = None,
    ) -> SnoozeRow | None:
        """Record a snooze and move the item to the ``snoozed`` state."""

        async with self._sessions() as session:
            item = await session.get(AttentionItemRow, item_id)
            if item is None:
                return None
            row = SnoozeRow(
                attention_item_id=item_id,
                snooze_until=until,
                reason=reason,
            )
            item.state = "snoozed"
            item.snoozed_until = until
            session.add(row)
            await session.commit()
            return row

    # -- notification log (dedup) -------------------------------------------

    async def record_notification(
        self,
        item_id: str,
        *,
        level: str,
        sent_at: datetime | None = None,
        channel: str | None = None,
    ) -> NotificationLogRow:
        """Log a notification for ``(item, level)``.

        The unique ``(attention_item_id, level)`` index enforces one
        notification per (item, level); a duplicate call updates the existing
        row's ``sent_at``/``channel`` rather than inserting a second.
        """

        async with self._sessions() as session:
            stmt = select(NotificationLogRow).where(
                and_(
                    NotificationLogRow.attention_item_id == item_id,
                    NotificationLogRow.level == level,
                )
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                row = NotificationLogRow(
                    attention_item_id=item_id,
                    level=level,
                    sent_at=sent_at or datetime.now(),
                    channel=channel,
                )
                session.add(row)
            else:
                row.sent_at = sent_at or row.sent_at
                row.channel = channel
            await session.commit()
            return row

    async def already_notified(self, item_id: str, level: str) -> bool:
        """True when a notification for ``(item, level)`` is already logged.

        This is the dedup primitive the delivery path (M+K) uses before it
        sends: if this returns true, the notification is suppressed.
        """

        async with self._sessions() as session:
            stmt = select(NotificationLogRow.id).where(
                and_(
                    NotificationLogRow.attention_item_id == item_id,
                    NotificationLogRow.level == level,
                )
            )
            result = await session.execute(stmt)
            return result.first() is not None

    @staticmethod
    def to_dict(row: Any) -> dict[str, Any]:
        return _to_dict(row)


__all__ = [
    "AttentionRepository",
    "VALID_STATES",
    "VALID_URGENCIES",
]
