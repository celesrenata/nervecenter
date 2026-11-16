"""Persistence for agent schedules and run history (NS-ADR-099)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.agents.overrides import (
    OverrideStore,
    builtin_manifest,
    effective_builtin,
)
from backend.persistence.models import Agent as AgentORM
from backend.persistence.models import AgentRun, AgentSchedule

MODES = ("interval", "daily", "on_demand")
PRIORITIES = ("background", "time_critical")
MIN_INTERVAL_SECONDS = 60
MAX_OUTPUT_CHARS = 20_000


def _aware(row: AgentSchedule | None) -> AgentSchedule | None:
    """SQLite drops tzinfo; treat stored naive datetimes as UTC."""
    if row is not None:
        for field in ("next_run_at", "last_run_at"):
            value = getattr(row, field)
            if value is not None and value.tzinfo is None:
                setattr(row, field, value.replace(tzinfo=UTC))
    return row


def schedule_to_dict(row: AgentSchedule | None, agent_id: str) -> dict[str, Any]:
    if row is None:
        return {
            "agent_id": agent_id,
            "mode": "on_demand",
            "interval_seconds": None,
            "daily_time": None,
            "timezone": None,
            "priority": "background",
            "enabled": False,
            "next_run_at": None,
            "last_run_at": None,
        }
    return {
        "agent_id": row.agent_id,
        "mode": row.mode,
        "interval_seconds": row.interval_seconds,
        "daily_time": row.daily_time,
        "timezone": row.timezone,
        "priority": row.priority,
        "enabled": row.enabled,
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
    }


def run_to_dict(row: AgentRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "status": row.status,
        "trigger": row.trigger,
        "output": row.output,
        "error": row.error,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "latency_ms": row.latency_ms,
        "skills": list(row.skills or []),
        "skills_status": row.skills_status,
    }


class RunnerStore:
    """Async access to ``agent_schedules`` / ``agent_runs``."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def get_agent(self, agent_id: str) -> Any:
        """A persisted instance, or a built-in agent with its override applied (NS-ADR-116)."""
        manifest = builtin_manifest(agent_id)
        if manifest is not None:
            return effective_builtin(manifest, await OverrideStore(self._factory).get(agent_id))
        async with self._factory() as session:
            return await session.get(AgentORM, agent_id)

    async def get_schedule(self, agent_id: str) -> AgentSchedule | None:
        async with self._factory() as session:
            result = await session.execute(
                select(AgentSchedule).where(AgentSchedule.agent_id == agent_id)
            )
            return _aware(result.scalar_one_or_none())

    async def save_schedule(self, agent_id: str, values: dict[str, Any]) -> AgentSchedule:
        async with self._factory() as session:
            result = await session.execute(
                select(AgentSchedule).where(AgentSchedule.agent_id == agent_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = AgentSchedule(agent_id=agent_id)
                session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _aware(row)

    async def list_enabled(self) -> list[AgentSchedule]:
        async with self._factory() as session:
            result = await session.execute(
                select(AgentSchedule).where(
                    AgentSchedule.enabled.is_(True), AgentSchedule.mode != "on_demand"
                )
            )
            return [_aware(r) for r in result.scalars().all()]

    async def mark_ran(
        self, agent_id: str, last_run_at: datetime, next_run_at: datetime | None
    ) -> None:
        async with self._factory() as session:
            result = await session.execute(
                select(AgentSchedule).where(AgentSchedule.agent_id == agent_id)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                row.last_run_at = last_run_at
                row.next_run_at = next_run_at
                await session.commit()

    async def defer(self, agent_id: str, next_run_at: datetime) -> None:
        async with self._factory() as session:
            result = await session.execute(
                select(AgentSchedule).where(AgentSchedule.agent_id == agent_id)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                row.next_run_at = next_run_at
                await session.commit()

    async def record_run(self, **values: Any) -> dict[str, Any]:
        if values.get("output") is not None:
            values["output"] = str(values["output"])[:MAX_OUTPUT_CHARS]
        async with self._factory() as session:
            row = AgentRun(**values)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return run_to_dict(row)

    async def runs(self, agent_id: str, limit: int = 25) -> list[dict[str, Any]]:
        async with self._factory() as session:
            result = await session.execute(
                select(AgentRun)
                .where(AgentRun.agent_id == agent_id)
                .order_by(AgentRun.started_at.desc())
                .limit(max(1, min(limit, 200)))
            )
            return [run_to_dict(r) for r in result.scalars().all()]

    async def counts(self, agent_ids: list[str]) -> dict[str, tuple[int, int]] | None:
        """``{agent_id: (completed, failed)}``; ``None`` when the table is unavailable."""
        if not agent_ids:
            return {}
        try:
            async with self._factory() as session:
                result = await session.execute(
                    select(
                        AgentRun.agent_id,
                        func.sum(case((AgentRun.status == "completed", 1), else_=0)),
                        func.sum(case((AgentRun.status == "failed", 1), else_=0)),
                    )
                    .where(AgentRun.agent_id.in_(agent_ids))
                    .group_by(AgentRun.agent_id)
                )
                return {a: (int(c or 0), int(f or 0)) for a, c, f in result.all()}
        except Exception:  # noqa: BLE001 - table may not exist yet
            return None

    async def delete_agent_data(self, agent_id: str) -> None:
        async with self._factory() as session:
            await session.execute(delete(AgentRun).where(AgentRun.agent_id == agent_id))
            await session.execute(delete(AgentSchedule).where(AgentSchedule.agent_id == agent_id))
            await session.commit()
