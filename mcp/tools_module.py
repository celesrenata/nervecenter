"""§45 tool catalogue and construction.

``ALL_TOOLS`` holds the spec §45 tool list (18 tools) plus the R112
personal-operations tools (11 more, 29 total) as plain schema entries (name,
description, JSON-schema input, plus the §46 API operation each one proxies).
``build_tools()`` turns that catalogue into :class:`mcp.tools.Tool` instances
bound to a §46 API base URL (argument → ``NERVE_API`` env →
``http://localhost:8000``); every proxied call carries a fresh
``Nerve-Trace-Id`` (spec §29) via :class:`mcp.api.NerveAPI`.

POSTs the backend does not implement yet (create_goal, update_goal,
create_task, report_claim, report_artifact, create_planned_change) are still
proxied: the API is dependency-tolerant and answers 2xx — typically 202 with
``request_id`` + ``timestamp`` — and any 2xx is treated as success.

R112 personal tools: the eleven new tools proxy the personal-operations
routes on the same §46 API (attention, briefing, people, conversations,
communications, waiting-on, followups, drafts). ``create_followup`` and
``draft_reply`` create local records only — they never send anything to any
channel. ``get_commitments`` targets the canonical ``GET /api/commitments``
route (spec §52) which the backend has not shipped yet; it is kept as an
honest contract descriptor, not a guessed route.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.api import DEFAULT_BASE_URL, ContractError, NerveAPI
from mcp.tools import Tool

Handler = Callable[[dict[str, Any], NerveAPI | None], Awaitable[Any]]


def resolve_base_url(base_url: str | None = None) -> str:
    """Base URL for the §46 API: argument → ``NERVE_API`` env → localhost default."""
    if base_url:
        return base_url
    return os.environ.get("NERVE_API") or DEFAULT_BASE_URL


def _string_arg(description: str) -> dict[str, Any]:
    """One string property spec for a JSON-schema input object."""
    return {"type": "string", "minLength": 1, "description": description}


def _id_arg(description: str) -> dict[str, Any]:
    """One id string property spec (loose identifier pattern)."""
    spec = _string_arg(description)
    spec["pattern"] = "^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    return spec


def _object_arg(description: str) -> dict[str, Any]:
    """One free-form JSON-object property spec (payload)."""
    return {"type": "object", "additionalProperties": True, "description": description}


async def _proxy(
    api: NerveAPI,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """Proxy one tool call to the §46 API; any 2xx (incl. 202) is success."""
    if method == "GET":
        return await api.get(path, params=params)
    return await api.post(path, json_body=body if body is not None else {})


async def _entity_timeline(args: dict[str, Any], api: NerveAPI) -> Any:
    """GET /api/events?entity=<id> — the event timeline of one entity."""
    return await _proxy(api, "GET", "/api/events", params={"entity": args["id"]})


async def _compile_context(args: dict[str, Any], api: NerveAPI) -> Any:
    """POST /api/context/compile, falling back to GET /api/context on 405.

    The live API only guarantees ``GET /api/context`` and ``POST
    /api/context/ask``; the compile endpoint is the newer contract.
    """
    try:
        return await api.post("/api/context/compile", json_body=dict(args))
    except ContractError as exc:
        if exc.status != 405:
            raise
        return await api.get("/api/context")


def _plain_handler(method: str, path: str) -> Handler:
    """Executor that proxies straight to one §46 endpoint.

    Argument routing:
    - path ``{id}``: the ``id`` argument fills the path; for POSTs the
      ``body`` argument (if present) is the JSON body, else the whole args
      dict.
    - other GETs: every argument becomes a query parameter
      (e.g. ``GET /api/search?q=...``, ``GET /api/events?entity=<id>``).
    - other POSTs: the whole args dict is the JSON body.
    """

    async def handler(args: dict[str, Any], api: NerveAPI) -> Any:
        if "{id}" in path:
            filled = path.format(id=args["id"])
            if method == "GET":
                return await _proxy(api, method, filled)
            body = args.get("body")
            payload = dict(body) if body is not None else dict(args)
            return await _proxy(api, method, filled, body=payload)
        if method == "GET":
            return await _proxy(api, method, path, params=dict(args))
        return await _proxy(api, method, path, body=dict(args))

    return handler


def _make_handler(method: str, path: str, kind: str, base_url: str) -> Handler:
    """Bind an executor to the resolved §46 base URL (used when no API is injected)."""

    if kind == "context":
        runner: Handler = _compile_context
    elif kind == "timeline":
        runner = _entity_timeline
    elif kind == "plain":
        runner = _plain_handler(method, path)
    else:
        raise ValueError(f"unknown handler kind: {kind!r}")

    async def handler(args: dict[str, Any], api: NerveAPI | None) -> Any:
        if api is None:
            async with NerveAPI(base_url) as bound:
                return await runner(args, bound)
        return await runner(args, api)

    return handler


# name, description, properties, required, §46 method, §46 path, handler kind
_SPEC: list[tuple[str, str, dict[str, Any], list[str], str, str, str]] = [
    (
        "search_entities",
        "Search NerveCenter knowledge (entities, events, evidence) with a free-text query "
        "(§46 GET /api/search).",
        {"q": _string_arg("Free-text search query")},
        ["q"],
        "GET",
        "/api/search",
        "plain",
    ),
    (
        "get_entity",
        "Fetch a single entity by id from the knowledge plane (§46 GET /api/entities/{id}).",
        {"id": _id_arg("Entity id")},
        ["id"],
        "GET",
        "/api/entities/{id}",
        "plain",
    ),
    (
        "get_entity_timeline",
        "List the event timeline for one entity (§46 GET /api/events?entity={id}).",
        {"id": _id_arg("Entity id (sent as the ?entity= query parameter)")},
        ["id"],
        "GET",
        "/api/events",
        "timeline",
    ),
    (
        "get_recent_changes",
        "List recent change records across the knowledge plane (§46 GET /api/changes).",
        {},
        [],
        "GET",
        "/api/changes",
        "plain",
    ),
    (
        "get_goal",
        "Fetch one persistent goal by id (§46 GET /api/goals/{id}).",
        {"id": _id_arg("Goal id")},
        ["id"],
        "GET",
        "/api/goals/{id}",
        "plain",
    ),
    (
        "create_goal",
        "Create a persistent goal (spec §4.5). Proxies §46 POST /api/goals; a 202 accept is "
        "success.",
        {
            "name": _string_arg("Goal name"),
            "body": _object_arg(
                "Optional extra goal fields (description, criteria, ...) merged into the POST body"
            ),
        },
        ["name"],
        "POST",
        "/api/goals",
        "plain",
    ),
    (
        "update_goal",
        "Update an existing goal. Proxies §46 POST /api/goals/{id}; a 202 accept is success.",
        {
            "id": _id_arg("Goal id"),
            "body": _object_arg("Goal fields to update (status, progress, criteria, ...)"),
        },
        ["id"],
        "POST",
        "/api/goals/{id}",
        "plain",
    ),
    (
        "get_task",
        "Fetch one task from the execution plane by id (§46 GET /api/tasks/{id}).",
        {"id": _id_arg("Task id")},
        ["id"],
        "GET",
        "/api/tasks/{id}",
        "plain",
    ),
    (
        "create_task",
        "Create a task in the task DAG. Proxies §46 POST /api/tasks; a 202 accept is success.",
        {
            "name": _string_arg("Task name"),
            "body": _object_arg(
                "Optional extra task fields (goal_id, dependencies, ...) merged into the POST body"
            ),
        },
        ["name"],
        "POST",
        "/api/tasks",
        "plain",
    ),
    (
        "report_claim",
        "Report a claim (an evidence-backed assertion, spec §4.3). Proxies §46 POST /api/claims; "
        "a 202 accept is success.",
        {"body": _object_arg("Claim payload (statement, type, confidence, evidence, ...)")},
        [],
        "POST",
        "/api/claims",
        "plain",
    ),
    (
        "report_artifact",
        "Report an artifact / evidence record. Proxies §46 POST /api/evidence; a 202 accept is success",  # noqa: E501
        {"body": _object_arg("Artifact payload (kind, uri, hash, ...)")},
        [],
        "POST",
        "/api/evidence",
        "plain",
    ),
    (
        "compile_context",
        "Compile a context package (entities, events, evidence, claims) for a focus area.",
        {
            "question": _string_arg("Question or focus area to compile context for"),
            "body": _object_arg(
                "Optional compile options (entity ids, recency window, max tokens), the POST body"
            ),
        },
        [],
        "POST",
        "/api/context/compile",
        "context",
    ),
    (
        "get_compute",
        "Show compute fabric state: GPUs, hosts, utilization (§46 GET /api/compute).",
        {},
        [],
        "GET",
        "/api/compute",
        "plain",
    ),
    (
        "get_models",
        "List known inference models and their observed state (§46 GET /api/models).",
        {},
        [],
        "GET",
        "/api/models",
        "plain",
    ),
    (
        "get_route_history",
        "List recent OmniRoute routing decisions (§46 GET /api/routes).",
        {},
        [],
        "GET",
        "/api/routes",
        "plain",
    ),
    (
        "get_incident",
        "Fetch one incident and its investigation state by id (§46 GET /api/incidents/{id}).",
        {"id": _id_arg("Incident id")},
        ["id"],
        "GET",
        "/api/incidents/{id}",
        "plain",
    ),
    (
        "start_investigation",
        "Start (or resume) an investigation for an incident. Proxies §46 POST "
        "/api/incidents/investigate; a 202 accept is success.",
        {
            "id": _id_arg("Incident id"),
            "body": _object_arg("Optional investigation options (focus, constraints, ...)"),
        },
        ["id"],
        "POST",
        "/api/incidents/investigate",
        "plain",
    ),
    (
        "create_planned_change",
        "Create a planned change record. Proxies §46 POST /api/changes; a 202 accept is success.",
        {"body": _object_arg("Planned change payload (what/why/impact, rollback plan, ...)")},
        [],
        "POST",
        "/api/changes",
        "plain",
    ),
    # ---- R112 personal-operations tools (11) ----------------------------
    (
        "get_attention",
        "List the ranked attention queue with each item's priority and 'why' "
        "(optionally filtered by state) (§46 GET /api/attention).",
        {
            "state": _string_arg(
                "Optional lifecycle state filter: NOW|TODAY|SOON|WAITING_ON|FYI|SUPPRESSED|RESOLVED"
            ),
            "limit": {"type": "integer", "minimum": 1, "description": "Optional page size (default 50)"},
        },
        [],
        "GET",
        "/api/attention",
        "plain",
    ),
    (
        "get_daily_brief",
        "Compose one daily briefing over the personal-operations planes; the "
        "morning brief is the full §30 daily brief (midday/evening are check-ins) "
        "(§46 GET /api/briefing).",
        {"kind": _string_arg("Briefing slot: morning | midday | evening (default morning)")},
        [],
        "GET",
        "/api/briefing",
        "plain",
    ),
    (
        "get_person",
        "Fetch one canonical person with its normalized identity links (§46 GET /api/people/{id}).",
        {"id": _id_arg("Person id")},
        ["id"],
        "GET",
        "/api/people/{id}",
        "plain",
    ),
    (
        "get_conversation",
        "Fetch one cross-channel conversation with its communications in chronological "
        "order (normalized records: refs only, no raw content; §46 GET /api/conversations/{id}).",
        {"id": _id_arg("Conversation id")},
        ["id"],
        "GET",
        "/api/conversations/{id}",
        "plain",
    ),
    (
        "search_communications",
        "List/search normalized cross-channel communications, optionally filtered by "
        "person and channel (chronological, refs only — §46 GET /api/communications).",
        {
            "person_id": _id_arg("Optional person id to filter by"),
            "channel": _string_arg("Optional channel: email|imessage|sms|rcs|voicemail"),
        },
        [],
        "GET",
        "/api/communications",
        "plain",
    ),
    (
        "get_commitments",
        "List open commitments — 'what did I promise people?' (spec §52; canonical "
        "§46 GET /api/commitments). CONTRACT BLOCKER: the backend has not shipped this "
        "route yet, so the call answers 404 until it does.",
        {},
        [],
        "GET",
        "/api/commitments",
        "plain",
    ),
    (
        "get_waiting_on",
        "List outgoing requests that went unanswered, most-waiting first, optionally "
        "filtered by status (open|resolved|cancelled|all; default open) "
        "(§46 GET /api/waiting-on).",
        {"status": _string_arg("Optional status filter: open|resolved|cancelled|all")},
        [],
        "GET",
        "/api/waiting-on",
        "plain",
    ),
    (
        "create_followup",
        "Create a tracked follow-up (R104 §57): target_kind + target_id name the "
        "originating record (e.g. waiting_on + request id, commitment + finding id). "
        "Proxies §46 POST /api/followups; only creates a local record — sends nothing.",
        {
            "target_kind": _string_arg("Originating record kind (waiting_on, commitment, ...)"),
            "target_id": _string_arg("Originating record id"),
            "reason": _string_arg("Why this follow-up must happen"),
            "person_id": _id_arg("Optional person involved"),
            "due_at": _string_arg("Optional due instant (ISO-8601)"),
        },
        ["target_kind", "target_id", "reason"],
        "POST",
        "/api/followups",
        "plain",
    ),
    (
        "snooze_attention",
        "Defer one attention item to a later window (it stays in the live queue) "
        "(§46 POST /api/attention/{id}/snooze).",
        {
            "id": _id_arg("Attention item id"),
            "until": _string_arg("Optional ISO-8601 instant to wake at"),
        },
        ["id"],
        "POST",
        "/api/attention/{id}/snooze",
        "plain",
    ),
    (
        "resolve_attention",
        "Mark one attention item RESOLVED (handled) (§46 POST /api/attention/{id}/resolve).",
        {"id": _id_arg("Attention item id")},
        ["id"],
        "POST",
        "/api/attention/{id}/resolve",
        "plain",
    ),
    (
        "draft_reply",
        "Create a local reply DRAFT for a conversation (channel + text). The drafts API "
        "only ever creates DRAFT state — it never sends anything to any channel "
        "(sending happens only in the relay). Proxies §46 POST /api/drafts.",
        {
            "channel": _string_arg("Channel: email|imessage|sms|rcs|voicemail"),
            "text": _string_arg("Draft body (local-only, human-reviewed)"),
            "conversation_id": _id_arg("Optional conversation the draft belongs to"),
            "communication_ref": _string_arg("Optional communication ref being replied to"),
            "basis": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: why this draft exists",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: communication / evidence refs backing the draft",
            },
        },
        ["channel", "text"],
        "POST",
        "/api/drafts",
        "plain",
    ),
]


ALL_TOOLS: list[dict[str, Any]] = [
    {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": properties, "required": required},
        "method": method,
        "path": path,
    }
    for name, description, properties, required, method, path, _kind in _SPEC
]


def build_tools(base_url: str | None = None) -> list[Tool]:
    """Build the 29 :class:`Tool` instances (18 §45 + 11 R112 personal) bound to a §46 API base URL."""
    resolved = resolve_base_url(base_url)
    tools: list[Tool] = []
    for name, description, properties, required, method, path, kind in _SPEC:
        tools.append(
            Tool(
                name=name,
                description=description,
                input_schema={"type": "object", "properties": properties, "required": required},
                handler=_make_handler(method, path, kind, resolved),
            )
        )
    return tools
