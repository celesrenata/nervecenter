"""Hourly kube check and daily waste report (NS-ADR-120).

``KubeMonitor.tick`` is an AgentRunner tick hook and calls no model. It runs the deterministic
check at most once an hour and the waste report at most once a day (``kube_settings`` stores both
times). A check stores a snapshot, syncs the issue ledger and may start ONE ``kube_doctor`` run
per tick for issues that have not been looked at yet, within the cooldown, the daily cap and the
in-flight limits. Known-down namespaces never trigger anything.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select

from backend.kube.issues import IssueStore, ReportStore
from backend.kube.settings import KubeOverrideStore, KubeSettingsStore
from backend.persistence.models.agent_runner import AgentRun

log = structlog.get_logger(__name__)

AGENT_ID = "kube_doctor"
TRIGGER = "kube_issue"
CHECK_INTERVAL = timedelta(hours=1)
WASTE_INTERVAL = timedelta(hours=24)
MAX_DRAFT_TASKS = 5
MAX_CONTEXT_ISSUES = 5
DRAFT_MARK = "agent:kube_doctor"


class CheckBusy(Exception):
    """A check is already running."""


def _evidence_line(issue: dict[str, Any]) -> str:
    ev = issue.get("evidence") or {}
    bits = [f"{k}={str(v)[:80]}" for k, v in list(ev.items())[:4]
            if isinstance(v, (str, int, float, bool))]
    return ", ".join(bits)


def issue_context(issues: list[dict[str, Any]]) -> str:
    """Short data block for the agent run (data, not instructions)."""
    lines = [f"- key={i['key']} reason={i['reason']} mode={i['mode']} "
             f"evidence: {_evidence_line(i) or 'none'}" for i in issues[:MAX_CONTEXT_ISSUES]]
    return "New kube issues found by the hourly check:\n" + "\n".join(lines)


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
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self.last: dict[str, Any] | None = None

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
            summary["triggered"] = await self._trigger(synced, settings, started, summary)
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

    async def _trigger(self, synced: dict[str, Any], settings: dict[str, Any],
                       now: datetime, summary: dict[str, Any]) -> list[str] | None:
        """At most one run per tick. Candidates: new issues plus open ones never looked at
        (an earlier tick deferred them)."""
        if not settings.get("auto_agent_enabled", True):
            summary["warnings"].append("automatic kube_doctor runs are switched off")
            return None
        cooldown = timedelta(hours=settings["issue_cooldown_hours"])
        new_keys = {i["key"] for i in synced["new"]}
        candidates = []
        for issue in synced["open"]:  # known-down issues are not in "open"
            if issue["mode"] == "known_down" or issue["kind"] == "coverage":
                continue  # a blind check is for the owner; there is nothing to diagnose
            ran = issue.get("last_agent_run_at")
            if ran is not None and now - ran < cooldown:
                continue
            if issue["key"] in new_keys or ran is None:
                candidates.append(issue)
        if not candidates:
            return None
        if self._inflight():
            summary["warnings"].append("kube_doctor is running; new issues wait")
            return None
        if await self._runs_today(now) >= settings["daily_run_cap"]:
            summary["warnings"].append("daily kube_issue run cap reached; new issues wait")
            return None
        batch = candidates[:MAX_CONTEXT_ISSUES]
        from backend.agents.runner.runner import AgentBusyError, AgentRunCapError
        try:
            await self._run_agent(AGENT_ID, trigger=TRIGGER, context=issue_context(batch))
        except (AgentBusyError, AgentRunCapError) as exc:
            summary["warnings"].append(f"kube_doctor not started ({type(exc).__name__})")
            return None
        except Exception as exc:  # noqa: BLE001
            summary["warnings"].append(f"kube_doctor run failed ({type(exc).__name__})")
            log.warning("kube.agent_trigger_failed", error=type(exc).__name__)
            return None
        for issue in batch:
            await self._issues.mark_agent_run(issue["key"], now)
        return [i["key"] for i in batch]


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
        create_task=create_task, inflight=lambda: AGENT_ID in runner._inflight)
    app.state.kube_monitor = monitor
    return monitor
