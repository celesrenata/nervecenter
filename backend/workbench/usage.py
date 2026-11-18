"""Per-Session model usage records (Req 8.8, 8.9).

``UsageRecorder.record`` stores the Session id, model name, input and output
token counts and a timestamp in ``workbench_model_usage``. Its signature has no
parameter for prompt or completion text, so none can be stored. The row is
written when the model request completes, well inside the 10 s budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.persistence.models.workbench import WorkbenchModelUsage

MAX_FIELD_CHARS = 512
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 1000


@dataclass(frozen=True)
class UsageRecord:
    id: int
    session_id: str
    model: str
    input_tokens: int
    output_tokens: int
    recorded_at: datetime


def _to_record(row: WorkbenchModelUsage) -> UsageRecord:
    return UsageRecord(
        id=row.id,
        session_id=row.session_id,
        model=row.model,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        recorded_at=row.recorded_at,
    )


class UsageRecorder:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        session_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        recorded_at: datetime | None = None,
    ) -> UsageRecord:
        """Store one usage row. Accepts no prompt or completion text."""
        if not session_id or not model:
            raise ValueError("session_id and model are required")
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must be non-negative")
        row = WorkbenchModelUsage(
            session_id=session_id[:MAX_FIELD_CHARS],
            model=model[:MAX_FIELD_CHARS],
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            recorded_at=recorded_at or datetime.now(UTC),
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(row)
                await session.flush()
                return _to_record(row)

    async def list_usage(
        self, session_id: str | None = None, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[UsageRecord]:
        """List usage rows, newest first, for the model-policy panel."""
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        stmt = select(WorkbenchModelUsage).order_by(
            WorkbenchModelUsage.recorded_at.desc(), WorkbenchModelUsage.id.desc()
        )
        if session_id is not None:
            stmt = stmt.where(WorkbenchModelUsage.session_id == session_id)
        async with self._session_factory() as session:
            result = await session.execute(stmt.limit(limit))
            return [_to_record(r) for r in result.scalars()]
