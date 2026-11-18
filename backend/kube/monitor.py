"""Hourly kube check and daily waste report (NS-ADR-120).

``KubeMonitor.tick`` is an AgentRunner tick hook and calls no model. It runs the deterministic
check at most once an hour and the waste report at most once a day (``kube_settings`` stores both
times). A check stores a snapshot, syncs the issue ledger and folds open issues into groups
(``backend.kube.groups``). A background focus pass then starts one ``kube_doctor`` run per GROUP
(the prompt carries only that group), one after another, waiting for other agent runs, within the
per-group cooldown and the daily cap. Known-down namespaces never trigger anything.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select

from backend.kube.groups import (
    DiagnosisStore,
    group_context,
    group_issues,
    parse_diagnosis,
    used_evidence_tool,
)
from backend.kube.issues import IssueStore, ReportStore
from backend.kube.settings import KubeOverrideStore, KubeSettingsStore
from backend.persistence.models.agent_runner import AgentRun

log = structlog.get_logger(__name__)

AGENT_ID = "kube_doctor"
TRIGGER = "kube_issue"
CHECK_INTERVAL = timedelta(hours=1)
WASTE_INTERVAL = timedelta(hours=24)
MAX_DRAFT_TASKS = 5
STAGGER_SECONDS = 90.0
IDLE_POLL_SECONDS = 30.0
IDLE_WAIT_MAX_SECONDS = 1800.0
PLAN_EVERY = timedelta(minutes=5)
FAILED_RETRY = timedelta(hours=1)
DRAFT_MARK = "agent:kube_doctor"


class CheckBusy(Exception):
    """A check is already running."""


def _evidence_line(issue: dict[str, Any]) -> str:
    ev = issue.get("evidence") or {}
    bits = [f"{k}={str(v)[:80]}" for k, v in list(ev.items())[:4]
            if isinstance(v, (str, int, float, bool))]
    return ", ".join(bits)


def _due(last: datetime | None, now: datetime, every: timedelta) -> bool:
    return last is None or now - last >= every


class KubeMonitor:
    def __init__(self, session_factory: Any,
                 collector: Callable[[], Awaitable[dict[str, Any]]],
                 waste: Callable[[str | None], Awaitable[dict[str, Any]]],
                 run_agent: Callable[..., Awaitable[Any]],
                 *, settings_store: KubeSettingsStore, issue_store: IssueStore,
                 report_store: ReportStore, override_store: KubeOverrideStore | None = None,
                 create_task: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
                 inflight: Callable[[], bool] | None = None,
                 other_busy: Callable[[], bool] | None = None,
                 tool_events: Callable[[], list[dict[str, Any]]] | None = None,
                 stagger_seconds: float = STAGGER_SECONDS,
                 sleep: Callable[[float], Awaitable[Any]] | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self._sessions = session_factory
        self._collector = collector
        self._waste = waste
        self._run_agent = run_agent
        self._settings = settings_store
        self._issues = issue_store
        self._reports = report_store
        self._overrides = override_store or KubeOverrideStore(session_factory, settings_store)
        self._create_task = create_task
        self._inflight = inflight or (lambda: False)
        self._other_busy = other_busy or (lambda: False)
        self._tool_events = tool_events
        self._stagger = stagger_seconds
        self._sleep = sleep or asyncio.sleep
        self._diagnoses = DiagnosisStore(report_store)
        self._focus_task: asyncio.Task[None] | None = None
        self._last_plan: datetime | None = None
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self.last: dict[str, Any] | None = None

    @property
    def focusing(self) -> bool:
        return self._focus_task is not None and not self._focus_task.done()

    async def wait_focus(self) -> None:
        """Wait for the running focus pass (tests and shutdown)."""
        task = self._focus_task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def tick(self) -> None:
        """Runner tick hook: hourly check, daily waste report."""
        if self._lock.locked():
            return
        try:
            settings = await self._settings.get()
            now = self._now()
            if settings.get("check_enabled", True) and _due(
                    settings["last_check_at"], now, CHECK_INTERVAL):
                await self.check()
            if settings.get("waste_enabled", True) and _due(
                    settings["last_waste_at"], now, WASTE_INTERVAL):
                await self.waste()
            if (not self.focusing and settings.get("auto_agent_enabled", True)
                    and (self._last_plan is None or now - self._last_plan >= PLAN_EVERY)):
                await self._start_focus(settings, now, {"warnings": []})
        except CheckBusy:
            return
        except Exception as exc:  # noqa: BLE001 - a tick hook must not stop the runner
            log.warning("kube.monitor_tick_failed", error=type(exc).__name__)

    async def check(self, wait: bool = False) -> dict[str, Any]:
        """Run the check now. Raises CheckBusy when one is running (unless ``wait``)."""
        if self._lock.locked() and not wait:
            raise CheckBusy("A check is already running.")
        async with self._lock:
            started = self._now()
            await self._settings.touch("last_check_at", started)  # claimed before any work
            summary: dict[str, Any] = {"started_at": started.isoformat(), "warnings": [],
                                       "new": 0, "open": 0, "known_down": 0, "resolved": 0,
                                       "triggered": None, "draft_tasks": 0}
            try:
                snapshot = await self._collector()
            except Exception as exc:  # noqa: BLE001 - a collect failure is a warning
                summary["warnings"].append(f"cluster not read ({type(exc).__name__})")
                self.last = summary
                log.warning("kube.check_collect_failed", error=type(exc).__name__)
                return summary
            cov = snapshot.get("coverage")
            if cov:
                summary["coverage"] = cov.get("line")
                summary["coverage_percent"] = cov.get("percent")
            summary["warnings"] += [str(w)[:200] for w in snapshot.get("warnings", [])][:10]
            await self._reports.add("snapshot", snapshot, started)
            settings = await self._settings.get()
            overrides = await self._overrides.active(started)
            synced = await self._issues.sync(snapshot, settings, started, overrides)
            summary.update(new=len(synced["new"]), open=len(synced["open"]),
                           known_down=len(synced["known_down"]), resolved=len(synced["resolved"]))
            summary["draft_tasks"] = await self._draft_tasks(synced["new"])
            summary["triggered"] = await self._start_focus(settings, started, summary)
            self.last = summary
            log.info("kube.check_done", **{k: v for k, v in summary.items()
                                           if k not in ("warnings", "started_at")})
            return summary

    async def waste(self) -> dict[str, Any] | None:
        started = self._now()
        await self._settings.touch("last_waste_at", started)  # claimed before any work
        settings = await self._settings.get()
        try:
            report = await self._waste(settings.get("prometheus_url"))
        except Exception as exc:  # noqa: BLE001
            log.warning("kube.waste_failed", error=type(exc).__name__)
            return None
        await self._reports.add("waste", report, started)
        return report

    async def _draft_tasks(self, new: list[dict[str, Any]]) -> int:
        if self._create_task is None:
            return 0
        made = 0
        for issue in new:
            if issue["mode"] != "normal" or issue["kind"] == "coverage" or made >= MAX_DRAFT_TASKS:
                continue
            payload = {
                "title": f"Kube: {issue['reason']} {issue['namespace']}/{issue['name']}"[:200],
                "description": f"Found by the hourly kube check ({issue['key']}). "
                               f"{_evidence_line(issue)}"[:4000],
                "status": "draft", "priority": 3, "goal_id": None, "dependencies": [],
                "inputs": {"origin": DRAFT_MARK, "tag": DRAFT_MARK},
            }
            try:
                await self._create_task(payload)
                made += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("kube.draft_task_failed", error=type(exc).__name__)
                break
        return made

    async def _runs_today(self, now: datetime) -> int:
        async with self._sessions() as s:
            return int((await s.execute(
                select(func.count()).select_from(AgentRun).where(
                    AgentRun.agent_id == AGENT_ID, AgentRun.trigger == TRIGGER,
                    AgentRun.started_at >= now - timedelta(hours=24),
                    AgentRun.status != "skipped"))).scalar() or 0)

    async def _plan(self, settings: dict[str, Any], now: datetime, warnings: list[str]
                    ) -> list[dict[str, Any]]:
        """Groups that get a focus run now: not known-down, out of cooldown, within the cap."""
        if not settings.get("auto_agent_enabled", True):
            warnings.append("automatic kube_doctor runs are switched off")
            return []
        cooldown = timedelta(hours=settings["issue_cooldown_hours"])
        known = await self._diagnoses.all()
        eligible = []
        for group in group_issues(await self._issues.list("open")):
            if group["mode"] == "known_down" or group["cause"].startswith("coverage-"):
                continue  # known down, or a blind check: for the owner, nothing to diagnose
            prev = known.get(group["id"]) or {}
            ran = _parse_time(prev.get("run_at"))
            wait = min(cooldown, FAILED_RETRY) if prev.get("status") == "failed" else cooldown
            if ran is not None and now - ran < wait:
                continue
            eligible.append(group)
        room = max(settings["daily_run_cap"] - await self._runs_today(now), 0)
        if len(eligible) > room:
            warnings.append(f"daily kube_doctor run cap reached; {len(eligible) - room} "
                            "group(s) wait")
        return eligible[:room]

    async def _start_focus(self, settings: dict[str, Any], now: datetime,
                           summary: dict[str, Any]) -> list[str] | None:
        """Plan the groups and start the background pass; returns the group ids planned."""
        self._last_plan = now
        if self.focusing:
            summary["warnings"].append("a focus pass is still running; new groups wait")
            return None
        groups = await self._plan(settings, now, summary["warnings"])
        if not groups:
            return None
        self._focus_task = asyncio.create_task(self._focus([g["id"] for g in groups]),
                                               name="kube-focus")
        return [g["id"] for g in groups]

    async def _wait_idle(self) -> bool:
        waited = 0.0
        while self._inflight() or self._other_busy():
            if waited >= IDLE_WAIT_MAX_SECONDS:
                return False
            await self._sleep(IDLE_POLL_SECONDS)
            waited += IDLE_POLL_SECONDS
        return True

    async def _focus(self, ids: list[str]) -> None:
        """One run per group, one after another, after any other agent run has finished."""
        try:
            for n, gid in enumerate(ids):
                if n:
                    await self._sleep(self._stagger)
                if not await self._wait_idle():
                    log.warning("kube.focus_gave_up", reason="agents stayed busy")
                    return
                await self._focus_one(gid)
        except Exception as exc:  # noqa: BLE001 - a background pass must not leak
            log.warning("kube.focus_failed", error=type(exc).__name__)

    async def _focus_one(self, gid: str) -> None:
        settings = await self._settings.get()
        now = self._now()
        if not settings.get("auto_agent_enabled", True):
            return
        if await self._runs_today(now) >= settings["daily_run_cap"]:
            return
        groups = group_issues(await self._issues.list("open"))
        group = next((g for g in groups if g["id"] == gid), None)
        if group is None or group["mode"] == "known_down":
            return  # resolved or switched to known-down since the plan
        known = await self._diagnoses.all()
        keep = {g["id"] for g in groups}
        prev = known.get(gid) or {}
        await self._diagnoses.put(gid, {**prev, "status": "running", "run_at": now.isoformat(),
                                        "mode": group["mode"]}, now, keep)
        from backend.agents.runner.runner import AgentBusyError, AgentRunCapError
        run: Any = None
        error = ""
        try:
            run = await self._run_agent(AGENT_ID, trigger=TRIGGER,
                                        context=group_context(group, self._coverage_line()))
        except (AgentBusyError, AgentRunCapError) as exc:
            error = f"not started ({type(exc).__name__})"
        except Exception as exc:  # noqa: BLE001
            error = f"run failed ({type(exc).__name__})"
            log.warning("kube.agent_trigger_failed", error=type(exc).__name__)
        done = self._now()
        run_id = str(run.get("id")) if isinstance(run, dict) and run.get("id") else None
        output = str(run.get("output") or "") if isinstance(run, dict) else ""
        status = run.get("status") if isinstance(run, dict) else None
        if not error and status not in (None, "completed"):
            error = f"run {status}: {str((run or {}).get('error') or '')[:160]}"
        if not error and self._tool_events is not None and not used_evidence_tool(
                self._tool_events()):
            error = "no evidence: the run never called a describe, logs or events tool"
        if error:
            diag = {"status": "failed", "error": error, "summary": "", "evidence": [],
                    "confidence": "none"}
        else:
            diag = {"status": "done", **parse_diagnosis(output)}
        diag.update(run_id=run_id, run_at=now.isoformat(), mode=group["mode"],
                    issue_count=group["issue_count"], pod_count=group["pod_count"])
        await self._diagnoses.put(gid, diag, done, keep)
        for key in group["issue_keys"]:
            await self._issues.mark_agent_run(key, now)

    def _coverage_line(self) -> str | None:
        return (self.last or {}).get("coverage")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def get_monitor(app: Any) -> KubeMonitor | None:
    existing = getattr(app.state, "kube_monitor", None)
    if existing is not None:
        return existing
    from backend.agents.runner import get_runner
    from backend.infra.service import get_service as infra_service
    from backend.kube.collector import collect_snapshot
    from backend.kube.store import get_services, kubernetes_connector
    from backend.kube.waste import collect_waste
    services = get_services(app)
    runner = get_runner(app)
    registry = getattr(app.state, "repository_registry", None)
    if services is None or runner is None or registry is None:
        return None

    async def _connector() -> tuple[Any, str]:
        infra = infra_service(app)
        if infra is None:
            raise RuntimeError("infra unavailable")
        name = await kubernetes_connector(infra)
        if name is None:
            raise RuntimeError("no kubernetes connector")
        return infra, name

    async def collector() -> dict[str, Any]:
        infra, name = await _connector()
        return await collect_snapshot(infra, name)

    async def waste(prometheus_url: str | None) -> dict[str, Any]:
        infra, name = await _connector()
        return await collect_waste(infra, name, prometheus_url)

    async def create_task(payload: dict[str, Any]) -> Any:
        tasks = (getattr(app.state, "services", None) or {}).get("tasks")
        if tasks is None:
            raise RuntimeError("tasks unavailable")
        return await tasks.create(payload)

    monitor = KubeMonitor(
        registry.session_factory, collector, waste, runner.run_agent,
        settings_store=services.settings, issue_store=services.issues,
        report_store=services.reports, override_store=services.overrides,
        create_task=create_task, inflight=lambda: AGENT_ID in runner._inflight,
        other_busy=lambda: bool(runner._inflight - {AGENT_ID}),
        tool_events=lambda: runner.live.view(AGENT_ID).get("events") or [])
    app.state.kube_monitor = monitor
    return monitor
