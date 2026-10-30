"""Obligation-plane repository (P2-A — Personal Reliability).

Durable access to :class:`~backend.persistence.models.obligations.ObligationRow`.
Built from the session factory exposed by :class:`RepositoryRegistry`
(``registry.session_factory``) — this module never edits ``registry.py``.

Hard rules implemented here (contract: ``docs/contracts/obligation.md``):

- **Deduplication is by ``dedup_key``.** ``upsert_by_dedup_key`` is the ONLY
  insert path for *inferred* obligations that carry a dedup key: if a row
  with the same ``dedup_key`` exists, its ``evidence`` is *merged* (an
  evidence entry whose ``{source_kind, source_id}`` is already present is not
  duplicated) and ``last_verified_at`` is bumped. A second row is never
  created — the same bill seen in email + calendar + Messages collapses to
  one obligation with three evidence references.
- **Terminal states are single-writer.** ``complete`` sets
  ``status = "done"`` + ``completed_at``; ``dismiss`` sets
  ``status = "dismissed"`` + ``dismissed_at``. Neither is terminal on
  re-entry — only ``stale`` re-enters ``active`` (via ``reverify``).
- **Snooze is reversible.** ``snooze`` sets ``status = "snoozed"`` +
  ``snooze_until``. ``wake_up_snoozed`` (called on the attention sweep)
  returns a snoozed row to ``active`` once ``now >= snooze_until``.
- **Status validation.** Every write validates ``status`` / ``origin`` /
  ``obligation_type`` against the frozen value sets; malformed input raises
  ``ValueError`` (never silently coerces).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select

from ..models.obligations import (
    OBLIGATION_ORIGINS,
    OBLIGATION_STATUSES,
    OBLIGATION_TYPES,
    ObligationRow,
)


def _to_dict(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def _require(value: str, allowed: tuple[str, ...], what: str) -> None:
    if value not in allowed:
        raise ValueError(f"invalid {what}: {value!r}")


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, str):
        try:
            return _ensure_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def _merge_evidence(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge two evidence lists by ``{source_kind, source_id}`` key.

    An incoming entry whose ``(source_kind, source_id)`` pair already exists
    in ``existing`` does NOT duplicate — it updates that entry's
    ``confidence``/``snippet`` if present. Order: existing entries keep their
    position; new entries are appended.
    """
    if not incoming:
        return list(existing)
    out = [dict(e) for e in (existing or [])]
    index = {}
    for i, e in enumerate(out):
        key = (e.get("source_kind"), e.get("source_id"))
        index.setdefault(key, i)
    for e in incoming or []:
        key = (e.get("source_kind"), e.get("source_id"))
        i = index.get(key)
        if i is None:
            out.append(dict(e))
            if key not in index:
                index[key] = len(out) - 1
        else:
            # update in place (keep first-seen position)
            out[i] = {**out[i], **e}
    return out


class ObligationsRepository:
    """Durable access to :class:`ObligationRow`."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    # -- create / upsert ------------------------------------------------------

    async def create(
        self,
        *,
        obligation_type: str,
        dedup_key: str,
        title: str,
        origin: str = "explicit",
        source_account: str = "",
        owner_id: str = "default",
        due_at: datetime | None = None,
        trigger: dict[str, Any] | None = None,
        confidence: float = 1.0,
        uncertainty: dict[str, Any] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        status: str = "active",
        next_action: str | None = None,
        suggested_action: dict[str, Any] | None = None,
        priority: int = 1,
        consequence: str | None = None,
        last_verified_at: datetime | None = None,
        relationships: list[dict[str, Any]] | None = None,
        source_record_ref: dict[str, Any] | None = None,
    ) -> ObligationRow:
        """Insert a NEW obligation (no dedup merge — caller already resolved
        the dedup key). Raises ``ValueError`` on an invalid enum value.
        """
        _require(obligation_type, OBLIGATION_TYPES, "obligation type")
        _require(origin, OBLIGATION_ORIGINS, "obligation origin")
        _require(status, OBLIGATION_STATUSES, "obligation status")
        if not dedup_key:
            raise ValueError("obligation dedup_key must be non-empty")
        async with self._sessions() as session:
            row = ObligationRow(
                obligation_type=obligation_type,
                dedup_key=dedup_key,
                title=title,
                origin=origin,
                source_account=source_account,
                owner_id=owner_id,
                due_at=_ensure_utc(due_at),
                trigger=trigger or {},
                confidence=confidence,
                uncertainty=uncertainty or {},
                evidence=list(evidence or []),
                status=status,
                next_action=next_action,
                suggested_action=suggested_action,
                priority=priority,
                consequence=consequence,
                last_verified_at=_ensure_utc(last_verified_at)
                or datetime.now(UTC),
                relationships=list(relationships or []),
                source_record_ref=source_record_ref or {},
            )
            session.add(row)
            await session.commit()
            return row

    async def upsert_by_dedup_key(
        self,
        *,
        dedup_key: str,
        obligation_type: str,
        title: str,
        origin: str = "inferred",
        source_account: str = "",
        owner_id: str = "default",
        due_at: datetime | None = None,
        evidence: list[dict[str, Any]] | None = None,
        next_action: str | None = None,
        consequence: str | None = None,
        confidence: float = 1.0,
        priority: int = 1,
        relationships: list[dict[str, Any]] | None = None,
        source_record_ref: dict[str, Any] | None = None,
    ) -> tuple[ObligationRow, bool]:
        """Insert-or-merge by ``dedup_key``.

        Returns ``(row, created)`` — ``created`` is ``True`` when a new row was
        written, ``False`` when an existing row was merged (evidence merged,
        ``last_verified_at`` bumped). The existing row's status / origin /
        priority are NOT clobbered by an inferred update (the user's
        decisions win over a re-observation).
        """
        _require(obligation_type, OBLIGATION_TYPES, "obligation type")
        _require(origin, OBLIGATION_ORIGINS, "obligation origin")
        if not dedup_key:
            raise ValueError("obligation dedup_key must be non-empty")
        async with self._sessions() as session:
            stmt = select(ObligationRow).where(ObligationRow.dedup_key == dedup_key)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                row = ObligationRow(
                    obligation_type=obligation_type,
                    dedup_key=dedup_key,
                    title=title,
                    origin=origin,
                    source_account=source_account,
                    owner_id=owner_id,
                    due_at=_ensure_utc(due_at),
                    confidence=confidence,
                    evidence=list(evidence or []),
                    status="active",
                    next_action=next_action,
                    consequence=consequence,
                    priority=priority,
                    relationships=list(relationships or []),
                    last_verified_at=datetime.now(UTC),
                    source_record_ref=source_record_ref or {},
                )
                session.add(row)
                await session.commit()
                return row, True
            # merge
            row.evidence = _merge_evidence(
                row.evidence or [], evidence or []
            )
            if _ensure_utc(due_at) is not None and row.due_at is None:
                row.due_at = _ensure_utc(due_at)
            row.last_verified_at = datetime.now(UTC)
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return row, False

    # -- reads ---------------------------------------------------------------

    async def get(self, obligation_id: str) -> ObligationRow | None:
        async with self._sessions() as session:
            return await session.get(ObligationRow, obligation_id)

    async def get_by_dedup_key(self, dedup_key: str) -> ObligationRow | None:
        async with self._sessions() as session:
            stmt = select(ObligationRow).where(ObligationRow.dedup_key == dedup_key)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def list(
        self,
        *,
        status: str | None = None,
        status_in: tuple[str, ...] | None = None,
        obligation_type: str | None = None,
        due_before: datetime | None = None,
        due_after: datetime | None = None,
        owner_id: str | None = None,
        actionable_only: bool = False,
        limit: int = 200,
    ) -> list[ObligationRow]:
        """List obligations with optional filters.

        ``actionable_only=True`` excludes terminal states
        (``done`` / ``dismissed``) — the Today "Needs You" query.
        """
        async with self._sessions() as session:
            stmt = select(ObligationRow).order_by(
                ObligationRow.priority.desc(),
                ObligationRow.due_at.asc().nulls_last(),
            )
            if status is not None:
                stmt = stmt.where(ObligationRow.status == status)
            if status_in is not None:
                stmt = stmt.where(ObligationRow.status.in_(status_in))
            if obligation_type is not None:
                stmt = stmt.where(ObligationRow.obligation_type == obligation_type)
            if owner_id is not None:
                stmt = stmt.where(ObligationRow.owner_id == owner_id)
            if due_before is not None:
                stmt = stmt.where(ObligationRow.due_at <= _ensure_utc(due_before))
            if due_after is not None:
                stmt = stmt.where(ObligationRow.due_at >= _ensure_utc(due_after))
            if actionable_only:
                stmt = stmt.where(
                    ObligationRow.status.not_in(("done", "dismissed"))
                )
            stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # -- status transitions (single writer) ----------------------------------

    async def accept(
        self, obligation_id: str, now: datetime | None = None
    ) -> ObligationRow | None:
        """Accept an ``inferred`` obligation → becomes actionable ``active``."""
        async with self._sessions() as session:
            row = await session.get(ObligationRow, obligation_id)
            if row is None:
                return None
            row.origin = "explicit"
            row.status = "active"
            row.updated_at = _ensure_utc(now) or datetime.now(UTC)
            await session.commit()
            return row

    async def snooze(
        self,
        obligation_id: str,
        until: datetime,
        now: datetime | None = None,
    ) -> ObligationRow | None:
        async with self._sessions() as session:
            row = await session.get(ObligationRow, obligation_id)
            if row is None:
                return None
            row.status = "snoozed"
            row.snooze_until = _ensure_utc(until) or datetime.now(UTC)
            row.updated_at = _ensure_utc(now) or datetime.now(UTC)
            await session.commit()
            return row

    async def wake_up_snoozed(
        self, now: datetime | None = None
    ) -> list[ObligationRow]:
        """Return every snoozed row whose window has passed back to ``active``.

        Idempotent: a row already ``active`` is untouched. Returns the newly
        re-activated rows.
        """
        now = _ensure_utc(now) or datetime.now(UTC)
        async with self._sessions() as session:
            stmt = select(ObligationRow).where(
                and_(
                    ObligationRow.status == "snoozed",
                    ObligationRow.snooze_until <= now,
                )
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
            for row in rows:
                row.status = "active"
                row.snooze_until = None
                row.updated_at = datetime.now(UTC)
            if rows:
                await session.commit()
            return rows

    async def complete(
        self, obligation_id: str, now: datetime | None = None
    ) -> ObligationRow | None:
        """Set ``done`` + ``completed_at`` (terminal)."""
        async with self._sessions() as session:
            row = await session.get(ObligationRow, obligation_id)
            if row is None:
                return None
            row.status = "done"
            row.completed_at = _ensure_utc(now) or datetime.now(UTC)
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return row

    async def dismiss(
        self, obligation_id: str, now: datetime | None = None
    ) -> ObligationRow | None:
        """Set ``dismissed`` + ``dismissed_at`` (terminal)."""
        async with self._sessions() as session:
            row = await session.get(ObligationRow, obligation_id)
            if row is None:
                return None
            row.status = "dismissed"
            row.dismissed_at = _ensure_utc(now) or datetime.now(UTC)
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return row

    async def reverify(
        self,
        obligation_id: str,
        now: datetime | None = None,
    ) -> ObligationRow | None:
        """Refresh ``last_verified_at``; a ``stale`` row re-enters ``active``."""
        now = _ensure_utc(now) or datetime.now(UTC)
        async with self._sessions() as session:
            row = await session.get(ObligationRow, obligation_id)
            if row is None:
                return None
            row.last_verified_at = now
            if row.status == "stale":
                row.status = "active"
            row.updated_at = now
            await session.commit()
            return row

    async def mark_stale(
        self,
        now: datetime | None = None,
        threshold_days: float = 30.0,
    ) -> list[ObligationRow]:
        """Flag rows whose ``last_verified_at`` is older than the threshold.

        A source that has not been re-verified past the threshold becomes
        ``stale`` — visually distinct from "no items" in Today. Only
        non-terminal rows are affected. Returns the newly-flagged rows.
        """
        now = _ensure_utc(now) or datetime.now(UTC)
        async with self._sessions() as session:
            stmt = select(ObligationRow).where(
                and_(
                    ObligationRow.status.not_in(("done", "dismissed")),
                    or_(
                        ObligationRow.last_verified_at.is_(None),
                        ObligationRow.last_verified_at
                        <= now - _timedelta_days(threshold_days),
                    ),
                )
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
            for row in rows:
                row.status = "stale"
                row.updated_at = datetime.now(UTC)
            if rows:
                await session.commit()
            return rows

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def row_to_dict(row: ObligationRow) -> dict[str, Any]:
        """Serialize a row to a JSON-friendly dict (API boundary)."""
        return _to_dict(row)


def _timedelta_days(days: float) -> Any:
    # Local import to keep module-level deps minimal + testable.
    from datetime import timedelta

    return timedelta(days=days)
