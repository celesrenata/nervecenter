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
        return self._shrink({
            "status": row.get("status"), "approved": bool(row.get("approved")),
            "fingerprint": _clip(row.get("fingerprint"), 12), "approved_by": row.get("approved_by"),
            "approved_at": row.get("approved_at"), "target_count": len(row.get("targets") or []),
            "targets": [_clip(t, 40) for t in (row.get("targets") or [])[:60]],
            "out_of_scope_count": len(out_of_scope),
            "out_of_scope": [_clip(o.get("cidr"), 40) for o in out_of_scope[:15]],
            "note": "Only the approved allowlist may be scanned; the owner approves it.",
        }, ("targets", "out_of_scope"))

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

        targets = args.get("targets")
        if not isinstance(targets, list) or not targets or len(targets) > 16 \
                or not all(isinstance(t, str) and len(t) <= 64 for t in targets):
            raise NerveToolError("bad_arguments")
        ports = args.get("scan_tcp_ports")
        if ports is not None and not isinstance(ports, bool):
            raise NerveToolError("bad_arguments")
        try:
            row = await self._pentest(get_service).start(PENTEST_ACTOR, targets, ports)
        except ScanRefused as exc:
            raise NerveToolError(f"scan_refused_{exc.kind}") from None
        return {"scan_id": row.get("id"), "status": row.get("status"),
                "targets": row.get("targets"),
                "note": "Started. Poll pentest_scan_status; do not start another."}

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
