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


def instructions() -> str:
    """The prompt block that teaches the protocol and lists the allowlist."""
    listing = "\n".join(
        f"- {server}: {', '.join(tools)}"
        for server, tools in policy.listing().items()
        if server != "memory"
    )
    return (
        "Tools are available. To use one, write a line exactly like\n"
        'TOOL_CALL {"server": "fetch", "tool": "fetch", "args": {"url": "https://..."}}\n'
        f"and stop; results come back as TOOL_RESULT lines (at most {MAX_ROUNDS} rounds). "
        "Never put personal data in tool arguments. Allowed servers and tools:\n" + listing
    )


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
) -> dict[str, Any]:
    """Run ``infer_once`` and serve its tool calls through ``gateway`` (bounded).

    Token counts and latency are summed over the rounds. Without a configured
    gateway this is exactly one call with the original prompt.
    """
    if gateway is None or not gateway.configured:
        return await infer_once(prompt)
    current = prompt + "\n\n" + instructions()
    result = await infer_once(current)
    totals = {k: int(result.get(k) or 0) for k in ("input_tokens", "output_tokens")}
    latency = float(result.get("e2e_latency_ms") or 0)
    for _ in range(MAX_ROUNDS):
        calls = parse_calls(str(result.get("content", "")))
        if not calls:
            break
        lines = [
            format_result(c, await gateway.call(agent, c.server, c.tool, c.args, context=prompt))
            for c in calls
        ]
        current = f"{current}\n\n{result.get('content', '')}\n\n" + "\n".join(lines)
        result = await infer_once(current)
        for k in totals:
            totals[k] += int(result.get(k) or 0)
        latency += float(result.get("e2e_latency_ms") or 0)
    merged = dict(result)
    merged.update(totals)
    if latency:
        merged["e2e_latency_ms"] = latency
    return merged
