"""Agents module for NerveCenter.

This module provides:

- :class:`AgentRegistry` — registry of native agent types and their capability manifests.
- :class:`AgentRun` — lifecycle tracking for individual agent executions.
- :class:`Blackboard` — append-only shared state for multi-agent coordination.
- :class:`LoopDetector` — loop protection per spec §75.

Native agent types (spec §26)
-----------------------------
Planner, RepositoryInspector, RuntimeInspector, LogInvestigator,
InfrastructureInspector, ArchitectureAnalyst, IncidentCorrelator,
CodeReviewer, TestRunner, BenchmarkAgent, ResearchAgent, Verifier,
ContextCritic, RouteCritic.

Spec sections
-------------
- NerveCenter specification Section 25 (Shared Agent Blackboard).
- NerveCenter specification Section 26 (Native Agent Types).
- NerveCenter specification Section 75 (Agent Loop Protection).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import threading
from typing import Any
from uuid import uuid4

import structlog

log = structlog.get_logger(__name__)

__all__ = [
    "AgentType",
    "AgentCapabilities",
    "AgentManifest",
    "AgentRegistry",
    "AgentRun",
    "Blackboard",
    "LoopDetector",
    "get_agent_registry",
    "reset_agent_registry",
]

# AgentType enum
AgentType = str
AGENT_PLANNER: AgentType = "planner"
AGENT_REPOSITORY_INSPECTOR: AgentType = "repository_inspector"
AGENT_RUNTIME_INSPECTOR: AgentType = "runtime_inspector"
AGENT_LOG_INVESTIGATOR: AgentType = "log_investigator"
AGENT_INFRASTRUCTURE_INSPECTOR: AgentType = "infrastructure_inspector"
AGENT_ARCHITECTURE_ANALYST: AgentType = "architecture_analyst"
AGENT_INCIDENT_CORRELATOR: AgentType = "incident_correlator"
AGENT_CODE_REVIEWER: AgentType = "code_reviewer"
AGENT_TEST_RUNNER: AgentType = "test_runner"
AGENT_BENCHMARK_AGENT: AgentType = "benchmark_agent"
AGENT_RESEARCH_AGENT: AgentType = "research_agent"
AGENT_VERIFIER: AgentType = "verifier"
AGENT_CONTEXT_CRITIC: AgentType = "context_critic"
AGENT_ROUTE_CRITIC: AgentType = "route_critic"


@dataclass(frozen=True)
class AgentCapabilities:
    """The capabilities of an agent (tools, policies, verification)."""
    tools: list[str]
    prompt: str
    policy: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentManifest:
    """Capability manifest for an agent type."""
    agent_type: AgentType
    name: str
    description: str
    prompt_template: str
    tools: list[str]
    loop_protection: dict[str, Any] = field(default_factory=dict)
    capabilities: AgentCapabilities | None = None
    #: Optional per-agent inference binding (contract §3): ``{"provider": str,
    #: "model": str}`` (either key optional) or ``None`` = use the default.
    #: Process-wide (in-process store) — not persisted.
    inference: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.capabilities is None:
            object.__setattr__(
                self,
                "capabilities",
                AgentCapabilities(
                    tools=self.tools,
                    prompt=self.prompt_template,
                    policy=None,
                    verification=None,
                ),
            )


@dataclass(slots=True)
class BlackboardEntry:
    """A single entry on the shared agent blackboard."""
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    entry_type: str = "fact"
    content: dict[str, Any] = field(default_factory=dict)
    agent_id: str | None = None
    task_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return as a dictionary for storage."""
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat(),
            "entry_type": self.entry_type,
            "content": self.content,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
        }


@dataclass(slots=True)
class Blackboard:
    """Shared append-only blackboard for multi-agent coordination.

    Per spec §25, entries are:
    - append-only (new entries supersede previous)
    - ordered by insertion time
    - queryable by type and task
    """
    task_id: str
    entries: list[BlackboardEntry] = field(default_factory=list)

    def add_fact(self, content: dict[str, Any], agent_id: str | None = None) -> None:
        """Add a fact entry."""
        self.entries.append(BlackboardEntry(
            entry_type="fact",
            content=content,
            agent_id=agent_id,
            task_id=self.task_id,
        ))

    def add_hypothesis(self, content: dict[str, Any], agent_id: str | None = None) -> None:
        """Add a hypothesis entry."""
        self.entries.append(BlackboardEntry(
            entry_type="hypothesis",
            content=content,
            agent_id=agent_id,
            task_id=self.task_id,
        ))

    def get_by_type(self, entry_type: str) -> list[BlackboardEntry]:
        """Get all entries of a specific type."""
        return [e for e in self.entries if e.entry_type == entry_type]

    def as_dict(self) -> dict[str, Any]:
        """Return as a dictionary matching the spec schema."""
        return {
            "task_id": self.task_id,
            "facts": [e.as_dict() for e in self.get_by_type("fact")],
            "hypotheses": [e.as_dict() for e in self.get_by_type("hypothesis")],
            "questions": [e.as_dict() for e in self.get_by_type("question")],
            "artifacts": [e.as_dict() for e in self.get_by_type("artifact")],
            "blockers": [e.as_dict() for e in self.get_by_type("blocker")],
            "agent_runs": [e.as_dict() for e in self.entries],
            "consensus": None,
        }


class AgentRegistry:
    """Registry of agent types and their manifests."""

    def __init__(self) -> None:
        self._manifests: dict[AgentType, AgentManifest] = {}
        # Process-wide store of inference bindings for *persisted* agent instances
        # (id → binding). Hydrated from the DB by the API layer just before a sync
        # call needs them (contract §3 — persisted instances use a jsonb column,
        # not the manifest, so this is the in-process mirror that ``ask`` reads).
        self._agent_inference: dict[str, dict[str, Any]] = {}
        self._register_native()

    def _register_native(self) -> None:
        """Register all 14 native agent types."""
        native = _create_native_manifests()
        self._manifests.update(native)

    def register(self, manifest: AgentManifest) -> None:
        """Register a new agent type."""
        self._manifests[manifest.agent_type] = manifest

    def get(self, agent_type: AgentType) -> AgentManifest | None:
        """Get the manifest for an agent type."""
        return self._manifests.get(agent_type)

    def list_all(self) -> list[AgentManifest]:
        """List all registered agent manifests."""
        return list(self._manifests.values())

    def get_native_types(self) -> list[AgentType]:
        """Return the list of native agent types."""
        return list(_create_native_manifests().keys())

    def set_inference(self, agent_type: AgentType, binding: dict[str, Any] | None) -> None:
        """Set (or clear) the inference binding for a native agent type.

        ``binding`` is ``{"provider": str, "model": str}`` (either key optional) or
        ``None`` to use the default. Process-wide (in-process store, no DB). An
        unknown type raises :class:`KeyError` — no silent write to a missing
        manifest.
        """
        manifest = self._manifests.get(agent_type)
        if manifest is None:
            raise KeyError(agent_type)
        # Frozen dataclass — mutate via the same escape hatch ``__post_init__`` uses.
        object.__setattr__(manifest, "inference", binding)

    def inference(self, agent_type: AgentType) -> dict[str, Any] | None:
        """Return the inference binding for a native agent type (or ``None``)."""
        manifest = self._manifests.get(agent_type)
        return None if manifest is None else manifest.inference

    def set_agent_binding(self, agent_id: str, binding: dict[str, Any] | None) -> None:
        """Mirror a persisted agent instance's inference binding into the store.

        ``binding`` is ``{"provider": str, "model": str}`` (either key optional) or
        ``None``. No native-type validation — this is the in-process mirror the
        sync :func:`ask` resolves against for a persisted agent id (contract §2
        step 2). The source of truth is the ``agents.inference`` jsonb column.
        """
        if binding is None:
            self._agent_inference.pop(agent_id, None)
        else:
            self._agent_inference[agent_id] = binding

    def agent_binding(self, agent_id: str) -> dict[str, Any] | None:
        """Return the mirrored inference binding for a persisted agent id (``None``
        if it has none — e.g. the agent is a native type or unbound)."""
        return self._agent_inference.get(agent_id)


#: Process-wide registry singleton backing the inference bindings. Both the agent
#: *endpoints* (which mirror a persisted instance's ``inference`` jsonb into the
#: in-process store via :meth:`AgentRegistry.set_agent_binding`) and the synchronous
#: :func:`backend.context.ask.AskService.ask` resolution must read the *same*
#: instance — a per-request ``AgentRegistry()`` would be a fresh empty store.
_agent_registry_lock = threading.Lock()
_agent_registry: AgentRegistry | None = None


def get_agent_registry() -> AgentRegistry:
    """Return the process-wide :class:`AgentRegistry` singleton (lazily created).

    The 14 native type manifests are immutable, so a single shared instance is
    safe; the only mutable state is the per-agent inference binding store, which
    is written under a lock on the instance.
    """
    global _agent_registry
    with _agent_registry_lock:
        if _agent_registry is None:
            _agent_registry = AgentRegistry()
        return _agent_registry


def reset_agent_registry() -> None:
    """Discard the process-wide registry (test hook)."""
    global _agent_registry
    with _agent_registry_lock:
        _agent_registry = None


def _create_native_manifests() -> dict[AgentType, AgentManifest]:
    """Create the 14 native agent capability manifests."""
    return {
        AGENT_PLANNER: AgentManifest(
            agent_type=AGENT_PLANNER,
            name="Planner",
            description="Creates and manages task DAGs for goals.",
            prompt_template="You are a planning agent. Analyze goals and create executable task DAGs.",
            tools=["task_create", "task_update", "task_list"],
            loop_protection={"max_iterations": 10, "blackboard_change_threshold": 0.1},
        ),
        AGENT_REPOSITORY_INSPECTOR: AgentManifest(
            agent_type=AGENT_REPOSITORY_INSPECTOR,
            name="Repository Inspector",
            description="Scans and analyzes Git repositories.",
            prompt_template="You are a repository inspector agent. Analyze Git repositories for issues.",
            tools=["git_clone", "git_diff", "git_log", "file_read", "file_list"],
            loop_protection={"max_iterations": 15, "blackboard_change_threshold": 0.15},
        ),
        AGENT_RUNTIME_INSPECTOR: AgentManifest(
            agent_type=AGENT_RUNTIME_INSPECTOR,
            name="Runtime Inspector",
            description="Inspects running services and infrastructure.",
            prompt_template="You are a runtime inspector agent. Inspect running services and infrastructure.",
            tools=["pod_logs", "service_status", "container_inspect", "env_get"],
            loop_protection={"max_iterations": 12, "blackboard_change_threshold": 0.15},
        ),
        AGENT_LOG_INVESTIGATOR: AgentManifest(
            agent_type=AGENT_LOG_INVESTIGATOR,
            name="Log Investigator",
            description="Searches and analyzes logs for anomalies.",
            prompt_template="You are a log investigator agent. Search and analyze logs for anomalies.",
            tools=["log_search", "log_pattern", "log_aggregate"],
            loop_protection={"max_iterations": 20, "blackboard_change_threshold": 0.1},
        ),
        AGENT_INFRASTRUCTURE_INSPECTOR: AgentManifest(
            agent_type=AGENT_INFRASTRUCTURE_INSPECTOR,
            name="Infrastructure Inspector",
            description="Inspects K8s, hosts, and resources.",
            prompt_template="You are an infrastructure inspector agent. Inspect K8s, hosts, and resources.",
            tools=["k8s_get", "k8s_list", "host_info", "resource_list"],
            loop_protection={"max_iterations": 15, "blackboard_change_threshold": 0.1},
        ),
        AGENT_ARCHITECTURE_ANALYST: AgentManifest(
            agent_type=AGENT_ARCHITECTURE_ANALYST,
            name="Architecture Analyst",
            description="Analyzes system architecture and dependencies.",
            prompt_template="You are an architecture analyst agent. Analyze system architecture and dependencies.",
            tools=["dep_graph", "svc_map", "arch_review"],
            loop_protection={"max_iterations": 8, "blackboard_change_threshold": 0.2},
        ),
        AGENT_INCIDENT_CORRELATOR: AgentManifest(
            agent_type=AGENT_INCIDENT_CORRELATOR,
            name="Incident Correlator",
            description="Correlates incidents with events and changes.",
            prompt_template="You are an incident correlator agent. Correlate incidents with events and changes.",
            tools=["incident_list", "event_search", "change_analyze"],
            loop_protection={"max_iterations": 10, "blackboard_change_threshold": 0.1},
        ),
        AGENT_CODE_REVIEWER: AgentManifest(
            agent_type=AGENT_CODE_REVIEWER,
            name="Code Reviewer",
            description="Reviews code quality and security.",
            prompt_template="You are a code reviewer agent. Review code quality and security.",
            tools=["code_review", "lint_check", "test_coverage"],
            loop_protection={"max_iterations": 12, "blackboard_change_threshold": 0.15},
        ),
        AGENT_TEST_RUNNER: AgentManifest(
            agent_type=AGENT_TEST_RUNNER,
            name="Test Runner",
            description="Executes and analyzes test suites.",
            prompt_template="You are a test runner agent. Execute and analyze test suites.",
            tools=["test_run", "test_report", "test_coverage"],
            loop_protection={"max_iterations": 10, "blackboard_change_threshold": 0.1},
        ),
        AGENT_BENCHMARK_AGENT: AgentManifest(
            agent_type=AGENT_BENCHMARK_AGENT,
            name="Benchmark Agent",
            description="Runs performance benchmarks.",
            prompt_template="You are a benchmark agent. Run performance benchmarks.",
            tools=["bench_run", "bench_compare", "metrics_collect"],
            loop_protection={"max_iterations": 8, "blackboard_change_threshold": 0.2},
        ),
        AGENT_RESEARCH_AGENT: AgentManifest(
            agent_type=AGENT_RESEARCH_AGENT,
            name="Research Agent",
            description="Finds and synthesizes information.",
            prompt_template="You are a research agent. Find and synthesize information.",
            tools=["web_search", "doc_read", "summary_generate"],
            loop_protection={"max_iterations": 15, "blackboard_change_threshold": 0.1},
        ),
        AGENT_VERIFIER: AgentManifest(
            agent_type=AGENT_VERIFIER,
            name="Verifier",
            description="Verifies claims and hypotheses.",
            prompt_template="You are a verifier agent. Verify claims and hypotheses.",
            tools=["evidence_search", "claim_verify", "hypothesis_test"],
            loop_protection={"max_iterations": 10, "blackboard_change_threshold": 0.1},
        ),
        AGENT_CONTEXT_CRITIC: AgentManifest(
            agent_type=AGENT_CONTEXT_CRITIC,
            name="Context Critic",
            description="Evaluates context completeness and quality.",
            prompt_template="You are a context critic agent. Evaluate context completeness and quality.",
            tools=["context_review", "gap_identify", "bias_check"],
            loop_protection={"max_iterations": 8, "blackboard_change_threshold": 0.2},
        ),
        AGENT_ROUTE_CRITIC: AgentManifest(
            agent_type=AGENT_ROUTE_CRITIC,
            name="Route Critic",
            description="Evaluates OmniRoute selection.",
            prompt_template="You are a route critic agent. Evaluate OmniRoute selection.",
            tools=["route_analyze", "model_compare", "cost_estimate"],
            loop_protection={"max_iterations": 8, "blackboard_change_threshold": 0.2},
        ),
    }


class AgentRun:
    """Lifecycle tracking for a single agent execution."""

    def __init__(
        self,
        task_id: str,
        agent_type: AgentType,
        model: str,
        route: str | None = None,
    ) -> None:
        self.task_id = task_id
        self.agent_type = agent_type
        self.model = model
        self.route = route
        self.start_time = datetime.now(UTC)
        self.end_time: datetime | None = None
        self.status: str = "running"  # running|success|failure|timeout|cancelled
        self.token_count: int = 0
        self.tool_calls: list[dict[str, Any]] = []
        self.output: str | None = None
        self.blackboard = Blackboard(task_id=task_id)
        self._loop_detector = LoopDetector()

    def record_tool_call(self, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        """Record a tool call for loop detection."""
        self.tool_calls.append({
            "tool": tool,
            "args": args,
            "result": result,
            "timestamp": datetime.now(UTC).isoformat(),
        })
        self._loop_detector.record_call(tool, args, result)

    def mark_success(self, output: str, token_count: int) -> None:
        """Mark the run as successful."""
        self.status = "success"
        self.output = output
        self.token_count = token_count
        self.end_time = datetime.now(UTC)

    def mark_failure(self, error: str) -> None:
        """Mark the run as failed."""
        self.status = "failure"
        self.output = error
        self.end_time = datetime.now(UTC)

    def is_loop_detected(self) -> bool:
        """Check if loop protection triggered."""
        return self._loop_detector.is_loop()

    def as_dict(self) -> dict[str, Any]:
        """Return run state as a dictionary."""
        return {
            "task_id": self.task_id,
            "agent_type": self.agent_type,
            "model": self.model,
            "route": self.route,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "status": self.status,
            "token_count": self.token_count,
            "tool_calls": self.tool_calls,
            "output": self.output,
            "blackboard": self.blackboard.as_dict(),
            "is_loop_detected": self.is_loop_detected(),
        }


class LoopDetector:
    """Loop protection per spec §75.

    Detects:
    - repeated identical tool calls
    - repeated identical failures
    - repeated equivalent model output
    - no blackboard state change
    - no new evidence
    """

    def __init__(
        self,
        max_identical_calls: int = 5,
        max_iterations: int = 10,
        min_blackboard_change: float = 0.1,
    ) -> None:
        self.max_identical_calls = max_identical_calls
        self.max_iterations = max_iterations
        self.min_blackboard_change = min_blackboard_change
        self._call_history: list[dict[str, Any]] = []
        self._iteration_count = 0

    def record_call(self, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        """Record a tool call and update loop state."""
        self._call_history.append({
            "tool": tool,
            "args_hash": hash(frozenset(args.items())),
            "result_hash": hash(frozenset(result.items())),
            "timestamp": datetime.now(UTC).isoformat(),
        })
        self._iteration_count += 1

    def is_loop(self) -> bool:
        """Check if loop protection triggered."""
        if self._iteration_count > self.max_iterations:
            return True

        # Check for repeated identical calls
        if len(self._call_history) >= self.max_identical_calls:
            last_calls = self._call_history[-self.max_identical_calls:]
            if len({c["args_hash"] for c in last_calls}) == 1:
                return True

        return False
