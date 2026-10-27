"""Routines-plane repository: routines, routine check-ins, sleep records.

Standalone durable repository for the Routine Engine (workstream H). It is
constructed from the session factory exposed by the
:class:`~backend.persistence.repositories.registry.RepositoryRegistry`
(``registry.session_factory``) — the registry itself is *not* edited (workstream
F owns it), so this module is self-contained and testable with any session
factory (including the SQLite scratch databases the test suite uses).

Freeze semantics kept at this layer (spec §9):

- No streak counters anywhere; the one-time "already flagged" markers live in
  the ``routines.missed_flags`` JSONB (per-schedule-window keys) and are only
  *read/merged* here — the windowing decision belongs to the engine.
- ``routine_check_ins`` is a one-row-per-``(routine, UTC day)`` upsert so the
  per-day completion history stays a single, idempotent record per day.
- ``sleep_records.duration_minutes`` is computed on write (never inferred
  later); ``consent_granted`` is stored as a durable fact.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, select

from ..models.routines import (
    RoutineCheckInRow,
    RoutineRow,
    SleepRecordRow,
)


def _to_dict(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def _ensure_utc(dt: datetime | str | None) -> datetime | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=__import__("datetime").UTC)
    return dt.astimezone(__import__("datetime").UTC)


class RoutinesRepository:
    """Durable access to :class:`RoutineRow`, :class:`RoutineCheckInRow`,
    and :class:`SleepRecordRow`."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    # -- routines -----------------------------------------------------------

    async def create_routine(
        self,
        *,
        kind: str,
        title: str,
        schedule: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> RoutineRow:
        async with self._sessions() as session:
            row = RoutineRow(
                kind=kind,
                title=title,
                schedule=schedule or {},
                enabled=enabled,
                deterministic_fallback_alarm={},
                missed_flags={},
            )
            session.add(row)
            await session.commit()
            return row

    async def get_routine(self, routine_id: str) -> RoutineRow | None:
        async with self._sessions() as session:
            return await session.get(RoutineRow, routine_id)

    async def list_routines(self, *, enabled_only: bool = False) -> list[RoutineRow]:
        stmt = select(RoutineRow).order_by(RoutineRow.title)
        if enabled_only:
            stmt = stmt.where(RoutineRow.enabled.is_(True))
        async with self._sessions() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def update_routine(
        self, routine_id: str, **fields: Any
    ) -> RoutineRow | None:
        """Update the allow-listed mutable columns of one routine.

        Only ``title`` / ``kind`` / ``schedule`` / ``enabled`` /
        ``last_completed_at`` / ``deterministic_fallback_alarm`` /
        ``missed_flags`` are updatable; unknown keys are ignored so callers
        cannot corrupt provenance columns.
        """
        allowed = {
            "title",
            "kind",
            "schedule",
            "enabled",
            "last_completed_at",
            "deterministic_fallback_alarm",
            "missed_flags",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self.get_routine(routine_id)
        async with self._sessions() as session:
            row = await session.get(RoutineRow, routine_id)
            if row is None:
                return None
            for key, value in updates.items():
                setattr(row, key, value)
            await session.commit()
            return row

    async def delete_routine(self, routine_id: str) -> bool:
        """Delete the routine; its check-ins cascade away (FK ondelete)."""
        async with self._sessions() as session:
            row = await session.get(RoutineRow, routine_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def touch_completed(
        self, routine_id: str, at: datetime
    ) -> RoutineRow | None:
        """Stamp ``last_completed_at`` (used by check-in / complete)."""
        return await self.update_routine(
            routine_id, last_completed_at=_ensure_utc(at)
        )

    async def merge_missed_flags(
        self, routine_id: str, flags: dict[str, Any]
    ) -> RoutineRow | None:
        """Idempotently merge one-time missed-window markers.

        Existing keys keep their first (earliest) flag timestamp — the
        marker is one-time and re-flagging the same window is a no-op.
        """
        async with self._sessions() as session:
            row = await session.get(RoutineRow, routine_id)
            if row is None:
                return None
            current = dict(row.missed_flags or {})
            changed = False
            for key, value in (flags or {}).items():
                if key not in current:
                    current[key] = value
                    changed = True
            if changed:
                row.missed_flags = current
                await session.commit()
            return row

    async def routines_due_on_local_day(
        self, day: date, timezone: str
    ) -> list[RoutineRow]:
        """Enabled routines whose schedule weekdays cover ``day``'s weekday in
        ``timezone`` (``day`` is the *local* calendar day in that zone; ISO
        weekday: Monday=0 … Sunday=6). An empty ``weekdays`` list means every
        day. The wall-clock slot evaluation itself belongs to the engine."""
        weekday = day.isoweekday() - 1
        rows = await self.list_routines(enabled_only=True)
        out: list[RoutineRow] = []
        for row in rows:
            weekdays = (row.schedule or {}).get("weekdays") or []
            if not weekdays:
                out.append(row)
            elif weekday in [int(w) for w in weekdays]:
                out.append(row)
        return out

    # -- check-ins ----------------------------------------------------------

    async def upsert_check_in(
        self,
        *,
        routine_id: str,
        at: datetime,
        completed: bool = True,
        note: str | None = None,
    ) -> RoutineCheckInRow:
        """One row per (routine, UTC day); repeat check-ins update the row.

        Upserting on the day keeps the per-day completion history a single
        idempotent record — checking in twice on the same day does not grow
        the history (and nothing is ever counted against the user).
        """
        at_utc = _ensure_utc(at)
        if at_utc is None:
            raise ValueError("check-in 'at' must be a datetime")
        day = at_utc.date()
        async with self._sessions() as session:
            stmt = (
                select(RoutineCheckInRow)
                .where(RoutineCheckInRow.routine_id == routine_id)
                .order_by(RoutineCheckInRow.at.desc())
                .limit(7)
            )
            result = await session.execute(stmt)
            row = next(
                (
                    r
                    for r in result.scalars().all()
                    if _ensure_utc(r.at).date() == day
                ),
                None,
            )
            if row is None:
                row = RoutineCheckInRow(
                    routine_id=routine_id, at=at_utc, completed=completed, note=note
                )
                session.add(row)
            else:
                row.at = at_utc
                row.completed = completed
                row.note = note
            await session.commit()
            return row

    async def list_check_ins(
        self, routine_id: str, *, since: datetime | None = None
    ) -> list[RoutineCheckInRow]:
        stmt = (
            select(RoutineCheckInRow)
            .where(RoutineCheckInRow.routine_id == routine_id)
            .order_by(RoutineCheckInRow.at.asc())
        )
        if since is not None:
            stmt = stmt.where(RoutineCheckInRow.at >= _ensure_utc(since))
        async with self._sessions() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # -- sleep records ------------------------------------------------------

    @staticmethod
    def compute_duration_minutes(
        bed_at: datetime, wake_at: datetime | None
    ) -> int | None:
        """Duration computed on write: ``None`` while ``wake_at`` is absent
        (missing data is unknown — never an assertion)."""
        bed = _ensure_utc(bed_at)
        wake = _ensure_utc(wake_at)
        if bed is None or wake is None:
            return None
        minutes = int(round((wake - bed).total_seconds() / 60))
        return minutes if minutes >= 0 else None

    async def create_sleep_record(
        self,
        *,
        bed_at: datetime,
        wake_at: datetime | None = None,
        source: str,
        consent_granted: bool = True,
    ) -> SleepRecordRow:
        row = SleepRecordRow(
            bed_at=_ensure_utc(bed_at),
            wake_at=_ensure_utc(wake_at),
            source=source,
            duration_minutes=self.compute_duration_minutes(bed_at, wake_at),
            consent_granted=bool(consent_granted),
        )
        async with self._sessions() as session:
            session.add(row)
            await session.commit()
            return row

    async def list_sleep_records(
        self, *, days: int = 14, now: datetime | None = None
    ) -> list[SleepRecordRow]:
        """Sleep records whose ``bed_at`` falls in the trailing ``days``
        window (inclusive of the current day)."""
        anchor = _ensure_utc(now) or datetime.now(__import__("datetime").UTC)
        since = anchor - timedelta(days=max(1, int(days)))
        stmt = (
            select(SleepRecordRow)
            .where(and_(SleepRecordRow.bed_at >= since))
            .order_by(SleepRecordRow.bed_at.asc())
        )
        async with self._sessions() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def latest_sleep_record(
        self, now: datetime | None = None
    ) -> SleepRecordRow | None:
        stmt = select(SleepRecordRow).order_by(SleepRecordRow.bed_at.desc()).limit(1)
        async with self._sessions() as session:
            result = await session.execute(stmt)
            return result.scalars().first()
