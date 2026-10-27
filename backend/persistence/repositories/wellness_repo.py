"""Wellness-plane repository (Personal-Ops Recovery §10 — workstream I).

Owns durable access to the private ``wellness_entries`` table plus the two
durable preference keys the enable/pause state lives in (``preferences``
table, per the §6 pattern used by the Today endpoint's Low Energy Mode).

Privacy note: this repository is *local-only by design* — it reads and
writes wellness rows for the user's own journal. It never publishes them to
inference, OmniRoute, or any cloud transport, and nothing in the package
imports those layers (enforced by the import-graph test in
``tests/backend/test_wellness.py``).

Every method opens a session inside a transaction, commits on success, and
rolls back on error — the same transactional semantics as the other
repositories in :mod:`backend.persistence.repositories`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select

from ..models.personal import PreferenceRow
from ..models.wellness import WellnessEntryRow

#: Preference keys (durable ``preferences`` table, §6).
PREF_ENABLED = "wellness.enabled"
PREF_PAUSED_UNTIL = "wellness.paused_until"

#: Stable audit key for clinician-export log rows (durable ``preferences``
#: table — the audit ledger lives next to the other wellness prefs).
EXPORT_LOG_KEY = "wellness.export_log"
EXPORT_AUDIT_CODE = "wellness_export"


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """Half-open [start, end) bounds covering ``day``.

    The *date* of ``at`` is the upsert key. We bound on the raw timestamptz
    value (day start .. day end, stored naive-UTC): the service normalizes
    each check-in to midnight-UTC-of-that-day before persisting, so the
    date math is stable across dialects (SQLite scratch DBs included).
    """
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


def _normalize_day(at: datetime) -> datetime:
    """Reduce a check-in moment to midnight-UTC-of-that-day (the upsert key)."""
    if at.tzinfo is not None:
        at = at.astimezone(timezone.utc)
    return datetime(at.year, at.month, at.day)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WellnessRepository:
    """Durable access to wellness entries and wellness preferences."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    # -- entries -------------------------------------------------------------

    async def upsert_by_day(
        self,
        *,
        at: datetime,
        mood: int | None = None,
        sleep_minutes: int | None = None,
        sleep_consistency: dict[str, Any] | None = None,
        energy: int | None = None,
        stress: int | None = None,
        journal_entry: str | None = None,
        observations: dict[str, Any] | None = None,
    ) -> tuple[WellnessEntryRow, bool]:
        """One check-in per day: re-saving the same day updates in place.

        A same-day re-save *overwrites* the provided fields and clears the
        ones passed as ``None`` (an un-checked field is not carried forward —
        the daily record reflects the latest save for that day).

        Returns ``(row, created)`` — ``created`` is True only when this
        call inserted a brand-new row (False for a same-day update).
        """
        key = _normalize_day(at)
        start, end = _day_bounds(key.date())
        async with self._sessions() as session:
            result = await session.execute(
                select(WellnessEntryRow)
                .where(WellnessEntryRow.at >= start, WellnessEntryRow.at < end)
                .limit(1)
            )
            row = result.scalar_one_or_none()
            created = row is None
            if row is None:
                row = WellnessEntryRow(at=start)
                session.add(row)
            row.at = start
            row.mood = mood
            row.sleep_minutes = sleep_minutes
            row.sleep_consistency = sleep_consistency
            row.energy = energy
            row.stress = stress
            row.journal_entry = journal_entry
            row.observations = observations
            row.updated_at = _utcnow()
            await session.commit()
            return row, created

    async def get_entry(self, entry_id: str) -> WellnessEntryRow | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(WellnessEntryRow).where(WellnessEntryRow.id == entry_id)
            )
            return result.scalar_one_or_none()

    async def list_entries(
        self,
        *,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        days: int | None = None,
    ) -> list[WellnessEntryRow]:
        """Windowed list, newest first.

        ``days`` (relative, back from now) is an alternative to ``from_dt``/``to_dt``.
        """
        async with self._sessions() as session:
            stmt = select(WellnessEntryRow)
            if from_dt is not None:
                stmt = stmt.where(WellnessEntryRow.at >= from_dt)
            if to_dt is not None:
                stmt = stmt.where(WellnessEntryRow.at <= to_dt)
            if days is not None:
                stmt = stmt.where(WellnessEntryRow.at >= _utcnow() - timedelta(days=days))
            result = await session.execute(stmt.order_by(WellnessEntryRow.at.desc()))
            return list(result.scalars().all())

    async def update_entry(
        self,
        *,
        entry_id: str,
        mood: int | None = None,
        sleep_minutes: int | None = None,
        sleep_consistency: dict[str, Any] | None = None,
        energy: int | None = None,
        stress: int | None = None,
        journal_entry: str | None = None,
        observations: dict[str, Any] | None = None,
    ) -> WellnessEntryRow | None:
        """Field-level patch: only the provided (non-``None``) fields change.

        ``None`` means "leave as-is" here — the opposite of the upsert
        semantics, which is correct for an explicit per-field edit.
        """
        async with self._sessions() as session:
            result = await session.execute(
                select(WellnessEntryRow).where(WellnessEntryRow.id == entry_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            if mood is not None:
                row.mood = mood
            if sleep_minutes is not None:
                row.sleep_minutes = sleep_minutes
            if sleep_consistency is not None:
                row.sleep_consistency = sleep_consistency
            if energy is not None:
                row.energy = energy
            if stress is not None:
                row.stress = stress
            if journal_entry is not None:
                row.journal_entry = journal_entry
            if observations is not None:
                row.observations = observations
            row.updated_at = _utcnow()
            await session.commit()
            return row

    async def delete_entry(self, entry_id: str) -> bool:
        async with self._sessions() as session:
            result = await session.execute(
                select(WellnessEntryRow).where(WellnessEntryRow.id == entry_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def delete_all(self) -> int:
        """Hard-delete *every* wellness entry (spec §10 — easy to delete)."""
        async with self._sessions() as session:
            result = await session.execute(delete(WellnessEntryRow))
            await session.commit()
            return int(result.rowcount or 0)

    async def count_entries(self) -> int:
        async with self._sessions() as session:
            result = await session.execute(select(func.count(WellnessEntryRow.id)))
            return int(result.scalar_one() or 0)

    async def last_entry_at(self) -> datetime | None:
        async with self._sessions() as session:
            result = await session.execute(select(func.max(WellnessEntryRow.at)))
            return result.scalar_one()

    # -- trend aggregates (SQL) ----------------------------------------------

    async def trend_rows(
        self,
        *,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Per-day aggregate rows for the deterministic TrendEngine.

        Returns one dict per day present in the window with
        ``{day, mood, energy, stress, sleep_minutes, n}`` (nulls preserved —
        the engine does the null-safe statistics in Python).
        """
        if days is not None and from_dt is None:
            from_dt = _utcnow() - timedelta(days=days)
        async with self._sessions() as session:
            day = func.date(WellnessEntryRow.at)
            stmt = (
                select(
                    day.label("day"),
                    func.avg(WellnessEntryRow.mood).label("mood"),
                    func.avg(WellnessEntryRow.energy).label("energy"),
                    func.avg(WellnessEntryRow.stress).label("stress"),
                    func.avg(WellnessEntryRow.sleep_minutes).label("sleep_minutes"),
                    func.count(WellnessEntryRow.id).label("n"),
                )
                .group_by(day)
                .order_by(day)
            )
            if from_dt is not None:
                stmt = stmt.where(WellnessEntryRow.at >= from_dt)
            if to_dt is not None:
                stmt = stmt.where(WellnessEntryRow.at <= to_dt)
            result = await session.execute(stmt)
            return [
                {
                    "day": str(r.day)[:10] if r.day is not None else None,
                    "mood": r.mood,
                    "energy": r.energy,
                    "stress": r.stress,
                    "sleep_minutes": r.sleep_minutes,
                    "n": r.n,
                }
                for r in result.all()
            ]

    # -- preferences (enable / pause) -----------------------------------------

    async def get_preference(self, key: str) -> dict[str, Any] | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(PreferenceRow).where(PreferenceRow.key == key)
            )
            row = result.scalar_one_or_none()
            return row.value if row is not None else None

    async def set_preference(self, key: str, value: dict[str, Any]) -> None:
        async with self._sessions() as session:
            result = await session.execute(
                select(PreferenceRow).where(PreferenceRow.key == key)
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = PreferenceRow(key=key, value=value)
                session.add(row)
            else:
                row.value = value
            await session.commit()

    # -- audit ----------------------------------------------------------------

    async def log_export(self, *, entries: int, window: str, fmt: str) -> str:
        """Append a stable-code audit entry for a clinician export.

        The freeze allows the export event to be recorded as an audit row
        with the stable code :data:`EXPORT_AUDIT_CODE`. The ledger lives in
        the durable ``preferences`` table under :data:`EXPORT_LOG_KEY` (a
        JSONB array, append-only) — the same durable store the enable/pause
        flags live in, readable for compliance review, and not owned by any
        other workstream. Returns the audit entry's id.
        """
        entry_id = str(uuid.uuid4())
        async with self._sessions() as session:
            result = await session.execute(
                select(PreferenceRow).where(PreferenceRow.key == EXPORT_LOG_KEY)
            )
            row = result.scalar_one_or_none()
            log: list[dict[str, Any]] = list(row.value) if row else []
            log.append(
                {
                    "id": entry_id,
                    "code": EXPORT_AUDIT_CODE,
                    "at": _utcnow().isoformat(),
                    "entries": entries,
                    "window": window,
                    "format": fmt,
                }
            )
            if row is None:
                row = PreferenceRow(key=EXPORT_LOG_KEY, value=log)
                session.add(row)
            else:
                row.value = log
            await session.commit()
        return entry_id
