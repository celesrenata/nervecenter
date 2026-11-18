"""The ``nerve`` tool server: NerveCenter's own data for agents (agent-tuning).

In-process and read-only, with three narrow exceptions that follow the owner's
decisions: ``task_create``/``task_update`` write DRAFT tasks (the owner accepts
them; live tasks need ``NERVE_PLANNER_LIVE_TASKS=1``, capped per run), and
``repo_ask`` queues a read-only opencode handoff whose brief holds repo, ref
and question only (NS-ADR-121). Each agent type gets only the tools listed in
:data:`backend.agents.tools.policy.NERVE_AGENT_TOOLS`. Every result is sized to
fit the 6 000-character protocol cap and secrets are scrubbed.
"""
from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from backend.agents.tools import policy

log = structlog.get_logger(__name__)

RESULT_BUDGET = 5_200
MAX_LIST = 50
LIVE_TASKS_ENV = "NERVE_PLANNER_LIVE_TASKS"
MAX_TASKS_PER_RUN = 10
PLANNER_MARK = "agent:planner"
PENTEST_MARK = "agent:pentest"
PENTEST_ACTOR = "agent:pentest"
ES_URL_ENV = "NERVE_LOGS_ES_URL"
ES_USER_ENV = "NERVE_LOGS_ES_USER"
ES_PASSWORD_ENV = "NERVE_LOGS_ES_PASSWORD"
ES_INDEX_ENV = "NERVE_LOGS_ES_INDEX"
DEFAULT_ES_INDEX = "logs-*"
MAX_OPEN_HANDOFFS = 3
MAX_QUESTION = 1_500
RESOURCE_PAGE = 5_000


def task_mark(agent: Any) -> str:
    """Draft tasks carry the mark of the agent type that wrote them (planner by default)."""
    return PENTEST_MARK if str(getattr(agent, "type", "") or "") == "pentest" else PLANNER_MARK


class NerveToolError(Exception):
    """A refusal with a stable reason code (never free-form text from a backend)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _fit(rows: list[Any], budget: int = RESULT_BUDGET) -> dict[str, Any]:
    """Drop trailing rows until the JSON fits the budget; say how many were cut."""
    kept = list(rows)
    while kept and len(json.dumps(kept, default=str)) > budget:
        kept.pop()
    return {"count": len(rows), "shown": len(kept), "items": kept}


def _clip(value: Any, n: int) -> Any:
    text = str(value) if value is not None else None
    return text if text is None or len(text) <= n else text[: n - 1] + "…"


def _int(args: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    raw = args.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise NerveToolError("bad_arguments")
    try:
        return max(low, min(int(raw), high))
    except ValueError:
        raise NerveToolError("bad_arguments") from None


def _text(args: dict[str, Any], key: str, *, required: bool = False, limit: int = 500) -> str:
    raw = args.get(key)
    if raw is None or raw == "":
        if required:
            raise NerveToolError(f"missing_{key}")
        return ""
    if not isinstance(raw, str) or len(raw) > limit:
        raise NerveToolError("bad_arguments")
    return raw.strip()


class NerveTools:
    """Resolve services from the FastAPI app lazily and serve the tools."""

    def __init__(
        self, app: Any = None, *, es_transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._app = app
        self._es_transport = es_transport
        self._created: dict[str, int] = {}

    # -- service lookup ------------------------------------------------------------
    def _state(self) -> Any:
        return getattr(self._app, "state", None)

    def _service(self, name: str) -> Any:
        services = getattr(self._state(), "services", None)
        svc = services.get(name) if isinstance(services, dict) else None
        if svc is None:
            raise NerveToolError("unconfigured")
        return svc

    def _incidents(self) -> Any:
        services = getattr(self._state(), "services", None)
        svc = services.get("incidents") if isinstance(services, dict) else None
        if svc is not None:
            return svc
        engine = getattr(self._state(), "db_engine", None)
        if engine is None:
            raise NerveToolError("unconfigured")
        from backend.services.incident_agent import IncidentService

        return IncidentService(engine)

    # -- entry point -----------------------------------------------------------------
    async def call(self, agent: Any, tool: str, args: dict[str, Any], run_key: str = "") -> Any:
        handler = getattr(self, f"_t_{tool}", None)
        if handler is None:
            raise NerveToolError("tool_not_allowed")
        if tool in ("task_create", "task_update"):
            return await handler(agent, args, run_key)
        if tool in ("repo_ask", "resources_list", "resources_read", "pentest_scan_start"):
            return await handler(agent, args)
        return await handler(args)

    # -- tasks and goals -----------------------------------------------------------------
    @staticmethod
    def _task_row(task: Any) -> dict[str, Any]:
        return {
            "id": task.id, "title": _clip(task.title, 120), "status": task.status,
            "priority": task.priority, "goal_id": task.goal_id,
            "dependencies": list(task.dependencies or []),
            "origin": (task.inputs or {}).get("origin"),
        }

    async def _t_tasks_list(self, args: dict[str, Any]) -> Any:
        rows = await self._service("tasks").list_tasks(
            status=_text(args, "status", limit=32) or None,
            goal_id=_text(args, "goal_id", limit=64) or None,
            limit=_int(args, "limit", 30, 1, MAX_LIST),
        )
        return _fit([self._task_row(t) for t in rows])

    async def _t_goals_list(self, args: dict[str, Any]) -> Any:
        rows = await self._service("goals").list_goals(limit=_int(args, "limit", 30, 1, MAX_LIST))
        return _fit([
            {"id": g.id, "title": _clip(getattr(g, "title", None), 120),
             "status": getattr(g, "status", None), "progress": getattr(g, "progress", None)}
            for g in rows
        ])

    async def _t_task_create(self, agent: Any, args: dict[str, Any], run_key: str) -> Any:
        title = _text(args, "title", required=True, limit=200)
        live = os.environ.get(LIVE_TASKS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
        used = self._created.get(run_key, 0)
        if used >= MAX_TASKS_PER_RUN:
            raise NerveToolError("task_cap_reached")
        deps = args.get("dependencies") or []
        if not isinstance(deps, list) or len(deps) > 20 or not all(isinstance(d, str) for d in deps):
            raise NerveToolError("bad_arguments")
        payload = {
            "title": title,
            "description": _text(args, "description", limit=4_000) or None,
            "status": "pending" if live else "draft",
            "priority": _int(args, "priority", 3, 0, 10),
            "goal_id": _text(args, "goal_id", limit=64) or None,
            "dependencies": deps,
            "inputs": {"origin": task_mark(agent), "tag": task_mark(agent)},
        }
        task = await self._service("tasks").create(payload)
        self._created[run_key] = used + 1
        return {"task_id": task.id, "status": task.status,
                "note": "Draft: the owner must accept it before it runs."
                if task.status == "draft" else "Live task created."}

    async def _t_task_update(self, agent: Any, args: dict[str, Any], run_key: str) -> Any:
        svc = self._service("tasks")
        task_id = _text(args, "task_id", required=True, limit=64)
        current = await svc.get(task_id)
        if (current.inputs or {}).get("origin") != task_mark(agent) or current.status != "draft":
            raise NerveToolError("only_own_draft_tasks")
        changes: dict[str, Any] = {}
        for key, limit in (("title", 200), ("description", 4_000)):
            if key in args:
                changes[key] = _text(args, key, limit=limit) or None
        if "priority" in args:
            changes["priority"] = _int(args, "priority", 3, 0, 10)
        if "dependencies" in args:
            deps = args["dependencies"]
            if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
                raise NerveToolError("bad_arguments")
            changes["dependencies"] = deps[:20]
        if not changes:
            raise NerveToolError("nothing_to_update")
        task = await svc.update(task_id, changes)
        return {"task_id": task.id, "status": task.status}

    # -- resources (owner files linked to this agent; NS-ADR-125) --------------------------
    def _resources(self) -> Any:
        from backend.resources.service import get_service

        svc = get_service(self._app) if self._app is not None else None
        if svc is None:
            raise NerveToolError("unconfigured")
        return svc

    @staticmethod
    def _agent_key(agent: Any) -> str:
        return str(getattr(agent, "id", "") or getattr(agent, "type", "") or "")

    async def _t_resources_list(self, agent: Any, args: dict[str, Any]) -> Any:
        rows = await self._resources().get_links(self._agent_key(agent))
        return _fit([
            {"id": r["id"], "name": _clip(r["name"], 100), "content_type": r["content_type"],
             "size": r["size"]}
            for r in rows if r["sensitivity"] == "normal"
        ])

    async def _t_resources_read(self, agent: Any, args: dict[str, Any]) -> Any:
        from backend.resources.service import ResourceStoreUnavailable, is_text_type

        rid = _text(args, "resource_id", required=True, limit=64)
        offset = _int(args, "offset", 0, 0, 100_000_000)
        limit = _int(args, "limit", RESOURCE_PAGE, 1, RESOURCE_PAGE)
        try:
            view, data = await self._resources().read_linked(self._agent_key(agent), rid)
        except KeyError:
            raise NerveToolError("resource_not_linked") from None
        except ResourceStoreUnavailable:
            raise NerveToolError("unconfigured") from None
        if view["sensitivity"] != "normal":
            raise NerveToolError("resource_not_linked")
        if not is_text_type(view["content_type"]):
            raise NerveToolError("resource_not_text")
        text = data.decode("utf-8", errors="replace")
        page = text[offset: offset + limit]
        end = offset + len(page)
        return {"id": rid, "name": _clip(view["name"], 100), "offset": offset,
                "total_chars": len(text), "next_offset": end if end < len(text) else None,
                "text": page}

    # -- pentest digests (NS-ADR-125); scoring and scoping happen in the backend ---------------
    def _pentest(self, getter: Any) -> Any:
        svc = getter(self._app) if self._app is not None else None
        if svc is None:
            raise NerveToolError("unconfigured")
        return svc

    @staticmethod
    def _shrink(out: dict[str, Any], keys: tuple[str, ...], budget: int = RESULT_BUDGET) -> Any:
        """Drop trailing rows from the named lists until the JSON fits the budget."""
        while len(json.dumps(out, default=str)) > budget:
            key = max(keys, key=lambda k: len(out.get(k) or []))
            if not out.get(key):
                break
            out[key] = out[key][:-1]
            out["truncated"] = True
        return out

    async def _t_pentest_scope(self, args: dict[str, Any]) -> Any:
        from backend.pentest.store import get_service

        row = await self._pentest(get_service).current_allowlist()
        out_of_scope = row.get("out_of_scope") or []
        selected = row.get("selected_targets") or []
        return self._shrink({
            "status": row.get("status"), "approved": bool(row.get("approved")),
            "fingerprint": _clip(row.get("fingerprint"), 12), "approved_by": row.get("approved_by"),
            "approved_at": row.get("approved_at"), "target_count": len(row.get("targets") or []),
            "targets": [_clip(t, 40) for t in (row.get("targets") or [])[:60]],
            "selected_count": len(selected),
            "selected_targets": [_clip(t, 40) for t in selected[:60]],
            "out_of_scope_count": len(out_of_scope),
            "out_of_scope": [_clip(o.get("cidr"), 40) for o in out_of_scope[:15]],
            "note": "Only the owner's ticked, approved selected_targets may be scanned, "
                    "not the full computed targets list.",
        }, ("targets", "selected_targets", "out_of_scope"))

    async def _t_pentest_exposure(self, args: dict[str, Any]) -> Any:
        from backend.pentest.findings import get_store
        from backend.pentest.store import get_service

        limit = _int(args, "limit", 20, 1, MAX_LIST)
        overview = await self._pentest(get_service).overview()
        snap = overview.get("snapshot")
        by_tier: dict[str, int] = {}
        store = get_store(self._app) if self._app is not None else None
        if store is not None:
            for f in await store.list("open", None, 500, 0):
                by_tier[str(f.get("tier"))] = by_tier.get(str(f.get("tier")), 0) + 1
        risks = overview.get("risks") or []
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        risks = sorted(risks, key=lambda r: order.get(str(r.get("severity")), 5))
        return self._shrink({
            "firewall_imported": snap is not None,
            "pfsense_version": (snap or {}).get("pfsense_version"),
            "risk_count": len(risks),
            "risks": [{"rule_id": _clip(r.get("rule_id"), 40), "severity": r.get("severity"),
                       "title": _clip(r.get("title"), 120),
                       "evidence": _clip(r.get("evidence"), 160)}
                      for r in risks[:limit]],
            "internet_hosts": [
                {"frontend": _clip(h.get("frontend"), 40), "hosts": [_clip(x, 80) for x in
                                                                     (h.get("hosts") or [])[:6]],
                 "has_host_acl": h.get("has_host_acl"), "ssl": h.get("ssl")}
                for h in (overview.get("haproxy_hosts") or [])[:15]],
            "open_findings_by_tier": by_tier,
            "note": "" if snap else "No firewall config imported: exposure tiers are unknown.",
        }, ("risks", "internet_hosts"))

    async def _t_pentest_findings(self, args: dict[str, Any]) -> Any:
        from backend.pentest.findings import get_store

        status = _text(args, "status", limit=16) or "open"
        severity = _text(args, "severity", limit=16) or None
        limit = _int(args, "limit", 12, 1, 25)
        offset = _int(args, "offset", 0, 0, 10_000)
        rows = await self._pentest(get_store).list(status, severity, limit, offset)
        out = self._shrink({"offset": offset, "findings": [
            {"id": f["id"], "severity": f["severity"], "tier": f["tier"], "kind": f["kind"],
             "title": _clip(f["title"], 140), "subject": _clip(f["subject"], 100),
             "evidence": _clip(f["evidence"], 200), "advisory": f.get("advisory_id"),
             "labels": f.get("labels"), "sources": [_clip(x, 120) for x in f["sources"][:3]]}
            for f in rows]}, ("findings",))
        out["shown"] = len(out["findings"])
        out["next_offset"] = offset + out["shown"] if out["shown"] == limit else None
        return out

    async def _t_pentest_scan_start(self, agent: Any, args: dict[str, Any]) -> Any:
        from backend.pentest.scans import ScanRefused, get_service

        svc = self._pentest(get_service)
        targets = args.get("targets")
        slice_id = args.get("slice")
        if targets is None:
            if slice_id is not None and not (isinstance(slice_id, str) and len(slice_id) <= 32):
                raise NerveToolError("bad_arguments")
            targets = await svc.resolve_slice(slice_id or None)
            if not targets:
                raise NerveToolError("slice_not_found" if slice_id else "nothing_to_scan")
        elif slice_id is not None or not isinstance(targets, list) or not targets \
                or len(targets) > 16 \
                or not all(isinstance(t, str) and len(t) <= 64 for t in targets):
            raise NerveToolError("bad_arguments")
        ports = args.get("scan_tcp_ports")
        if ports is not None and not isinstance(ports, bool):
            raise NerveToolError("bad_arguments")
        try:
            row = await svc.start(PENTEST_ACTOR, targets, ports)
        except ScanRefused as exc:
            # cmap's own (redacted, short) rejection text is surfaced; other kinds stay coded.
            detail = f": {exc.reason[:240]}" if exc.kind == "cmap" else ""
            raise NerveToolError(f"scan_refused_{exc.kind}{detail}") from None
        return {"scan_id": row.get("id"), "status": row.get("status"),
                "targets": row.get("targets"),
                "note": "Started. Poll pentest_scan_status; do not start another."}

    async def _t_pentest_scan_plan(self, args: dict[str, Any]) -> Any:
        from backend.pentest.scans import get_service
        plan = await self._pentest(get_service).plan(10)
        return self._shrink(plan, ("slices",))
    async def _t_pentest_scan_status(self, args: dict[str, Any]) -> Any:
        from backend.pentest.scans import get_service

        svc = self._pentest(get_service)
        scan_id = _text(args, "scan_id", limit=64)
        keep = ("id", "status", "actor", "targets", "reason", "created_at", "finished_at")
        if scan_id:
            row = await svc.get(scan_id)
            if row is None:
                raise NerveToolError("not_found")
            return {k: row.get(k) for k in keep}
        rows = await svc.list(10)
        return self._shrink({"scans": [
            {k: (_clip(r.get(k), 160) if k == "reason" else r.get(k)) for k in keep}
            | {"targets": (r.get("targets") or [])[:4]} for r in rows],
            "limits": await svc.settings()}, ("scans",))

    # -- kube agent (NS-ADR-120): read-only; modes come from the backend, never the model ----------
    KUBE_KINDS = ("pod", "pvc", "pv", "node", "deploy", "statefulset", "daemonset", "job",
                  "cronjob", "svc", "ingress", "event", "volumes.longhorn.io", "certificate")
    KUBE_CLUSTER_KINDS = ("pv", "node")
    KUBE_MODE_NOTES = {
        "known_down": "known down on purpose: report only, never change",
        "diagnose_only": "protected namespace: report only, do not propose changes",
        "override_active": "protected namespace with an active owner override for this issue",
        "normal": "",
    }

    async def _kube(self) -> tuple[Any, Any, str]:
        from backend.infra.service import get_service as infra_service
        from backend.kube.store import get_services, kubernetes_connector
        if self._app is None:
            raise NerveToolError("unconfigured")
        kube, infra = get_services(self._app), infra_service(self._app)
        if kube is None or infra is None:
            raise NerveToolError("unconfigured")
        connector = await kubernetes_connector(infra)
        if connector is None:
            raise NerveToolError("no_kubernetes_connector")
        return kube, infra, connector

    async def _kube_mode(self, kube: Any, namespace: str, issue_key: str | None = None) -> str:
        from backend.kube.settings import classify_namespace
        verdict = classify_namespace(namespace, await kube.settings.get(),
                                     await kube.overrides.active(), issue_key)
        return "diagnose_only" if verdict == "protected" else verdict

    @staticmethod
    def _kube_age(row: dict[str, Any] | None, minutes: int) -> bool:
        stamp = row.get("created_at") if row else None
        return isinstance(stamp, datetime) and (
            datetime.now(UTC) - stamp < timedelta(minutes=minutes))

    KUBE_MAX_GROUPS = 10
    KUBE_MAX_NAMES = 4

    async def _kube_snapshot(self, kube: Any, infra: Any, connector: str
                             ) -> tuple[dict[str, Any], str]:
        from backend.kube.collector import collect_snapshot
        stored = await kube.reports.latest("snapshot")
        if self._kube_age(stored, 15):
            return dict(stored["data"]), "stored"
        snap = await collect_snapshot(infra, connector)
        await kube.reports.add("snapshot", snap)
        return snap, "fresh"

    async def _kube_issues(self, kube: Any, snap: dict[str, Any]) -> list[dict[str, Any]]:
        """Snapshot problems as ledger-style dicts with the namespace mode filled in."""
        from backend.kube.collector import snapshot_problems
        from backend.kube.settings import classify_namespace
        settings, overrides = await kube.settings.get(), await kube.overrides.active()
        skip = ("key", "kind", "namespace", "name", "reason", "severity")
        issues = []
        for p in snapshot_problems(snap):
            verdict = classify_namespace(p["namespace"], settings, overrides, p["key"])
            issues.append({
                "key": p["key"], "kind": p["kind"], "namespace": p["namespace"],
                "name": p["name"], "reason": p["reason"], "severity": p["severity"],
                "mode": "diagnose_only" if verdict == "protected" else verdict,
                "evidence": {k: v for k, v in p.items() if k not in skip}})
        return issues

    async def _t_kube_health(self, args: dict[str, Any]) -> Any:
        from backend.kube.groups import group_issues
        kube, infra, connector = await self._kube()
        snap, source = await self._kube_snapshot(kube, infra, connector)
        issues = await self._kube_issues(kube, snap)
        groups = group_issues(issues)
        diagnoses = await self._kube_diagnoses(kube)
        cov = snap.get("coverage") or {}
        coverage = {
            "line": cov.get("line") or "coverage unknown: snapshot has no coverage record",
            "percent": cov.get("percent"),
            "unreadable_namespaces": (cov.get("unreadable") or [])[:40],
            "not_read": cov.get("cluster_unreadable") or [],
            "note": "Anything listed as unreadable was NOT checked. No issue there does not mean "
                    "healthy; say so under Not checked."}
        head = {"source": source, "collected_at": snap.get("collected_at"), "coverage": coverage,
                "namespaces": snap.get("namespaces"), "issue_count": len(issues),
                "group_count": len(groups)}
        wanted = _text(args, "group", limit=140)
        if wanted:
            return self._kube_group_detail(head, wanted, groups, issues, snap, diagnoses)
        rows = []
        for g in groups:
            row = {"id": g["id"], "namespace": g["namespace"] or "_cluster",
                   "cause": g["cause"], "severity": g["severity"], "mode": g["mode"],
                   "issues": g["issue_count"], "pod_count": g["pod_count"],
                   "reasons": g["reasons"][:4], "workloads": g["workloads"][:4],
                   "pods": g["pods"][:3]}
            if g["pod_count"] > 3:
                row["pods_more"] = g["pod_count"] - 3
            if self.KUBE_MODE_NOTES.get(g["mode"]):
                row["note"] = self.KUBE_MODE_NOTES[g["mode"]]
            if diagnoses.get(g["id"], {}).get("summary"):
                row["last_diagnosis"] = _clip(diagnoses[g["id"]]["summary"], 120)
            rows.append(row)
        rows = rows[:self.KUBE_MAX_GROUPS]
        out = {**head, "groups": rows}
        while len(json.dumps(out, default=str)) > RESULT_BUDGET and len(rows) > 1:
            rows.pop()
        out["note"] = (f"{len(groups)} groups, showing top {len(rows)} by severity and blast "
                       "radius" + ("; expand one with kube_health {group: <id>} (exact pod "
                                   "names)" if groups else ""))
        events = [{k: _clip(e.get(k), 100) for k in ("namespace", "object", "reason", "message")}
                  | {"count": e.get("count")} for e in (snap.get("events") or [])[:4]]
        out["warning_events"] = events if len(json.dumps(out, default=str)) < 3_800 else []
        out["warnings"] = [_clip(w, 100) for w in (snap.get("warnings") or [])[:3]]
        return out

    def _kube_group_detail(self, head: dict[str, Any], wanted: str, groups: list[dict[str, Any]],
                           issues: list[dict[str, Any]], snap: dict[str, Any],
                           diagnoses: dict[str, Any]) -> Any:
        from backend.kube.groups import group_id
        group = next((g for g in groups if g["id"] == wanted), None)
        if group is None:
            raise NerveToolError("unknown_group")
        members = [i for i in issues if group_id(i) == wanted]
        rows = []
        for i in members:
            ev = i["evidence"]
            rows.append({
                "key": i["key"], "kind": i["kind"], "reason": i["reason"],
                "workload": i["name"], "pods": (ev.get("pods") or [])[:20],
                "container": ev.get("container"), "restarts": ev.get("restarts"),
                "last_termination": ev.get("last_termination"),
                "message": _clip(ev.get("message") or ev.get("scheduler_message"), 220)})
        events = [{k: _clip(e.get(k), 160) for k in ("object", "reason", "message")}
                  | {"count": e.get("count")} for e in (snap.get("events") or [])
                  if e.get("namespace") == group["namespace"]][:6]
        out = {**head, "group": {k: group[k] for k in (
            "id", "namespace", "cause", "severity", "mode", "issue_count", "pod_count")},
               "issues": rows, "warning_events": events}
        if self.KUBE_MODE_NOTES.get(group["mode"]):
            out["note"] = self.KUBE_MODE_NOTES[group["mode"]]
        total = len(rows)
        while len(json.dumps(out, default=str)) > RESULT_BUDGET and out["warning_events"]:
            out["warning_events"].pop()
        while len(json.dumps(out, default=str)) > RESULT_BUDGET and len(rows) > 1:
            rows.pop()
            out["truncated"] = f"{total - len(rows)} more issues not shown; use kube_describe"
        return out

    @staticmethod
    async def _kube_diagnoses(kube: Any) -> dict[str, Any]:
        from backend.kube.groups import DiagnosisStore
        return await DiagnosisStore(kube.reports).all()

    async def _kube_read(self, args: list[str]) -> str:
        from backend.infra.policy import redact
        _, infra, connector = await self._kube()
        res = await infra.run_read(connector, {"args": args})
        if not res.get("ok"):
            raise NerveToolError(str(res.get("denied_reason") or "read_failed")[:60])
        text = str(res.get("output") or "")
        return redact(text, limit=len(text) + 1)  # no cut here: callers cut and say so

    @staticmethod
    def _kube_name(args: dict[str, Any], key: str) -> str:
        value = _text(args, key, required=True, limit=253)
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?", value):
            raise NerveToolError(f"bad_{key}")
        return value

    def _kube_names(self, args: dict[str, Any], one: str, many: str) -> list[str]:
        """``one`` (a name) or ``many`` (up to 4 names); at least one is required."""
        raw = args.get(many)
        if raw is None:
            return [self._kube_name(args, one)]
        if not isinstance(raw, list) or not 1 <= len(raw) <= self.KUBE_MAX_NAMES:
            raise NerveToolError(f"{many}_1_to_{self.KUBE_MAX_NAMES}")
        names = [self._kube_name({one: n}, one) for n in raw]
        return list(dict.fromkeys(names))

    @staticmethod
    def _cut(text: str, budget: int, hint: str, keep: str = "tail") -> tuple[str, bool]:
        """Fit ``text`` in ``budget`` characters and say what was cut and how to narrow.

        Describe keeps the head and the Events tail; logs and events keep the newest lines.
        """
        if len(text) <= budget:
            return text, False
        lines = text.splitlines()
        head_n = 0
        if keep == "both":
            used = 0
            while head_n < len(lines) and used + len(lines[head_n]) + 1 <= budget // 3:
                used += len(lines[head_n]) + 1
                head_n += 1
        tail_budget = budget - sum(len(x) + 1 for x in lines[:head_n]) - 160
        tail_n = 0
        used = 0
        while tail_n < len(lines) - head_n and used + len(lines[-1 - tail_n]) + 1 <= tail_budget:
            used += len(lines[-1 - tail_n]) + 1
            tail_n += 1
        cut = len(lines) - head_n - tail_n
        marker = f"[truncated, {cut} more lines, narrow with {hint}]"
        tail = lines[len(lines) - tail_n:] if tail_n else []
        return "\n".join([*lines[:head_n], marker, *tail]), True

    async def _t_kube_describe(self, args: dict[str, Any]) -> Any:
        kind = _text(args, "kind", required=True, limit=40).lower()
        if kind not in self.KUBE_KINDS:
            raise NerveToolError("kind_not_allowed")
        names = self._kube_names(args, "name", "names")
        kube, _, _ = await self._kube()
        namespace = ""
        if kind not in self.KUBE_CLUSTER_KINDS:
            namespace = _text(args, "namespace", limit=63) or (
                "longhorn-system" if kind == "volumes.longhorn.io" else "")
            if not namespace:
                raise NerveToolError("namespace_required")
            self._kube_name({"namespace": namespace}, "namespace")
        mode = await self._kube_mode(kube, namespace) if namespace else "normal"
        budget = (RESULT_BUDGET - 400) // len(names)
        items = []
        for name in names:
            cmd = ["describe", kind, name] + (["-n", namespace] if namespace else [])
            try:
                text = await self._kube_read(cmd)
            except NerveToolError as exc:
                items.append({"name": name, "error": exc.reason})
                continue
            text, truncated = self._cut(
                text, budget, f"kube_events {{namespace, name: \"{name}\"}} or kube_logs",
                keep="both")  # the head and the Events section at the end
            items.append({"name": name, "truncated": truncated, "text": text})
        if len(names) == 1 and "text" in items[0]:
            out = {"kind": kind, "name": names[0], "namespace": namespace, "mode": mode,
                   "truncated": items[0]["truncated"], "text": items[0]["text"]}
        else:
            if len(names) == 1:
                raise NerveToolError(items[0]["error"])
            out = {"kind": kind, "namespace": namespace, "mode": mode, "items": items}
        if self.KUBE_MODE_NOTES.get(mode):
            out["note"] = self.KUBE_MODE_NOTES[mode]
        return out

    async def _t_kube_logs(self, args: dict[str, Any]) -> Any:
        namespace = self._kube_name(args, "namespace")
        pods = self._kube_names(args, "pod", "pods")
        tail = _int(args, "tail", 100, 1, 300)
        container = _text(args, "container", limit=63)
        previous = args.get("previous") is True
        kube, _, _ = await self._kube()
        mode = await self._kube_mode(kube, namespace)
        budget = (RESULT_BUDGET - 400) // len(pods)
        items = []
        for pod in pods:
            cmd = ["logs", pod, "-n", namespace, f"--tail={tail}"]
            if container:
                cmd += ["-c", self._kube_name({"container": container}, "container")]
            if previous:
                cmd.append("--previous")
            try:
                text = await self._kube_read(cmd)
            except NerveToolError as exc:
                items.append({"pod": pod, "error": exc.reason})
                continue
            text, truncated = self._cut(
                text, budget, "a smaller tail, a container, or previous=true")  # newest last
            items.append({"pod": pod, "truncated": truncated, "text": text})
        if len(pods) == 1:
            if "error" in items[0]:
                raise NerveToolError(items[0]["error"])
            out = {"pod": pods[0], "namespace": namespace, "container": container,
                   "previous": previous, "tail": tail, "mode": mode,
                   "truncated": items[0]["truncated"], "text": items[0]["text"]}
        else:
            out = {"namespace": namespace, "container": container, "previous": previous,
                   "tail": tail, "mode": mode, "items": items}
        if self.KUBE_MODE_NOTES.get(mode):
            out["note"] = self.KUBE_MODE_NOTES[mode]
        return out

    async def _t_kube_events(self, args: dict[str, Any]) -> Any:
        """Events of one object (``name``) or the warnings of a namespace; newest last."""
        namespace = self._kube_name(args, "namespace")
        name = _text(args, "name", limit=253)
        cmd = ["get", "events", "-n", namespace, "--sort-by=.lastTimestamp"]
        if name:
            cmd.append(f"--field-selector=involvedObject.name={self._kube_name(args, 'name')}")
        else:
            cmd.append("--field-selector=type=Warning")
        kube, _, _ = await self._kube()
        text, truncated = self._cut(
            await self._kube_read(cmd), RESULT_BUDGET - 400,
            "a name: kube_events {namespace, name}")
        mode = await self._kube_mode(kube, namespace)
        out = {"namespace": namespace, "name": name, "mode": mode, "truncated": truncated,
               "text": text}
        if self.KUBE_MODE_NOTES.get(mode):
            out["note"] = self.KUBE_MODE_NOTES[mode]
        return out

    async def _t_kube_waste(self, args: dict[str, Any]) -> Any:
        from backend.kube.waste import collect_waste
        kube, infra, connector = await self._kube()
        stored = await kube.reports.latest("waste")
        if self._kube_age(stored, 26 * 60):
            report, source = dict(stored["data"]), "stored"
        else:
            url = (await kube.settings.get()).get("prometheus_url")
            report, source = await collect_waste(infra, connector, url), "fresh"
            await kube.reports.add("waste", report)
        sections = ("idle_pods", "oversized_pods", "unused_pvcs", "orphaned_pvs",
                    "restarting_pods", "crash_loop_burn", "clean_exit_churn")
        while len(json.dumps(report, default=str)) > RESULT_BUDGET:
            key = max(sections, key=lambda k: len((report.get(k) or {}).get("items") or []))
            items = (report.get(key) or {}).get("items") or []
            if not items:
                break
            report[key] = {**report[key], "items": items[:-1]}
            report["truncated"] = True
        return {"source": source, **report}

    async def _t_kube_known_down(self, args: dict[str, Any]) -> Any:
        from backend.kube.settings import effective
        kube, _, _ = await self._kube()
        settings = await kube.settings.get()
        overrides = [{"issue_key": o["issue_key"], "namespace": o["namespace"],
                      "expires_at": o["expires_at"].isoformat()
                      if hasattr(o["expires_at"], "isoformat") else o["expires_at"]}
                     for o in await kube.overrides.active()]
        first_run = bool(settings.get("diagnose_only_until_first_report", True))
        return {
            "known_down": effective(settings, "known_down"),
            "protected_namespaces": effective(settings, "protected_namespaces"),
            "switched_off": {"known_down": settings.get("known_down_off") or [],
                             "protected": settings.get("protected_off") or []},
            "active_overrides": overrides[:20], "override_hours": settings["override_hours"],
            "first_run": first_run,
            "note": ("first_run is true: diagnose only, do not call propose_plan" if first_run
                     else "Known-down namespaces are never changed; protected namespaces only "
                          "for an issue listed in active_overrides."),
        }

    async def _t_kube_runbook(self, args: dict[str, Any]) -> Any:
        from backend.kube import skeleton
        from backend.kube.runbooks import new_stamp
        kube, infra, connector = await self._kube()
        runbook = {k: v for k, v in args.items() if k != "issue_key"}
        runbook.setdefault("stamp", new_stamp())
        if "dbtype" in runbook and runbook.get("kind") is None:
            runbook["kind"] = "backup"
        ssh = await self._kube_ssh(infra, runbook.pop("ssh_connector", None))
        settings = await kube.settings.get()
        try:
            steps = skeleton.build_steps(runbook, settings, connector, ssh)
        except skeleton.RunbookError as exc:
            raise NerveToolError(f"runbook_invalid: {exc}"[:120]) from None
        if ssh:
            runbook["ssh_connector"] = ssh
        out: dict[str, Any] = {
            "issue_key": _text(args, "issue_key", limit=300), "connector": connector,
            "runbook": runbook, "step_count": len(steps),
            "steps": skeleton.summarize(steps),
            "use": "In propose_plan pass {\"runbook\": <the runbook object above>, \"title\", "
                   "\"summary\", \"issue_key\", \"verify\", \"rollback\"}; the system fills in "
                   "the step manifests. Extra steps go in \"steps\" and get indexes after these.",
        }
        if not ssh and any("mkdir" in str(s.get("command", "")) or s.get("kind") == "backup"
                           for s in steps):
            out["backup_directory"] = "No ssh connector was named: ask the owner to create the " \
                "backups directory on the NAS before the backup steps run."
        return self._shrink(out, ("steps",))

    async def _kube_ssh(self, infra: Any, name: Any) -> str | None:
        if not isinstance(name, str) or not name:
            return None
        found = [c for c in await infra.list_connectors()
                 if c.get("kind") == "ssh" and c.get("name") == name]
        return name if found else None

    async def kube_first_run(self) -> bool:
        """True while the owner has not enabled proposals (also when settings are unreadable)."""
        try:
            kube, _, _ = await self._kube()
            return bool((await kube.settings.get()).get("diagnose_only_until_first_report", True))
        except NerveToolError:
            return True

    async def expand_runbook(self, runbook: Any) -> list[dict[str, Any]]:
        """Steps of a ``runbook`` object in ``propose_plan`` (same arguments as kube_runbook)."""
        from backend.kube import skeleton
        kube, infra, connector = await self._kube()
        spec = dict(runbook) if isinstance(runbook, dict) else {}
        ssh = await self._kube_ssh(infra, spec.pop("ssh_connector", None))
        try:
            return skeleton.build_steps(spec, await kube.settings.get(), connector, ssh)
        except skeleton.RunbookError as exc:
            raise NerveToolError(f"runbook_invalid: {exc}"[:120]) from None

    # -- incidents, events, changes ----------------------------------------------------------
    async def _t_incidents_list(self, args: dict[str, Any]) -> Any:
        rows = await self._incidents().list_incidents()
        limit = _int(args, "limit", 20, 1, MAX_LIST)
        keep = ("id", "title", "status", "severity", "created_at", "updated_at")
        return _fit([
            {k: _clip(r.get(k), 160) for k in keep if k in r} for r in rows[:limit]
        ])

    async def _t_incident_get(self, args: dict[str, Any]) -> Any:
        row = await self._incidents().get(_text(args, "incident_id", required=True, limit=64))
        text = json.dumps(row, default=str)
        return json.loads(text) if len(text) <= RESULT_BUDGET else {"truncated": text[:RESULT_BUDGET]}

    async def _t_events_search(self, args: dict[str, Any]) -> Any:
        from backend.events.query import list_events

        nats = getattr(self._state(), "nats_client", None)
        if nats is None:
            raise NerveToolError("unconfigured")
        entity = _text(args, "entity", limit=200) or None
        try:
            result = await list_events(nats, entity=entity, limit=_int(args, "limit", 30, 1, 100))
        except Exception as exc:  # noqa: BLE001 - bus errors become a reason code
            log.warning("nerve.events_failed", error=type(exc).__name__)
            raise NerveToolError("event_bus_unavailable") from None
        rows = [e.model_dump(mode="json") for e in result.events]
        slim = [{k: _clip(v, 160) for k, v in r.items() if k in
                 ("id", "type", "subject", "occurred_at", "created_at", "source", "payload")}
                for r in rows]
        out = _fit(slim)
        out["total_in_stream"] = result.total
        return out

    async def _t_changes_list(self, args: dict[str, Any]) -> Any:
        svc = self._service("orchestration")
        for name in ("get_all_changes", "list_changes"):
            fn = getattr(svc, name, None)
            if callable(fn):
                rows = fn()
                if hasattr(rows, "__await__"):
                    rows = await rows
                limit = _int(args, "limit", 30, 1, MAX_LIST)
                items = [r.model_dump(mode="json") if hasattr(r, "model_dump") else r
                         for r in list(rows)[:limit]]
                return _fit([{k: _clip(v, 160) for k, v in dict(i).items()} for i in items])
        raise NerveToolError("unconfigured")

    # -- logs -------------------------------------------------------------------------------------
    async def _t_app_logs(self, args: dict[str, Any]) -> Any:
        from backend.observability.log_buffer import get_log_buffer

        limit = _int(args, "limit", 40, 1, 200)
        needle = _text(args, "contains", limit=120).lower()
        entries = get_log_buffer().get_entries(_text(args, "min_level", limit=16), 1_000)
        rows = [e.as_dict() for e in entries]
        if needle:
            rows = [r for r in rows if needle in json.dumps(r, default=str).lower()]
        return _fit([{k: _clip(v, 240) for k, v in r.items()} for r in rows[:limit]])

    async def _t_logs_search(self, args: dict[str, Any]) -> Any:
        base = os.environ.get(ES_URL_ENV, "").strip().rstrip("/")
        if not base:
            raise NerveToolError("unconfigured")
        from backend.observability.log_buffer import _scrub

        query = _text(args, "query", required=True, limit=300)
        minutes = _int(args, "since_minutes", 60, 1, 60 * 24 * 7)
        size = _int(args, "limit", 30, 1, 100)
        since = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
        body = {
            "size": size,
            "sort": [{"@timestamp": "desc"}],
            "_source": ["@timestamp", "log", "message", "kubernetes.namespace_name",
                        "kubernetes.pod_name"],
            "query": {"bool": {"filter": [
                {"range": {"@timestamp": {"gte": since}}},
                {"query_string": {"query": query, "default_operator": "AND"}},
            ]}},
        }
        index = os.environ.get(ES_INDEX_ENV, DEFAULT_ES_INDEX).strip() or DEFAULT_ES_INDEX
        user = os.environ.get(ES_USER_ENV, "")
        auth = (user, os.environ.get(ES_PASSWORD_ENV, "")) if user else None
        try:
            async with httpx.AsyncClient(
                transport=self._es_transport, timeout=15.0, auth=auth
            ) as client:
                resp = await client.post(f"{base}/{index}/_search", json=body)
        except httpx.HTTPError:
            raise NerveToolError("log_store_unreachable") from None
        if resp.status_code != 200:
            raise NerveToolError(f"log_store_http_{resp.status_code}")
        hits = resp.json().get("hits", {})
        rows = []
        for hit in hits.get("hits", []):
            src = hit.get("_source", {})
            kube = src.get("kubernetes", {}) if isinstance(src.get("kubernetes"), dict) else {}
            rows.append({
                "t": src.get("@timestamp"), "ns": kube.get("namespace_name"),
                "pod": kube.get("pod_name"),
                "msg": _scrub(_clip(src.get("log") or src.get("message"), 300) or ""),
            })
        out = _fit(rows)
        total = hits.get("total", {})
        out["total_matches"] = total.get("value") if isinstance(total, dict) else total
        return out

    # -- evidence, systems --------------------------------------------------------------------------
    async def _t_evidence_search(self, args: dict[str, Any]) -> Any:
        svc = self._service("evidence")
        needle = _text(args, "contains", limit=120).lower()
        source = _text(args, "source_type", limit=64)
        rows = await svc.list_evidence(limit=200)
        out = []
        for ev in rows:
            if source and ev.source_type != source:
                continue
            blob = f"{ev.source_ref} {ev.content or ''}".lower()
            if needle and needle not in blob:
                continue
            out.append({"id": ev.id, "source_type": ev.source_type,
                        "source_ref": _clip(ev.source_ref, 160), "trust": ev.trust,
                        "captured_at": ev.captured_at.isoformat(),
                        "content": _clip(ev.content, 240)})
        return _fit(out[: _int(args, "limit", 20, 1, MAX_LIST)])

    async def _t_evidence_get(self, args: dict[str, Any]) -> Any:
        ev = await self._service("evidence").get(_text(args, "evidence_id", required=True, limit=64))
        if ev is None:
            raise NerveToolError("not_found")
        intact = None
        if ev.content_hash and ev.content is not None:
            intact = await self._service("evidence").verify_content(ev.id, ev.content_hash)
        return {"id": ev.id, "source_type": ev.source_type, "source_ref": _clip(ev.source_ref, 200),
                "trust": ev.trust, "captured_at": ev.captured_at.isoformat(),
                "content_hash_matches": intact, "content": _clip(ev.content, 3_500)}

    async def _t_systems_status(self, args: dict[str, Any]) -> Any:
        return _fit(await self._service("systems").list_systems())

    # -- repositories through opencode (read-only handoff, NS-ADR-121) ---------------------------------
    async def _handoffs(self) -> Any:
        from backend.handoffs.service import get_service

        svc = get_service(self._app) if self._app is not None else None
        if svc is None:
            raise NerveToolError("unconfigured")
        return svc

    async def _t_repo_ask(self, agent: Any, args: dict[str, Any]) -> Any:
        from backend.handoffs.service import REPO_RE, HandoffInputError

        repo = _text(args, "repo", required=True, limit=200)
        ref = _text(args, "ref", limit=100) or "the default branch"
        question = _text(args, "question", required=True, limit=MAX_QUESTION)
        if not REPO_RE.match(repo) or ".." in repo:
            raise NerveToolError("bad_repo")
        if policy.has_personal_data({"q": question, "repo": repo, "ref": ref}):
            raise NerveToolError("personal_data_in_brief")
        svc = await self._handoffs()
        who = f"agent:{getattr(agent, 'id', '-')}"
        open_rows = [h for st in ("queued", "claimed") for h in await svc.list(status=st)
                     if h.get("requested_by") == who]
        if len(open_rows) >= MAX_OPEN_HANDOFFS:
            raise NerveToolError("too_many_open_handoffs")
        brief = (
            f"READ-ONLY question about the repository '{repo}' at {ref}. Do not modify, "
            "commit or push anything and do not run the project's code. Answer from the "
            "files and git history, quoting file paths and line ranges, in at most 6000 "
            f"characters of Markdown.\n\nQuestion: {question}"
        )
        try:
            row = await svc.enqueue(
                target="opencode", title=f"Read-only: {repo}: {question}"[:200], brief=brief,
                repo=repo, requested_by=who, runner="central",
            )
        except HandoffInputError:
            raise NerveToolError("bad_arguments") from None
        return {"handoff_id": row["id"], "status": row["status"],
                "note": "Queued for central opencode. Call repo_result later; "
                        "it can take several minutes."}

    async def _t_repo_result(self, args: dict[str, Any]) -> Any:
        from backend.handoffs.service import HandoffNotFoundError

        svc = await self._handoffs()
        try:
            row = await svc.get(_text(args, "handoff_id", required=True, limit=64))
        except HandoffNotFoundError:
            raise NerveToolError("not_found") from None
        if not str(row.get("requested_by", "")).startswith("agent:"):
            raise NerveToolError("not_found")
        return {"handoff_id": row["id"], "status": row["status"],
                "result": _clip(row.get("result_summary"), RESULT_BUDGET - 200),
                "error": _clip(row.get("error"), 300)}
