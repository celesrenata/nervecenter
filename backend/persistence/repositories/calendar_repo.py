"""Calendar repository — durable access to ``calendar_accounts`` /
``calendar_events`` (Personal-Ops Recovery §8 — workstream E).

Async and session-scoped like the rest of the durable plane (workstream F):
each method opens a session inside a transaction, commits on success and rolls
back on error. Rows are returned as ORM instances so the sync runner and the
endpoints can shape results.

Upsert semantics: :meth:`CalendarRepository.upsert_event` keys on
``(account_id, ical_uid)`` — the provider's stable event identity — so a
repeated sync pass is idempotent and a provider change (new ``DTSTAMP`` /
``changed_at``) updates the row in place. Deleted provider events are recorded
with the explicit ``deleted`` marker rather than silently dropped; the sync
runner then removes them from both the durable table and the in-memory
service (freeze §8: "never silently drop").
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from backend.persistence.models.personal import CalendarAccountRow, CalendarEventRow


class CalendarRepository:
    """Durable access to :class:`CalendarAccountRow` and
    :class:`CalendarEventRow`."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    # -- accounts -----------------------------------------------------------
    async def create_account(
        self,
        *,
        provider: str,
        label: str | None = None,
        account_ref: str | None = None,
        secret_ref: str | None = None,
        timezone: str = "UTC",
        config: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> CalendarAccountRow:
        """Insert a new calendar account row and return it."""
        async with self._sessions() as session:
            row = CalendarAccountRow(
                provider=provider,
                label=label,
                account_ref=account_ref,
                secret_ref=secret_ref,
                timezone=timezone,
                config=config or {},
                sync_state={},
                enabled=enabled,
            )
            session.add(row)
            await session.commit()
            return row

    async def get_account(self, account_id: str) -> CalendarAccountRow | None:
        async with self._sessions() as session:
            return await session.get(CalendarAccountRow, account_id)

    async def list_accounts(self, *, enabled_only: bool = False) -> list[CalendarAccountRow]:
        async with self._sessions() as session:
            stmt = select(CalendarAccountRow).order_by(CalendarAccountRow.created_at)
            if enabled_only:
                stmt = stmt.where(CalendarAccountRow.enabled.is_(True))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def update_account(
        self,
        account_id: str,
        *,
        label: str | None = None,
        account_ref: str | None = None,
        secret_ref: str | None = None,
        timezone: str | None = None,
        config: dict[str, Any] | None = None,
        enabled: bool | None = None,
    ) -> CalendarAccountRow | None:
        """Patch the mutable, non-cursor fields; ``None`` leaves a field
        untouched. Returns the updated row (``None`` if absent)."""
        async with self._sessions() as session:
            row = await session.get(CalendarAccountRow, account_id)
            if row is None:
                return None
            if label is not None:
                row.label = label
            if account_ref is not None:
                row.account_ref = account_ref
            if secret_ref is not None:
                row.secret_ref = secret_ref
            if timezone is not None:
                row.timezone = timezone
            if config is not None:
                row.config = config
            if enabled is not None:
                row.enabled = enabled
            await session.commit()
            return row

    async def delete_account(self, account_id: str) -> bool:
        """Remove the account and its durable events (cascade)."""
        async with self._sessions() as session:
            row = await session.get(CalendarAccountRow, account_id)
            if row is None:
                return False
            await session.execute(
                CalendarEventRow.__table__.delete().where(
                    CalendarEventRow.account_id == account_id
                )
            )
            await session.delete(row)
            await session.commit()
            return True

    # -- sync state / liveness ----------------------------------------------
    async def save_sync_state(self, account_id: str, state: dict[str, Any]) -> None:
        """Replace the durable per-account sync cursor (sync tokens / etags)."""
        async with self._sessions() as session:
            stmt = (
                update(CalendarAccountRow)
                .where(CalendarAccountRow.id == account_id)
                .values(sync_state=state)
            )
            await session.execute(stmt)
            await session.commit()

    async def mark_attempt(
        self,
        account_id: str,
        *,
        operational_status: str,
        last_attempt_at: datetime,
        last_success_at: datetime | None = None,
        last_error_code: str | None = None,
        last_error_message: str | None = None,
    ) -> None:
        """Record one sync attempt (liveness, spec §3 two-axis status)."""
        async with self._sessions() as session:
            stmt = (
                update(CalendarAccountRow)
                .where(CalendarAccountRow.id == account_id)
                .values(
                    operational_status=operational_status,
                    last_attempt_at=last_attempt_at,
                    last_success_at=last_success_at,
                    last_error_code=last_error_code,
                    last_error_message=last_error_message,
                )
            )
            await session.execute(stmt)
            await session.commit()

    # -- events ---------------------------------------------------------------
    async def upsert_event(
        self,
        *,
        account_id: str,
        ical_uid: str,
        title: str,
        start: datetime | None = None,
        end: datetime | None = None,
        all_day: bool = False,
        location: str | None = None,
        description: str | None = None,
        timezone: str | None = None,
        changed_at: str | None = None,
        appt: bool = True,
        deleted: bool = False,
        status: str = "confirmed",
        extra: dict[str, Any] | None = None,
    ) -> CalendarEventRow:
        """Upsert one event by ``(account_id, ical_uid)``."""
        async with self._sessions() as session:
            stmt = (
                select(CalendarEventRow)
                .where(
                    CalendarEventRow.account_id == account_id,
                    CalendarEventRow.ical_uid == ical_uid,
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                row = CalendarEventRow(account_id=account_id, ical_uid=ical_uid)
                session.add(row)
            row.start = _utc(start)
            row.end = _utc(end)
            row.all_day = all_day
            row.title = title
            row.location = location
            row.description = description
            row.timezone = timezone
            row.changed_at = changed_at
            row.appt = appt
            row.deleted = deleted
            row.status = status
            row.extra = extra or {}
            await session.commit()
            return row

    async def get_event(self, event_id: str) -> CalendarEventRow | None:
        async with self._sessions() as session:
            row = await session.get(CalendarEventRow, event_id)
        if row is not None:
            _attach_utc(row)
        return row

    async def get_events_for_account(
        self, account_id: str
    ) -> list[CalendarEventRow]:
        """Every durable event row for one account (incl. deleted markers)."""
        async with self._sessions() as session:
            stmt = select(CalendarEventRow).where(
                CalendarEventRow.account_id == account_id
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        for row in rows:
            _attach_utc(row)
        return rows

    async def list_events(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        account_id: str | None = None,
        include_deleted: bool = False,
        limit: int = 500,
    ) -> list[CalendarEventRow]:
        """Durable events in the window [start, end) (half-open, matching the
        in-memory service's semantics). All-day events are matched by their
        day span [midnight, next midnight)."""
        async with self._sessions() as session:
            stmt = select(CalendarEventRow)
            if account_id is not None:
                stmt = stmt.where(CalendarEventRow.account_id == account_id)
            if not include_deleted:
                stmt = stmt.where(CalendarEventRow.deleted.is_(False))

            if start is not None:
                start_day = start.replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                end_or_max = end or _max_dt()
                # Timed events overlapping [start, …) OR all-day events whose
                # day starts before `end`, OR rows that carry no clock at all
                # (NULL ``start`` — all-day / deletion markers with no time).
                timed = (CalendarEventRow.all_day.is_(False)) & (
                    CalendarEventRow.start < end_or_max
                ) & (
                    (CalendarEventRow.end.is_(None))
                    | (CalendarEventRow.end > start)
                )
                allday = (CalendarEventRow.all_day.is_(True)) & (
                    CalendarEventRow.start >= start_day
                ) & (
                    CalendarEventRow.start < end_or_max
                )
                unbounded = CalendarEventRow.start.is_(None)
                stmt = stmt.where(timed | allday | unbounded)
            result = await session.execute(stmt.limit(limit))
            rows = list(result.scalars().all())
        for row in rows:
            _attach_utc(row)
        # Keep the in-memory service's ordering: by effective start.
        rows.sort(key=lambda r: (r.start or _max_dt()))
        return rows

    async def delete_event(self, event_id: str) -> bool:
        """Remove one durable event row (sync delete)."""
        async with self._sessions() as session:
            row = await session.get(CalendarEventRow, event_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def delete_events_by_ical_uid(
        self, account_id: str, ical_uid: str
    ) -> int:
        """Remove all rows for one provider event identity (incl. recurrences)."""
        async with self._sessions() as session:
            stmt = (
                CalendarEventRow.__table__.delete().where(
                    CalendarEventRow.account_id == account_id,
                    CalendarEventRow.ical_uid == ical_uid,
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount or 0


def _utc(value: datetime | None) -> datetime | None:
    """Normalize to UTC (naive → treated as an already-UTC wall clock)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _attach_utc(row: CalendarEventRow) -> None:
    """Re-attach UTC to the row's ``start``/``end`` (SQLite returns naive)."""
    row.start = _utc(row.start)
    row.end = _utc(row.end)


def _max_dt() -> datetime:
    """A tz-aware UTC sentinel used for window comparisons (well past any
    real event). Must stay tz-aware — it is compared against ``start``/``end``
    values that the read paths return with UTC attached."""
    return datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)


__all__ = ["CalendarRepository"]
