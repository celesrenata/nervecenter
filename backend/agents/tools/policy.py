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

#: Infrastructure tools (NS-ADR-120): read-only kubectl/ssh plus plan proposals.
INFRA_SERVER = "infra"
INFRA_TOOLS = frozenset({"connectors", "kubectl", "ssh", "propose_plan"})
#: Agent types that get the infra tools by default; others need "infra" in their tools.
INFRA_AGENT_TYPES = frozenset(
    {"architecture_analyst", "infrastructure_inspector", "runtime_inspector",
     "log_investigator", "incident_correlator"}
)


#: NerveCenter's own data (read-only; see backend/agents/tools/nerve.py). Each agent
#: type gets only the tools listed for it. Anything not listed is "tool_not_allowed".
NERVE_SERVER = "nerve"
NERVE_AGENT_TOOLS: MappingProxyType[str, frozenset[str]] = MappingProxyType(
    {
        "planner": frozenset(
            {"tasks_list", "goals_list", "task_create", "task_update", "resources_list",
             "resources_read"}
        ),
        "repository_inspector": frozenset({"repo_ask", "repo_result"}),
        "code_reviewer": frozenset({"repo_ask", "repo_result"}),
        "test_runner": frozenset({"repo_ask", "repo_result"}),
        "log_investigator": frozenset({"app_logs", "logs_search"}),
        "incident_correlator": frozenset(
            {"incidents_list", "incident_get", "events_search", "changes_list", "app_logs"}
        ),
        "verifier": frozenset(
            {"evidence_search", "evidence_get", "systems_status", "resources_list",
             "resources_read"}
        ),
        "architecture_analyst": frozenset({"systems_status"}),
        "context_critic": frozenset(
            {"goals_list", "tasks_list", "evidence_search", "resources_list", "resources_read"}
        ),
        "research_agent": frozenset({"resources_list", "resources_read"}),
        "pentest": frozenset(
            {"pentest_scope", "pentest_exposure", "pentest_findings", "pentest_scan_plan",
             "pentest_scan_start", "pentest_scan_status", "resources_list", "resources_read", "task_create",
             "task_update"}
        ),
        "kube_doctor": frozenset(
            {"kube_health", "kube_describe", "kube_logs", "kube_events", "kube_waste",
             "kube_known_down",
             "kube_runbook", "task_create", "task_update"}
        ),
    }
)
NERVE_TOOLS = frozenset().union(*NERVE_AGENT_TOOLS.values())
def nerve_tools_for(agent: Any) -> frozenset[str]:
    return NERVE_AGENT_TOOLS.get(str(getattr(agent, "type", "") or ""), frozenset())
def infra_allowed(agent: Any) -> bool:
    if agent is None:
        return False
    if str(getattr(agent, "type", "") or "") in INFRA_AGENT_TYPES:
        return True
    tools = getattr(agent, "tools", None) or []
    return isinstance(tools, (list, tuple, set)) and "infra" in tools


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
    if server == INFRA_SERVER:
        return tool in INFRA_TOOLS
    if server == NERVE_SERVER:
        return tool in NERVE_TOOLS
    if server == OMNIROUTE_SERVER:
        return tool in OMNIROUTE_TOOLS
    return tool in HUB_SERVERS.get(server, frozenset())


def check(
    agent: Any, server: str, tool: str, args: dict[str, Any], *, non_sensitive: bool = False
) -> str | None:
    """A refusal reason code, or ``None`` when the call is allowed."""
    if server not in (OMNIROUTE_SERVER, INFRA_SERVER, NERVE_SERVER) and server not in HUB_SERVERS:
        return "unknown_server"
    if server == NERVE_SERVER and tool not in nerve_tools_for(agent):
        return "tool_not_allowed"
    if server == INFRA_SERVER and not infra_allowed(agent):
        return "infra_not_granted"
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


def listing(
    agent: Any = None, *, include_infra: bool = False, include_nerve: bool = False
) -> dict[str, list[str]]:
    """The allowlist as plain data (for the tools endpoint and prompts)."""
    out = {name: sorted(tools) for name, tools in HUB_SERVERS.items()}
    out[OMNIROUTE_SERVER] = sorted(OMNIROUTE_TOOLS)
    if include_infra or infra_allowed(agent):
        out[INFRA_SERVER] = sorted(INFRA_TOOLS)
    if include_nerve:
        out[NERVE_SERVER] = sorted(NERVE_TOOLS)
    elif nerve_tools_for(agent):
        out[NERVE_SERVER] = sorted(nerve_tools_for(agent))
    return out
