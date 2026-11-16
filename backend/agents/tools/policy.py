"""Tool allowlist and memory rule for agents (NS-ADR-113).

Everything here is a frozen constant or a pure function. Agents (and the model
output they produce) can only pick from these names; nothing at runtime adds to
them. The Gremlin hub memory graph is persisted and shared by every hub client,
so memory tools are refused unless a caller explicitly marks a call
non-sensitive, the agent is not in a sensitive class and the arguments carry no
personal data (NS-ADR-107/108: personal data never reaches shared state).
"""

from __future__ import annotations

import json
import re
from types import MappingProxyType
from typing import Any

HUB = "hub"
OMNIROUTE_SERVER = "omniroute"

MEMORY_TOOLS = frozenset(
    {
        "create_entities",
        "create_relations",
        "add_observations",
        "delete_entities",
        "delete_observations",
        "delete_relations",
        "read_graph",
        "search_nodes",
        "open_nodes",
    }
)

#: Hub route (``/<server>``) to the tools callable on it.
HUB_SERVERS: MappingProxyType[str, frozenset[str]] = MappingProxyType(
    {
        "sequential-thinking": frozenset({"sequentialthinking"}),
        "fetch": frozenset({"fetch"}),
        "memory": MEMORY_TOOLS,
    }
)

#: The read-only OmniRoute MCP tools (privileged key).
OMNIROUTE_TOOLS = frozenset(
    {
        "omniroute_get_health",
        "omniroute_list_combos",
        "omniroute_get_combo_metrics",
        "omniroute_check_quota",
        "omniroute_cost_report",
        "omniroute_list_models_catalog",
        "omniroute_simulate_route",
        "omniroute_get_provider_metrics",
        "omniroute_best_combo_for_task",
        "omniroute_explain_route",
        "omniroute_get_session_snapshot",
    }
)

#: Agent classes that handle personal data: they never write to shared memory.
SENSITIVE_CLASSES = frozenset(
    {"finance", "mental_health", "health", "wellness", "bills", "messages", "mail"}
)

MAX_ARGS_CHARS = 8_000

_PERSONAL = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.-]+"  # email
    r"|\+?\d[\d\s().-]{7,}\d"  # phone-like run
    r"|\d{9,}"  # long digit run (account, card, id)
    r"|\b(?:sk|pk|ghp|xox[bp])[-_][A-Za-z0-9_-]{16,}"  # key-like token
    r"|[A-Za-z0-9+/=_-]{40,}"  # long opaque string
)


def sensitive_class(agent: Any) -> bool:
    """True when the agent's type or name falls in a sensitive class."""
    text = f"{getattr(agent, 'type', '')} {getattr(agent, 'name', '')}".lower()
    text = re.sub(r"[\s-]+", "_", text)
    return any(cls in text for cls in SENSITIVE_CLASSES)


def has_personal_data(args: Any) -> bool:
    return bool(_PERSONAL.search(json.dumps(args, default=str)))


def is_allowed(server: str, tool: str) -> bool:
    if server == OMNIROUTE_SERVER:
        return tool in OMNIROUTE_TOOLS
    return tool in HUB_SERVERS.get(server, frozenset())


def check(
    agent: Any, server: str, tool: str, args: dict[str, Any], *, non_sensitive: bool = False
) -> str | None:
    """A refusal reason code, or ``None`` when the call is allowed."""
    if server != OMNIROUTE_SERVER and server not in HUB_SERVERS:
        return "unknown_server"
    if not is_allowed(server, tool):
        return "tool_not_allowed"
    if not isinstance(args, dict):
        return "bad_arguments"
    if len(json.dumps(args, default=str)) > MAX_ARGS_CHARS:
        return "arguments_too_large"
    if server == "memory":
        if not non_sensitive:
            return "memory_requires_non_sensitive"
        if sensitive_class(agent):
            return "memory_sensitive_agent"
        if has_personal_data(args):
            return "memory_personal_data"
    return None


def listing() -> dict[str, list[str]]:
    """The allowlist as plain data (for the tools endpoint and prompts)."""
    out = {name: sorted(tools) for name, tools in HUB_SERVERS.items()}
    out[OMNIROUTE_SERVER] = sorted(OMNIROUTE_TOOLS)
    return out
