"""Ingest-plane repository: durable cursors and import jobs.

Cursors are the source of truth for resume positions (DB-owned), so a worker
restart never re-imports; import jobs track bulk-import lifecycle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from ..models.personal import ImportJobRow, IngestCursorRow

#: Ingest cursor kinds (frozen by spec §6).
CURSOR_KINDS = ("imap_seq", "gmail_history", "relay_seq")
#: Import job states (frozen by spec §6).
VALID_JOB_STATES = ("pending", "running", "done", "failed")


def _to_dict(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


class ImportRepository:
    """Durable access to :class:`IngestCursorRow` and :class:`ImportJobRow`."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    # -- ingest cursors -----------------------------------------------------

    async def get_cursor(
        self, connection_id: str, kind: str
    ) -> IngestCursorRow | None:
        async with self._sessions() as session:
            stmt = select(IngestCursorRow).where(
                IngestCursorRow.connection_id == connection_id,
                IngestCursorRow.kind == kind,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def set_cursor(
        self,
        connection_id: str,
        kind: str,
        cursor_value: dict[str, Any],
    ) -> IngestCursorRow:
        """Upsert the cursor for ``(connection_id, kind)`` with ``cursor_value``."""

        if kind not in CURSOR_KINDS:
            raise ValueError(f"unknown cursor kind: {kind!r}")
        async with self._sessions() as session:
            stmt = select(IngestCursorRow).where(
                IngestCursorRow.connection_id == connection_id,
                IngestCursorRow.kind == kind,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                row = IngestCursorRow(
                    connection_id=connection_id,
                    kind=kind,
                    cursor_value=cursor_value,
                )
                session.add(row)
            else:
                row.cursor_value = cursor_value
            await session.commit()
            return row

    # -- import jobs --------------------------------------------------------

    async def create_import_job(
        self,
        connection_id: str,
        kind: str,
        *,
        total: int | None = None,
        state: str = "pending",
    ) -> ImportJobRow:
        if state not in VALID_JOB_STATES:
            raise ValueError(f"invalid import job state: {state!r}")
        async with self._sessions() as session:
            row = ImportJobRow(
                connection_id=connection_id,
                kind=kind,
                state=state,
                total=total,
                started_at=datetime.now() if state == "running" else None,
            )
            session.add(row)
            await session.commit()
            return row

    async def get_import_job(self, job_id: str) -> ImportJobRow | None:
        async with self._sessions() as session:
            return await session.get(ImportJobRow, job_id)

    async def update_import_job_progress(
        self,
        job_id: str,
        *,
        progress: dict[str, Any] | None = None,
        state: str | None = None,
    ) -> ImportJobRow | None:
        async with self._sessions() as session:
            row = await session.get(ImportJobRow, job_id)
            if row is None:
                return None
            if progress is not None:
                row.progress = progress
            if state is not None:
                if state not in VALID_JOB_STATES:
                    raise ValueError(f"invalid import job state: {state!r}")
                row.state = state
                if state == "running" and row.started_at is None:
                    row.started_at = datetime.now()
            await session.commit()
            return row

    async def finish_import_job(
        self,
        job_id: str,
        *,
        state: str = "done",
        error_code: str | None = None,
    ) -> ImportJobRow | None:
        if state not in VALID_JOB_STATES:
            raise ValueError(f"invalid import job state: {state!r}")
        async with self._sessions() as session:
            row = await session.get(ImportJobRow, job_id)
            if row is None:
                return None
            row.state = state
            row.finished_at = datetime.now()
            row.error_code = error_code
            await session.commit()
            return row

    @staticmethod
    def to_dict(row: Any) -> dict[str, Any]:
        return _to_dict(row)


__all__ = ["ImportRepository", "CURSOR_KINDS", "VALID_JOB_STATES"]
