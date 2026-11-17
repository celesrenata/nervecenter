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
#: A reply this long (output tokens) has hit the model's reply limit (8,192 on the routes in use).
CUT_OFF_TOKENS = 8_000

_LINE = re.compile(r"^\s*TOOL_CALL\s+(\{.*\})\s*$", re.MULTILINE)
_RESULT_LINE = re.compile(r"^\s*TOOL_RESULT\b", re.MULTILINE)
_CALL_START = re.compile(r"^\s*TOOL_CALL\b.*$", re.MULTILINE)


@dataclass(frozen=True)
class ToolCall:
    server: str
    tool: str
    args: dict[str, Any]


def strip_invented_results(text: str) -> str:
    """Drop everything from the first ``TOOL_RESULT`` line on.

    Only the runner writes results. A model that writes its own (and keeps
    "reasoning" over them) is inventing data, so its turn ends at that line.
    """
    match = _RESULT_LINE.search(text or "")
    return text[: match.start()].rstrip() if match else text


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


class CutOffToolCall(RuntimeError):
    """The model's reply ended inside a TOOL_CALL, twice in a row."""
def is_cut_off_call(text: str, output_tokens: int | None = None) -> bool:
    """Whether the reply's last ``TOOL_CALL`` line is unparseable JSON and the reply was cut off.
    Cut off means it reached the reply limit, or it does not end with a closing brace.
    """
    lines = _CALL_START.findall(strip_invented_results(text or ""))
    if not lines:
        return False
    last = lines[-1].strip()
    try:
        json.loads(last.removeprefix("TOOL_CALL").strip())
        return False
    except ValueError:
        pass
    return int(output_tokens or 0) >= CUT_OFF_TOKENS or not last.endswith("}")
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
    '"rollback": "how to undo"}}. Nothing runs until the owner approves it.\n'
    "Optional step fields (kube plans): \"kind\" (read, change, backup, backup_verify, repair, "
    "restore), \"requires\" (earlier step indexes that must have succeeded), "
    "\"on_failure_of\" (runs only when those steps failed), \"rollback\" (one sentence to undo "
    "that step). Optional plan field: \"issue_key\" (the issue this plan fixes). A plan may "
    "carry \"runbook\": the object from nerve/kube_runbook; the system then writes the steps."
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
    "pentest_scope": "{} (the scan allowlist: status, targets, what was left out)",
    "pentest_exposure": '{"limit": 20} (firewall risks and internet-facing hosts)',
    "pentest_findings": '{"status": "open", "severity": "high", "offset": 0, "limit": 12} '
    "(severity and exposure tier are computed, not yours to change)",
    "pentest_scan_plan": "{} (the next scan-sized slices from the owner's ticked selection, "
    "counts and at most 10 slices; read-only)",
    "pentest_scan_start": '{"slice": "<id from pentest_scan_plan>"} (no argument starts the '
    "first planned slice; never write a target list yourself; refused outside the "
    "owner-approved allowlist)",
    "pentest_scan_status": '{"scan_id": "<optional>"} (the scan ledger, or one scan)',
    "kube_health": "{} (open cluster problems with mode: known_down, diagnose_only, "
    "override_active or normal; read-only)",
    "kube_describe": '{"kind": "pod", "name": "...", "namespace": "..."} (kinds: pod, pvc, pv, '
    "node, deploy, statefulset, daemonset, job, cronjob, svc, ingress, event, "
    "volumes.longhorn.io, certificate; never Secrets; read-only)",
    "kube_logs": '{"namespace": "...", "pod": "...", "container": "", "previous": false, '
    '"tail": 100} (1 to 300 lines; read-only)',
    "kube_waste": "{} (idle and over-sized pods, unused PVCs, orphaned PVs, restart churn; "
    "read-only)",
    "kube_known_down": "{} (known-down and protected namespaces, active overrides, first_run)",
    "kube_runbook": '{"kind": "mysql_repair|redis_repair|postgres|nextcloud_upgrade|crashloop|'
    'imagepull|pvc|backup", "namespace": "...", "workload": "...", "pod": "...", '
    '"dbtype": "mysql|postgres|redis", "issue_key": "..."} (a step skeleton to pass to '
    "propose_plan as runbook; read-only, nothing runs)",
    "resources_list": "{} (files the owner linked to you; empty when none)",
    "resources_read": '{"resource_id": "...", "offset": 0, "limit": 5000} (text files only, '
    "5,000 characters per page)",
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
    detail = f": {outcome['detail']}" if outcome.get("detail") else ""
    return (
        f"TOOL_RESULT {call.server}/{call.tool}: refused or failed "
        f"({outcome.get('denied_reason')}{detail})"
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
    async def mend(reply: dict[str, Any], base: str) -> dict[str, Any]:
        """A reply cut off inside a TOOL_CALL is a failed round: ask once more, then fail."""
        nonlocal round_no, latency
        if not is_cut_off_call(str(reply.get("content", "")), reply.get("output_tokens")):
            return reply
        emit("model_retry", "Model reply was cut off mid tool call; asking for a short call")
        again = (
            f"{base}\n\nTOOL_RESULT: your last reply was cut off in the middle of a TOOL_CALL. "
            "Write a short TOOL_CALL (one line, under 300 characters, no lists of targets or "
            "addresses), or write the final Markdown report if you have enough."
        )
        round_no += 1
        retry = await call_model(again, round_no)
        for k in totals:
            totals[k] += int(retry.get(k) or 0)
        latency += float(retry.get("e2e_latency_ms") or 0)
        if is_cut_off_call(str(retry.get("content", "")), retry.get("output_tokens")):
            raise CutOffToolCall("model reply was cut off mid tool call")
        return retry
    result = await mend(result, current)
    for _ in range(MAX_ROUNDS):
        calls = parse_calls(strip_invented_results(str(result.get("content", ""))))
        if not calls:
            break
        result = {**result, "content": strip_invented_results(str(result.get("content", "")))}
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
        result = await mend(result, current)
    if parse_calls(strip_invented_results(str(result.get("content", "")))):
        # The round budget ran out while the model still wanted tools: ask for the report.
        current = (
            f"{current}\n\n{strip_invented_results(str(result.get('content', '')))}\n\n"
            "TOOL_RESULT: no tool rounds are left. Do not write any TOOL_CALL. Write the final "
            "Markdown report now from the results above, listing what you could not check."
        )
        round_no += 1
        result = await call_model(current, round_no)
        for k in totals:
            totals[k] += int(result.get(k) or 0)
        latency += float(result.get("e2e_latency_ms") or 0)
        result = await mend(result, current)
    content = str(result.get("content", ""))
    if "## Summary" in content and "## Not checked" not in content:
        # The report started but was cut off (reply limit reached): ask once for a short one.
        current = (
            f"{current}\n\nTOOL_RESULT: your last reply was cut off before the end. Write the "
            "complete Markdown report again now, much shorter (under 400 words), with every "
            "section. Do not write any TOOL_CALL."
        )
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
