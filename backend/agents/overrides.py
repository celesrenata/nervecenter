"""Durable owner overrides for built-in agents (NS-ADR-116).

A built-in agent is defined in code (:mod:`backend.agents.registry`); that
manifest is read-only. The owner can layer an override on top: prompt additions,
pinned skills, an inference binding, and enabled/disabled. The schedule is the
usual ``agent_schedules`` row keyed by the stable built-in id. "Reset to
defaults" removes the override and the schedule.

:func:`effective_builtin` merges manifest and override into the object the
runner, the Skill Hub injection and the tool gateway already consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.agents.registry import AgentManifest, AgentRegistry
from backend.persistence.models.agent_runner import AgentOverride, AgentSchedule

MAX_PROMPT_ADDITION = 4_000

_REGISTRY = AgentRegistry()


def builtin_manifest(agent_id: str) -> AgentManifest | None:
    return _REGISTRY.get(agent_id)


def builtin_ids() -> list[str]:
    return [m.agent_type for m in _REGISTRY.list_all()]


@dataclass
class EffectiveAgent:
    """A built-in agent as it runs: manifest plus override."""

    id: str
    type: str
    name: str
    description: str
    prompt_template: str
    tools: list[str]
    skills: list[str] = field(default_factory=list)
    status: str = "active"
    inference: dict[str, Any] | None = None
    combo_id: str | None = None
    builtin: bool = True


def effective_builtin(manifest: AgentManifest, override: AgentOverride | None) -> EffectiveAgent:
    prompt = manifest.prompt_template
    addition = (override.prompt_addition or "").strip() if override else ""
    if addition:
        prompt = f"{prompt}\n\nOwner additions:\n{addition}"
    return EffectiveAgent(
        id=manifest.agent_type,
        type=manifest.agent_type,
        name=manifest.name,
        description=manifest.description,
        prompt_template=prompt,
        tools=list(manifest.tools),
        skills=list(override.skills or []) if override and override.skills is not None else [],
        status="active" if override is None or override.enabled else "disabled",
        inference=(override.inference if override and override.inference else None)
        or getattr(manifest, "inference", None),
    )


def override_to_dict(override: AgentOverride | None) -> dict[str, Any]:
    if override is None:
        return {
            "has_override": False,
            "prompt_addition": "",
            "skills_overridden": False,
            "inference_overridden": False,
            "enabled": True,
        }
    return {
        "has_override": True,
        "prompt_addition": override.prompt_addition or "",
        "skills_overridden": override.skills is not None,
        "inference_overridden": bool(override.inference),
        "enabled": bool(override.enabled),
    }


class OverrideStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def get(self, agent_id: str) -> AgentOverride | None:
        try:
            async with self._factory() as session:
                result = await session.execute(
                    select(AgentOverride).where(AgentOverride.agent_id == agent_id)
                )
                return result.scalar_one_or_none()
        except Exception:  # noqa: BLE001 - table may not exist yet: run on defaults
            return None

    async def all(self) -> dict[str, AgentOverride]:
        try:
            async with self._factory() as session:
                result = await session.execute(select(AgentOverride))
                return {r.agent_id: r for r in result.scalars().all()}
        except Exception:  # noqa: BLE001 - table may not exist yet
            return {}

    async def save(self, agent_id: str, changes: dict[str, Any]) -> AgentOverride:
        """Upsert the override; only keys present in ``changes`` are written."""
        async with self._factory() as session:
            result = await session.execute(
                select(AgentOverride).where(AgentOverride.agent_id == agent_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = AgentOverride(agent_id=agent_id, enabled=True)
                session.add(row)
            for key, value in changes.items():
                setattr(row, key, value)
            row.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            return row

    async def reset(self, agent_id: str) -> None:
        """Remove the override and the schedule; run history is kept."""
        async with self._factory() as session:
            await session.execute(delete(AgentOverride).where(AgentOverride.agent_id == agent_id))
            await session.execute(delete(AgentSchedule).where(AgentSchedule.agent_id == agent_id))
            await session.commit()
