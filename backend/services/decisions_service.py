"""Read-only Decisions service over the durable ``decisions`` table.

Backs the web Timeline view's two read routes
(``GET /api/decisions`` / ``GET /api/decisions/{id}``). The service is
intentionally *read-only*: decisions are written by the claim / orchestration
planes, and this layer only projects existing rows onto the v0
:class:`schemas.claims.Decision` record so callers never see ORM objects.

Honest degradation (house semantics, mirroring
:meth:`backend.evidence.service.EvidenceService.get`): an empty table yields an
empty list — never fabricated rows — and an unknown or malformed id yields
``None`` (which the endpoint maps to a ``404``).

The ``decisions.id`` column is a ``String`` primary key (the v4 UUID is
generated on the ORM side, as in every other NerveCenter model), so lookups
bind a plain string. This keeps the service portable across the Postgres
production dialect and the in-memory SQLite test dialect alike.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.persistence.models.claims import Decision as DecisionORM
from schemas.claims import Decision


def _as_datetime(value: Any) -> datetime | None:
    """Coerce an ORM temporal value (datetime or ISO string) to an aware UTC datetime.

    Naive values (as returned by non-Postgres test dialects) are assumed to be
    UTC, mirroring :mod:`backend.evidence.service`.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def _orm_to_schema(orm: DecisionORM) -> Decision:
    """Project an ORM decision row onto the v0 pydantic schema."""
    now = datetime.now(UTC)
    created_at = _as_datetime(orm.created_at) or now
    decided_at = _as_datetime(orm.decided_at) or now
    return Decision(
        id=str(orm.id),
        created_at=created_at,
        schema_version=orm.schema_version or "v0",
        hash=orm.hash,
        claim_id=orm.claim_id,
        statement=orm.statement,
        mechanism=orm.mechanism,  # type: ignore[arg-type]
        rationale=orm.rationale,
        action=orm.action,
        supporting_evidence=list(orm.supporting_evidence or []),
        planned_change_id=orm.planned_change_id,
        task_id=orm.task_id,
        decided_at=decided_at,
    )


class DecisionsService:
    """Read-only service for durable decision records."""

    def __init__(self, engine: AsyncEngine):
        """Initialize with a SQLAlchemy async engine (mirrors ``EvidenceService``)."""
        self._engine = engine
        self._sessionmaker = async_sessionmaker(
            bind=engine, expire_on_commit=False, autoflush=False
        )

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    async def list_decisions(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[Decision]:
        """Return durable decision records, newest first, with pagination.

        An empty ``decisions`` table yields an empty list — never a fabricated
        record. Ordering by ``decided_at`` descending keeps the web Timeline
        chronological; ties break on ``id`` for a stable page.
        """
        safe_limit = max(0, min(int(limit), 1000))
        safe_offset = max(0, int(offset))
        stmt = (
            select(DecisionORM)
            .order_by(DecisionORM.decided_at.desc(), DecisionORM.id.desc())
            .limit(safe_limit)
            .offset(safe_offset)
        )
        async with self._sessionmaker() as session:
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [_orm_to_schema(row) for row in rows]

    # ------------------------------------------------------------------
    # fetch one
    # ------------------------------------------------------------------

    async def get(self, decision_id: str) -> Decision | None:
        """Return a single decision by its stable id.

        Returns the record, or ``None`` when the id is unknown or malformed
        (which the endpoint maps to a ``404``). The id is bound as a string to
        match the ``String`` primary-key column; a malformed / unknown id
        simply resolves to no row.
        """
        if not decision_id or not isinstance(decision_id, str):
            return None
        async with self._sessionmaker() as session:
            orm = await session.get(DecisionORM, decision_id)
            if orm is None:
                return None
            return _orm_to_schema(orm)
