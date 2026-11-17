"""Task lifecycle service for NerveCenter (spec Sections 20-23).

This module implements :class:`TaskService`, the async CRUD + scheduling layer
over the ``tasks`` table. It mirrors the design of :class:`~backend.knowledge.entity.EntityService`
and :class:`~backend.goals.service.GoalService`:

- Every mutating method runs inside a single transaction.
- Records are returned as :class:`~schemas.tasks.Task` (pydantic) so callers
  never see ORM rows.
- The service is wired with :class:`~backend.tasks.dag.TaskDAG` for dependency
  resolution and :class:`~backend.tasks.dag.RunnableQueue` for ordered scheduling.

Task lifecycle
--------------
::

    draft -> pending       (owner accepts an agent-proposed task)
    pending -> scheduled   (queued for execution)
    scheduled|pending -> in_progress  (picked by scheduler)
    in_progress -> completed | failed | cancelled

A task may transition to ``pending`` from any state via cancellation to allow
retry.

Spec sections
-------------
- NerveCenter specification Section 20 (Goal Model).
- NerveCenter specification Section 21 (Goal Decomposition).
- NerveCenter specification Section 22 (Progress & Reconciliation).
- NerveCenter specification Section 23 (Task DAG).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.persistence.models import Task as TaskORM
from schemas.tasks import Task as TaskSchema

log = structlog.get_logger(__name__)

__all__ = ["TaskService", "TaskNotFoundError"]


def _utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime is tz-aware UTC (``None`` passthrough)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _orm_to_schema(orm: TaskORM) -> TaskSchema:
    """Project an ORM task onto the v0 pydantic schema."""
    due_at = orm.due_at
    if isinstance(due_at, str):
        try:
            due_at = datetime.fromisoformat(due_at)
        except ValueError:
            due_at = None
    scheduled_at = orm.scheduled_at
    if isinstance(scheduled_at, str):
        try:
            scheduled_at = datetime.fromisoformat(scheduled_at)
        except ValueError:
            scheduled_at = None
    return TaskSchema(
        id=str(orm.id),
        created_at=orm.created_at,
        schema_version=orm.schema_version or "v0",
        hash=orm.hash,
        title=orm.title,
        description=orm.description,
        status=orm.status,  # type: ignore[arg-type]
        priority=int(orm.priority or 0),
        goal_id=orm.goal_id,
        decision_id=orm.decision_id,
        assignee_agent_id=orm.assignee_agent_id,
        due_at=due_at,
        scheduled_at=scheduled_at,
        inputs=dict(orm.inputs or {}),
        parent_task_id=orm.parent_task_id,
        dependencies=list(orm.dependencies or []),
    )


def _schema_to_orm(schema: TaskSchema) -> TaskORM:
    """Project the v0 pydantic schema onto the ORM row (id/created_at preserved)."""
    return TaskORM(
        id=schema.id,
        created_at=schema.created_at,
        schema_version=schema.schema_version or "v0",
        hash=schema.fingerprint() if schema.hash is None else schema.hash,
        title=schema.title,
        description=schema.description,
        status=schema.status,
        priority=int(schema.priority),
        goal_id=schema.goal_id,
        decision_id=schema.decision_id,
        assignee_agent_id=schema.assignee_agent_id,
        due_at=_utc(schema.due_at),
        scheduled_at=_utc(schema.scheduled_at),
        inputs=dict(schema.inputs or {}),
        parent_task_id=schema.parent_task_id,
        dependencies=list(schema.dependencies or []),
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TaskNotFoundError(Exception):
    """Raised when a task id does not resolve to a row in the database."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"Task {task_id!r} not found")
        self.task_id = task_id


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TaskService:
    """Async lifecycle service for :class:`~schemas.tasks.Task` records.

    The service owns the transaction boundary for task writes and returns
    pydantic schema objects (not ORM rows), so callers do not need to import
    the persistence layer.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory

    # -- internal helpers ---------------------------------------------------

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            from backend.persistence.connection import make_session_factory

            return make_session_factory(self._engine)
        return self._session_factory

    async def _load(self, session: AsyncSession, task_id: str) -> TaskORM:
        orm = await session.get(TaskORM, task_id)
        if orm is None:
            raise TaskNotFoundError(task_id)
        return orm

    # -- CRUD ----------------------------------------------------------------

    async def create(self, payload: dict[str, Any]) -> TaskSchema:
        """Create a new task from a plain dict payload.

        ``payload`` must contain ``title``; all other fields fall back to their
        schema defaults. The returned task carries the generated UUID.
        """
        if not payload.get("id"):
            payload = {**payload, "id": str(uuid4())}
        schema = TaskSchema(**payload)

        factory = self._factory()
        async with factory() as session:
            orm = _schema_to_orm(schema)
            session.add(orm)
            await session.commit()
            await session.refresh(orm)
            log.info(
                "tasks.created",
                task_id=str(orm.id),
                title=orm.title,
                status=orm.status,
                priority=orm.priority,
            )
            return _orm_to_schema(orm)

    async def get(self, task_id: str) -> TaskSchema:
        """Fetch a single task by id. Raises :class:`TaskNotFoundError`."""
        factory = self._factory()
        async with factory() as session:
            orm = await self._load(session, task_id)
            return _orm_to_schema(orm)

    async def update(
        self,
        task_id: str,
        changes: dict[str, Any],
    ) -> TaskSchema:
        """Apply a partial update to an existing task.

        The ``changes`` dict may contain any task field except ``id`` and
        ``created_at`` (both immutable). Unknown keys are rejected by the
        underlying pydantic schema.
        """
        if "id" in changes or "created_at" in changes:
            raise ValueError("id and created_at are immutable")
        factory = self._factory()
        async with factory() as session:
            orm = await self._load(session, task_id)
            current = _orm_to_schema(orm)
            merged = TaskSchema.model_validate(
                {**current.model_dump(mode="python"), **changes}
            )
            new_orm = _schema_to_orm(merged)
            orm.title = new_orm.title
            orm.description = new_orm.description
            orm.status = new_orm.status
            orm.priority = new_orm.priority
            orm.goal_id = new_orm.goal_id
            orm.decision_id = new_orm.decision_id
            orm.assignee_agent_id = new_orm.assignee_agent_id
            orm.due_at = new_orm.due_at
            orm.scheduled_at = new_orm.scheduled_at
            orm.inputs = new_orm.inputs
            orm.parent_task_id = new_orm.parent_task_id
            orm.dependencies = new_orm.dependencies
            orm.hash = new_orm.hash
            await session.commit()
            await session.refresh(orm)
            log.info("tasks.updated", task_id=task_id)
            return _orm_to_schema(orm)

    async def delete(self, task_id: str) -> None:
        """Delete a task row. Associated task runs are the caller's responsibility."""
        factory = self._factory()
        async with factory() as session:
            orm = await self._load(session, task_id)
            await session.delete(orm)
            await session.commit()
            log.info("tasks.deleted", task_id=task_id)

    async def list_tasks(
        self,
        *,
        status: str | None = None,
        priority: int | None = None,
        goal_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskSchema]:
        """List tasks with optional filters.

        Results are ordered by priority (descending) then creation time
        (descending) so the most important, newest tasks surface first.
        """
        factory = self._factory()
        stmt = select(TaskORM)
        if status is not None:
            stmt = stmt.where(TaskORM.status == status)
        if priority is not None:
            stmt = stmt.where(TaskORM.priority >= priority)
        if goal_id is not None:
            stmt = stmt.where(TaskORM.goal_id == goal_id)
        stmt = stmt.order_by(desc(TaskORM.priority), desc(TaskORM.created_at))
        stmt = stmt.limit(limit).offset(offset)
        async with factory() as session:
            result = await session.execute(stmt)
            return [_orm_to_schema(row) for row in result.scalars().all()]

    async def list_for_goal(
        self,
        goal_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskSchema]:
        """List tasks belonging to a specific goal."""
        factory = self._factory()
        stmt = select(TaskORM).where(TaskORM.goal_id == goal_id)
        if status is not None:
            stmt = stmt.where(TaskORM.status == status)
        stmt = stmt.order_by(desc(TaskORM.priority), desc(TaskORM.created_at))
        stmt = stmt.limit(limit).offset(offset)
        async with factory() as session:
            result = await session.execute(stmt)
            return [_orm_to_schema(row) for row in result.scalars().all()]

    async def pending_for_scheduling(self, *, limit: int = 100) -> list[TaskSchema]:
        """Return tasks in ``pending`` or ``scheduled`` state ready for scheduler."""
        return await self.list_tasks(
            status="pending", limit=limit
        ) + await self.list_tasks(status="scheduled", limit=limit)

    async def active_tasks(self, *, limit: int = 100) -> list[TaskSchema]:
        """Return tasks in ``in_progress`` state (currently running)."""
        return await self.list_tasks(status="in_progress", limit=limit)

    async def terminal_tasks(
        self,
        *, limit: int = 100,
    ) -> list[TaskSchema]:
        """Return tasks in terminal states (completed, failed, cancelled)."""
        completed = await self.list_tasks(status="completed", limit=limit)
        failed = await self.list_tasks(status="failed", limit=limit)
        cancelled = await self.list_tasks(status="cancelled", limit=limit)
        return completed + failed + cancelled

    # -- lifecycle transitions ----------------------------------------------

    async def schedule(self, task_id: str, at: datetime | None = None) -> TaskSchema:
        """Move a task from ``pending`` to ``scheduled`` (queued)."""
        payload: dict[str, Any] = {"status": "scheduled"}
        if at:
            payload["scheduled_at"] = at
        return await self.update(task_id, payload)

    async def start(self, task_id: str, assignee: str | None = None) -> TaskSchema:
        """Move a task to ``in_progress`` (execution started)."""
        payload: dict[str, Any] = {"status": "in_progress"}
        if assignee:
            payload["assignee_agent_id"] = assignee
        return await self.update(task_id, payload)

    async def complete(self, task_id: str) -> TaskSchema:
        """Mark a task as ``completed`` (success)."""
        return await self._transition(task_id, to_status="completed")

    async def fail(self, task_id: str, reason: str | None = None) -> TaskSchema:
        """Mark a task as ``failed`` (permanent failure)."""
        payload: dict[str, Any] = {"status": "failed"}
        if reason:
            payload["inputs"] = {"error": reason}
        return await self.update(task_id, payload)

    async def cancel(self, task_id: str) -> TaskSchema:
        """Cancel a task; may transition back to ``pending`` for retry."""
        return await self._transition(task_id, to_status="cancelled")

    async def _load_task(self, task_id: str) -> TaskORM:
        factory = self._factory()
        async with factory() as session:
            return await self._load(session, task_id)

    async def _transition(self, task_id: str, *, to_status: str) -> TaskSchema:
        """Validate and apply a task lifecycle transition."""
        terminal = ("completed", "failed", "cancelled")
        factory = self._factory()
        async with factory() as session:
            orm = await self._load(session, task_id)
            current = orm.status
            if current in terminal:
                raise ValueError(
                    f"Task {task_id!r} is terminal ({current!r}); cannot transition to {to_status!r}"
                )
            if current == to_status:
                return _orm_to_schema(orm)
            # Valid transitions
            allowed = {
                "draft": {"pending", "cancelled"},
                "pending": {"scheduled", "cancelled"},
                "scheduled": {"in_progress", "cancelled"},
                "in_progress": {"completed", "failed", "cancelled"},
            }
            if to_status not in allowed.get(current, set()):
                raise ValueError(
                    f"Task {task_id!r}: cannot transition {current!r} -> {to_status!r}"
                )
            orm.status = to_status
            if to_status == "completed":
                orm.scheduled_at = None  # Clear scheduling time after completion
            await session.commit()
            await session.refresh(orm)
            log.info("tasks.transition", task_id=task_id, from_=current, to=to_status)
            return _orm_to_schema(orm)

    # -- priority & dependency helpers --------------------------------------

    async def by_priority(
        self,
        *, limit: int = 100,
    ) -> list[TaskSchema]:
        """Return all tasks ordered by priority (highest first)."""
        factory = self._factory()
        stmt = select(TaskORM).order_by(desc(TaskORM.priority), desc(TaskORM.created_at)).limit(limit)
        async with factory() as session:
            result = await session.execute(stmt)
            return [_orm_to_schema(row) for row in result.scalars().all()]

    async def with_dependency(self, dependency_id: str) -> list[TaskSchema]:
        """Return all tasks that depend on the given task id."""
        factory = self._factory()
        stmt = select(TaskORM).where(TaskORM.dependencies.contains([dependency_id]))
        async with factory() as session:
            result = await session.execute(stmt)
            return [_orm_to_schema(row) for row in result.scalars().all()]

    async def resolve_dependency_cycle(self, task_id: str) -> list[str]:
        """Return the chain of dependencies leading to ``task_id`` (for cycle detection)."""
        visited: set[str] = set()
        chain: list[str] = []

        async def walk(tid: str) -> bool:
            if tid in visited:
                chain.append(tid)
                return True
            visited.add(tid)
            task = await self.get(tid)
            for dep_id in task.dependencies:
                if await walk(dep_id):
                    chain.append(dep_id)
                    return True
            return False

        try:
            await self.get(task_id)
            await walk(task_id)
        except Exception:
            pass
        return chain[::-1]

    async def batch_complete(
        self,
        task_ids: list[str],
    ) -> dict[str, TaskSchema]:
        """Complete multiple tasks in a single transaction (batch operation)."""
        factory = self._factory()
        results: dict[str, TaskSchema] = {}
        async with factory() as session:
            for tid in task_ids:
                orm = await session.get(TaskORM, tid)
                if orm and orm.status != "completed":
                    orm.status = "completed"
                    session.add(orm)
                    results[tid] = _orm_to_schema(orm)
            await session.commit()
            log.info("tasks.batch_complete", count=len(results))
        return results

    async def count_by_status(self) -> dict[str, int]:
        """Return a mapping of status -> count for all tasks."""
        factory = self._factory()
        stmt = select(TaskORM.status, func.count()).group_by(TaskORM.status)
        async with factory() as session:
            result = await session.execute(stmt)
            return {status: count for status, count in result.all()}
