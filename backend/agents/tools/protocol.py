"""Bounded text protocol for tool use (NS-ADR-113).

The model asks for a tool with one line per call::

    TOOL_CALL {"server": "fetch", "tool": "fetch", "args": {"url": "https://example.org"}}

At most :data:`MAX_ROUNDS` rounds of at most :data:`MAX_CALLS_PER_ROUND` calls
run per model invocation. The protocol has no way to mark a call non-sensitive:
that flag exists only on the gateway's Python and owner API surface.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from backend.agents.tools import policy
from backend.infra.policy import SSH_READ_COMMANDS

MAX_ROUNDS = 3
MAX_CALLS_PER_ROUND = 3
RESULT_CHARS = 6_000

_LINE = re.compile(r"^\s*TOOL_CALL\s+(\{.*\})\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ToolCall:
    server: str
    tool: str
    args: dict[str, Any]


def parse_calls(text: str) -> list[ToolCall]:
    """The well-formed ``TOOL_CALL`` lines in ``text`` (malformed ones are ignored)."""
    calls: list[ToolCall] = []
    for match in _LINE.finditer(text or ""):
        try:
            raw = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(raw, dict):
            continue
        server, tool, args = raw.get("server"), raw.get("tool"), raw.get("args", {})
        if isinstance(server, str) and isinstance(tool, str) and isinstance(args, dict):
            calls.append(ToolCall(server, tool, args))
        if len(calls) >= MAX_CALLS_PER_ROUND:
            break
    return calls


INFRA_HELP = (
    "Infrastructure (read-only; changes only through an approved plan):\n"
    '- TOOL_CALL {"server": "infra", "tool": "connectors", "args": {}} lists clusters/hosts.\n'
    '- TOOL_CALL {"server": "infra", "tool": "kubectl", "args": {"connector": "<name>", '
    '"args": ["get", "pods", "-A"]}} (get/describe/logs/top/events/api-resources only; '
    "Secrets are never readable).\n"
    '- TOOL_CALL {"server": "infra", "tool": "ssh", "args": {"connector": "<name>", '
    '"command": "<menu entry>", "params": {}}} with a menu entry from: '
    + ", ".join(sorted(SSH_READ_COMMANDS))
    + " (service_status/journal take params.unit, journal/kernel_log params.lines).\n"
    '- To change anything: TOOL_CALL {"server": "infra", "tool": "propose_plan", "args": '
    '{"title": "...", "summary": "why and what", "steps": [{"connector": "<name>", '
    '"args": ["rollout", "restart", "deploy/x", "-n", "ns"], "purpose": "..."}, '
    '{"connector": "<ssh name>", "command": "sudo systemctl restart foo", "purpose": "..."}], '
    '"verify": [{"connector": "<name>", "args": ["get", "pods", "-n", "ns"]}], '
    '"rollback": "how to undo"}}. Nothing runs until the owner approves it.'
)


#: Argument help for the ``nerve`` tools (shown only for the tools an agent is granted).
NERVE_ARGS = {
    "tasks_list": '{"status": "draft|pending|...", "goal_id": "...", "limit": 30}',
    "goals_list": '{"limit": 30}',
    "task_create": '{"title": "...", "description": "...", "priority": 0-10, '
    '"goal_id": "...", "dependencies": ["<task id>"]} (creates a DRAFT the owner must accept)',
    "task_update": '{"task_id": "...", "title": "...", "description": "...", "priority": 3, '
    '"dependencies": []} (only your own drafts)',
    "incidents_list": '{"limit": 20}',
    "incident_get": '{"incident_id": "..."}',
    "events_search": '{"entity": "<subject id, optional>", "limit": 30}',
    "changes_list": '{"limit": 30}',
    "app_logs": '{"min_level": "warning", "contains": "text", "limit": 40} (the API pod only)',
    "logs_search": '{"query": "level:error AND payments", "since_minutes": 60, "limit": 30} '
    "(cluster-wide Elasticsearch, Lucene query syntax)",
    "evidence_search": '{"contains": "text", "source_type": "...", "limit": 20}',
    "evidence_get": '{"evidence_id": "..."}',
    "systems_status": "{}",
    "repo_ask": '{"repo": "nervcenter", "ref": "main", "question": "..."} (queues a read-only '
    "opencode job; no personal data in it)",
    "repo_result": '{"handoff_id": "..."} (poll a repo_ask; it can take minutes)',
}
def nerve_help(tools: list[str]) -> str:
    lines = []
    for t in tools:
        args, _, note = NERVE_ARGS.get(t, "{}").partition(" (")
        tail = f" ({note}" if note else ""
        lines.append(f'- TOOL_CALL {{"server": "nerve", "tool": "{t}", "args": {args}}}{tail}')
    return "NerveCenter data (read-only unless stated):\n" + "\n".join(lines)
def instructions(agent: Any = None) -> str:
    """The prompt block that teaches the protocol and lists the allowlist."""
    servers = policy.listing(agent)
    listing = "\n".join(
        f"- {server}: {', '.join(tools)}" for server, tools in servers.items() if server != "memory"
    )
    text = (
        "Tools are available. To use one, write a line exactly like\n"
        'TOOL_CALL {"server": "fetch", "tool": "fetch", "args": {"url": "https://..."}}\n'
        f"and stop; results come back as TOOL_RESULT lines (at most {MAX_ROUNDS} rounds). "
        "Never put personal data in tool arguments. Allowed servers and tools:\n" + listing
    )
    if policy.INFRA_SERVER in servers:
        text += "\n" + INFRA_HELP
    if policy.NERVE_SERVER in servers:
        text += "\n" + nerve_help(servers[policy.NERVE_SERVER])
    return text


def format_result(call: ToolCall, outcome: dict[str, Any]) -> str:
    if outcome.get("ok"):
        body = json.dumps(outcome.get("result"), default=str)[:RESULT_CHARS]
        return f"TOOL_RESULT {call.server}/{call.tool}: {body}"
    return (
        f"TOOL_RESULT {call.server}/{call.tool}: refused or failed ({outcome.get('denied_reason')})"
    )


async def run_with_tools(
    infer_once: Callable[[str], Awaitable[dict[str, Any]]],
    prompt: str,
    gateway: Any,
    agent: Any,
    on_event: Callable[[str, str, dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    """Run ``infer_once`` and serve its tool calls through ``gateway`` (bounded).

    Token counts and latency are summed over the rounds. Without a configured
    gateway this is exactly one call with the original prompt. ``on_event``
    (kind, text, data) reports progress for the live log; it never raises.
    """
    def emit(kind: str, text: str, data: dict[str, Any] | None = None) -> None:
        if on_event is not None:
            try:
                on_event(kind, text, data)
            except Exception:  # noqa: BLE001 - progress reporting is best-effort
                pass

    async def call_model(text: str, round_no: int) -> dict[str, Any]:
        emit("model_call", f"Model round {round_no}: waiting for a reply", {"round": round_no})
        reply = await infer_once(text)
        emit(
            "model_reply",
            str(reply.get("content", "")),
            {
                "round": round_no,
                "input_tokens": reply.get("input_tokens"),
                "output_tokens": reply.get("output_tokens"),
                "latency_ms": reply.get("e2e_latency_ms"),
            },
        )
        return reply

    if gateway is None or not gateway.configured:
        return await call_model(prompt, 1)
    current = prompt + "\n\n" + instructions(agent)
    result = await call_model(current, 1)
    round_no = 1
    totals = {k: int(result.get(k) or 0) for k in ("input_tokens", "output_tokens")}
    latency = float(result.get("e2e_latency_ms") or 0)
    for _ in range(MAX_ROUNDS):
        calls = parse_calls(str(result.get("content", "")))
        if not calls:
            break
        lines = []
        for c in calls:
            emit(
                "tool_call",
                f"{c.server}.{c.tool}",
                {"server": c.server, "tool": c.tool, "args": c.args},
            )
            line = format_result(
                c, await gateway.call(agent, c.server, c.tool, c.args, context=prompt)
            )
            emit("tool_result", line, {"server": c.server, "tool": c.tool})
            lines.append(line)
        current = f"{current}\n\n{result.get('content', '')}\n\n" + "\n".join(lines)
        round_no += 1
        result = await call_model(current, round_no)
        for k in totals:
            totals[k] += int(result.get(k) or 0)
        latency += float(result.get("e2e_latency_ms") or 0)
    merged = dict(result)
    merged.update(totals)
    if latency:
        merged["e2e_latency_ms"] = latency
    return merged
