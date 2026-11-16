"""Durable long-term researcher on the OmniRoute combo ``secure/planner`` (NS-ADR-103).

One project is a plan (ordered steps) executed one bounded inference call at
a time. Every finished step is committed together with a checkpoint in one
transaction, so a restarted process continues from the last checkpoint and
never repeats a completed step. The engine registers a tick hook with the
agent runner (NS-ADR-099) and so shares its back-off controller: step delays
are the project cadence times the current stretch factor, and nothing new
starts while inference is backed off. All model calls go to OmniRoute with the
model name :data:`COMBO`; there is no other provider call in this package.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.agents.runner.backoff import BACKED_OFF, BackoffController
from backend.agents.runner.runner import _is_throttle
from backend.agents.runner.skillhub import SkillHubClient
from backend.persistence.models import ResearchCheckpoint, ResearchProject, ResearchStep
from backend.research import planner

log = structlog.get_logger(__name__)

COMBO = "secure/planner"
MAX_FAILURES = 3
MIN_RETRY_SECONDS = 60
ACTIVE = ("planned", "running")
PRIORITY = "background"


class ResearchNotFoundError(Exception):
    """No project with that id."""


class ResearchStateError(Exception):
    """The requested change does not fit the project's current status."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; treat stored naive datetimes as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def project_to_dict(p: ResearchProject, steps: list[ResearchStep] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": p.id,
        "title": p.title,
        "question": p.question,
        "status": p.status,
        "budget_hours": p.budget_hours,
        "cadence_seconds": p.cadence_seconds,
        "combo": p.combo,
        "spent_seconds": round(p.spent_seconds or 0.0, 1),
        "error": p.error,
        "notes": p.notes,
        "has_report": bool(p.report),
        "next_step_at": _iso(p.next_step_at),
        "created_at": _iso(p.created_at),
        "finished_at": _iso(p.finished_at),
    }
    if steps is not None:
        finished = sum(1 for s in steps if s.status in ("done", "skipped"))
        out["steps_total"] = len(steps)
        out["steps_done"] = finished
    return out


def step_to_dict(s: ResearchStep) -> dict[str, Any]:
    return {
        "id": s.id,
        "ordinal": s.ordinal,
        "goal": s.goal,
        "status": s.status,
        "result": s.result,
        "attempts": s.attempts,
        "finished_at": _iso(s.finished_at),
    }


class ResearchEngine:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        transport: Any,
        *,
        backoff: BackoffController | None = None,
        skillhub: SkillHubClient | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._factory = session_factory
        self._transport = transport
        self.backoff = backoff or BackoffController()
        self.skillhub = skillhub or SkillHubClient()
        self._clock = clock
        self._inflight: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    # -- project management ---------------------------------------------------------

    async def create(
        self, title: str, question: str, budget_hours: float, cadence_seconds: int
    ) -> dict[str, Any]:
        async with self._factory() as session:
            project = ResearchProject(
                title=title.strip(),
                question=question.strip(),
                status="planned",
                budget_hours=budget_hours,
                cadence_seconds=cadence_seconds,
                combo=COMBO,
                spent_seconds=0.0,
                failures=0,
                next_step_at=self._clock(),
            )
            session.add(project)
            await session.commit()
            await session.refresh(project)
            return project_to_dict(project, [])

    async def list_projects(self) -> list[dict[str, Any]]:
        async with self._factory() as session:
            projects = (
                (
                    await session.execute(
                        select(ResearchProject).order_by(ResearchProject.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            steps = (await session.execute(select(ResearchStep))).scalars().all()
        by_project: dict[str, list[ResearchStep]] = {}
        for s in steps:
            by_project.setdefault(s.project_id, []).append(s)
        return [project_to_dict(p, by_project.get(p.id, [])) for p in projects]

    async def detail(self, project_id: str) -> dict[str, Any]:
        async with self._factory() as session:
            project = await session.get(ResearchProject, project_id)
            if project is None:
                raise ResearchNotFoundError(project_id)
            steps = await self._steps(session, project_id)
            checkpoint = await self._latest_checkpoint(session, project_id)
        out = project_to_dict(project, steps)
        out["steps"] = [step_to_dict(s) for s in steps]
        out["working_notes"] = checkpoint.notes if checkpoint else ""
        out["checkpoint_cursor"] = checkpoint.step_cursor if checkpoint else 0
        out["checkpointed_at"] = _iso(checkpoint.created_at) if checkpoint else None
        return out

    async def report(self, project_id: str) -> dict[str, Any]:
        async with self._factory() as session:
            project = await session.get(ResearchProject, project_id)
            if project is None:
                raise ResearchNotFoundError(project_id)
            return {"id": project.id, "status": project.status, "report": project.report}

    async def set_notes(self, project_id: str, notes: str) -> dict[str, Any]:
        async with self._factory() as session:
            project = await session.get(ResearchProject, project_id)
            if project is None:
                raise ResearchNotFoundError(project_id)
            project.notes = notes
            project.updated_at = self._clock()
            await session.commit()
            return project_to_dict(project)

    async def pause(self, project_id: str) -> dict[str, Any]:
        async with self._factory() as session:
            project = await self._get(session, project_id)
            if project.status not in ACTIVE:
                raise ResearchStateError(f"a {project.status} project cannot be paused")
            project.status = "paused"
            project.updated_at = self._clock()
            await session.commit()
            return project_to_dict(project)

    async def resume(self, project_id: str) -> dict[str, Any]:
        async with self._factory() as session:
            project = await self._get(session, project_id)
            if project.status not in ("paused", "failed"):
                raise ResearchStateError(f"a {project.status} project cannot be resumed")
            steps = await self._steps(session, project_id)
            for s in steps:
                if s.status in ("failed", "running"):
                    s.status, s.attempts = "pending", 0
            project.status = "running" if steps else "planned"
            project.failures = 0
            project.error = None
            project.next_step_at = self._clock()
            project.updated_at = self._clock()
            await session.commit()
            return project_to_dict(project, steps)

    # -- running --------------------------------------------------------------------

    async def advance(self, project_id: str) -> str:
        """Do one unit of work (plan, one step, or the report); returns what was done."""
        if project_id in self._inflight:
            return "busy"
        self._inflight.add(project_id)
        try:
            async with self._factory() as session:
                project = await session.get(ResearchProject, project_id)
                if project is None:
                    raise ResearchNotFoundError(project_id)
                if project.status not in ACTIVE:
                    return "idle"
                # A step left "running" by a killed process has no result: redo it.
                await session.execute(
                    update(ResearchStep)
                    .where(ResearchStep.project_id == project_id, ResearchStep.status == "running")
                    .values(status="pending")
                )
                await session.commit()
                steps = await self._steps(session, project_id)
                checkpoint = await self._latest_checkpoint(session, project_id)
            notes = checkpoint.notes if checkpoint else ""
            if not steps:
                return await self._plan(project)
            pending = next((s for s in steps if s.status == "pending"), None)
            over_budget = (project.spent_seconds or 0.0) >= project.budget_hours * 3600
            if pending is None or over_budget:
                return await self._synthesize(project, steps, notes, partial=pending is not None)
            return await self._run_step(project, steps, pending, notes)
        finally:
            self._inflight.discard(project_id)

    async def _call(self, prompt: str) -> tuple[dict[str, Any], float]:
        started = self._clock()
        result = await asyncio.to_thread(self._transport.generate, COMBO, prompt)
        finished = self._clock()
        latency_ms = result.get("e2e_latency_ms")
        if latency_ms is None:
            latency_ms = (finished - started).total_seconds() * 1000
        self.backoff.record_latency(finished, float(latency_ms))
        return result, float(latency_ms) / 1000.0

    def _delay(self, project: ResearchProject) -> datetime:
        stretch = self.backoff.stretch(PRIORITY)
        return self._clock() + timedelta(seconds=project.cadence_seconds * stretch)

    async def _plan(self, project: ResearchProject) -> str:
        prompt = planner.plan_prompt(project.title, project.question, project.budget_hours)
        try:
            result, seconds = await self._call(prompt)
            goals = planner.parse_plan(str(result.get("content", "")))
            if not goals:
                raise ValueError("the planner returned no usable steps")
        except Exception as exc:  # noqa: BLE001 - recorded on the project
            await self._record_failure(project.id, exc, None)
            return "failed_step"
        async with self._factory() as session:
            row = await self._get(session, project.id)
            for i, goal in enumerate(goals):
                session.add(
                    ResearchStep(
                        project_id=project.id, ordinal=i, goal=goal, status="pending", attempts=0
                    )
                )
            if row.status == "planned":
                row.status = "running"
            row.spent_seconds = (row.spent_seconds or 0.0) + seconds
            row.failures, row.error = 0, None
            row.next_step_at = self._delay(row)
            row.updated_at = self._clock()
            session.add(ResearchCheckpoint(project_id=project.id, step_cursor=0, notes=""))
            await session.commit()
        return "planned"

    async def _run_step(
        self,
        project: ResearchProject,
        steps: list[ResearchStep],
        step: ResearchStep,
        notes: str,
    ) -> str:
        async with self._factory() as session:
            row = await session.get(ResearchStep, step.id)
            row.status = "running"
            await session.commit()
        skills = ""
        try:
            skills = await self.skillhub.skills_context(step.goal)
        except Exception:  # noqa: BLE001 - Skill Hub is optional
            skills = ""
        prompt = planner.step_prompt(
            question=project.question,
            plan=[s.goal for s in steps],
            ordinal=step.ordinal,
            goal=step.goal,
            notes=notes,
            skills=skills,
        )
        try:
            result, seconds = await self._call(prompt)
        except Exception as exc:  # noqa: BLE001 - recorded on the project
            await self._record_failure(project.id, exc, step.id)
            return "failed_step"
        text = str(result.get("content", ""))[: planner.RESULT_CHARS]
        # One transaction: step result + checkpoint + project progress.
        async with self._factory() as session:
            row = await session.get(ResearchStep, step.id)
            row.status, row.result, row.finished_at = "done", text, self._clock()
            row.attempts = (row.attempts or 0) + 1
            row.input_tokens = result.get("input_tokens")
            row.output_tokens = result.get("output_tokens")
            proj = await self._get(session, project.id)
            proj.spent_seconds = (proj.spent_seconds or 0.0) + seconds
            proj.failures, proj.error = 0, None
            proj.next_step_at = self._delay(proj)
            proj.updated_at = self._clock()
            cursor = sum(1 for s in steps if s.status in ("done", "skipped")) + 1
            session.add(
                ResearchCheckpoint(
                    project_id=project.id,
                    step_cursor=cursor,
                    notes=planner.append_notes(notes, step.ordinal, step.goal, text),
                )
            )
            await session.commit()
        return "step_done"

    async def _synthesize(
        self, project: ResearchProject, steps: list[ResearchStep], notes: str, *, partial: bool
    ) -> str:
        results = [(s.goal, s.result or "") for s in steps if s.status == "done"]
        prompt = planner.report_prompt(project.title, project.question, results, partial)
        try:
            result, seconds = await self._call(prompt)
            report = str(result.get("content", "")).strip()
            if not report:
                raise ValueError("the synthesizer returned an empty report")
        except Exception as exc:  # noqa: BLE001 - recorded on the project
            await self._record_failure(project.id, exc, None)
            return "failed_step"
        async with self._factory() as session:
            proj = await self._get(session, project.id)
            for s in await self._steps(session, project.id):
                if s.status == "pending":
                    s.status = "skipped"
            proj.report, proj.status = report, "done"
            proj.finished_at = self._clock()
            proj.spent_seconds = (proj.spent_seconds or 0.0) + seconds
            proj.failures, proj.error, proj.next_step_at = 0, None, None
            proj.updated_at = self._clock()
            session.add(
                ResearchCheckpoint(project_id=project.id, step_cursor=len(steps), notes=notes)
            )
            await session.commit()
        return "done"

    async def _record_failure(self, project_id: str, exc: Exception, step_id: str | None) -> None:
        if _is_throttle(exc):
            self.backoff.record_throttle(self._clock())
        # Reason class only: never the prompt, key or response body.
        log.warning("research.step_failed", project_id=project_id, error=type(exc).__name__)
        async with self._factory() as session:
            proj = await self._get(session, project_id)
            proj.failures = (proj.failures or 0) + 1
            proj.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            if step_id is not None:
                step = await session.get(ResearchStep, step_id)
                step.status = "pending"
                step.attempts = (step.attempts or 0) + 1
            if proj.failures >= MAX_FAILURES:
                proj.status = "failed"
                proj.next_step_at = None
            else:
                stretch = self.backoff.stretch(PRIORITY)
                delay = max(proj.cadence_seconds, MIN_RETRY_SECONDS) * stretch
                proj.next_step_at = self._clock() + timedelta(seconds=delay)
            proj.updated_at = self._clock()
            await session.commit()

    # -- scheduling (registered with the agent runner) --------------------------------

    async def tick(self, *, wait: bool = False) -> list[str]:
        """Start the next due project step; one project runs at a time."""
        if self._inflight:
            return []
        now = self._clock()
        async with self._factory() as session:
            projects = (
                (
                    await session.execute(
                        select(ResearchProject)
                        .where(ResearchProject.status.in_(ACTIVE))
                        .order_by(ResearchProject.created_at)
                    )
                )
                .scalars()
                .all()
            )
            due = None
            for p in projects:
                at = _aware(p.next_step_at)
                if self.backoff.state == BACKED_OFF and at is None:
                    p.next_step_at = self._delay(p)
                    continue
                if at is None or at <= now:
                    if self.backoff.state == BACKED_OFF:
                        p.next_step_at = self._delay(p)
                        continue
                    due = p
                    break
            await session.commit()
        if due is None:
            return []
        project_id = due.id
        task = asyncio.create_task(self._advance_logged(project_id), name=f"research-{project_id}")
        self._inflight.add(project_id)  # claim before the task starts; advance() re-checks
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if wait:
            await task
        return [project_id]

    async def _advance_logged(self, project_id: str) -> None:
        self._inflight.discard(project_id)
        try:
            await self.advance(project_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the runner loop must survive
            log.warning("research.advance_failed", error=type(exc).__name__)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # -- helpers ----------------------------------------------------------------------

    @staticmethod
    async def _get(session: AsyncSession, project_id: str) -> ResearchProject:
        project = await session.get(ResearchProject, project_id)
        if project is None:
            raise ResearchNotFoundError(project_id)
        return project

    @staticmethod
    async def _steps(session: AsyncSession, project_id: str) -> list[ResearchStep]:
        rows = await session.execute(
            select(ResearchStep)
            .where(ResearchStep.project_id == project_id)
            .order_by(ResearchStep.ordinal)
        )
        return list(rows.scalars().all())

    @staticmethod
    async def _latest_checkpoint(
        session: AsyncSession, project_id: str
    ) -> ResearchCheckpoint | None:
        rows = await session.execute(
            select(ResearchCheckpoint)
            .where(ResearchCheckpoint.project_id == project_id)
            .order_by(ResearchCheckpoint.created_at.desc(), ResearchCheckpoint.step_cursor.desc())
            .limit(1)
        )
        return rows.scalars().first()


def build_engine(app: Any) -> ResearchEngine | None:
    from backend.inference.local_first import LocalFirstTransport
    from backend.inference.omniroute import OmniRouteTransport
    from backend.persistence.connection import make_session_factory

    engine = getattr(app.state, "db_engine", None)
    if engine is None:
        return None
    runner = getattr(app.state, "agent_runner", None)
    return ResearchEngine(
        make_session_factory(engine),
        # NS-ADR-107: secure/planner is tier-1 local only; the wrapper keeps it so.
        LocalFirstTransport(OmniRouteTransport.from_env(), "researcher"),
        backoff=getattr(runner, "backoff", None),
        skillhub=getattr(runner, "skillhub", None),
    )


def get_engine(app: Any) -> ResearchEngine | None:
    """The app's researcher, built lazily and hooked into the agent runner's loop."""
    existing = getattr(app.state, "research_engine", None)
    if existing is not None:
        return existing
    from backend.agents.runner import get_runner

    get_runner(app)  # makes sure app.state.agent_runner exists when a DB is configured
    engine = build_engine(app)
    if engine is None:
        return None
    app.state.research_engine = engine
    runner = getattr(app.state, "agent_runner", None)
    if runner is not None:
        runner.add_tick_hook(engine.tick)
    return engine
