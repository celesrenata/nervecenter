# NerveCenter

**Version:** 0.2 Initial Build Specification  
**Status:** Ready for Spec Kit decomposition  
**Primary deployment:** Local-first / self-hosted  
**Primary orchestration client:** Zoo  
**Primary inference fabric:** OmniRoute  
**Primary build host for Apple applications:** M5 Max macOS system  
**Primary interaction surfaces:** Web, CLI, VS Code/Zoo, iPhone, Apple Watch  
**Architecture style:** Event-sourced, temporal, evidence-backed, agentic control plane

---

# 1. Executive Summary

NerveCenter is a persistent AI engineering operating system.

It continuously observes, models, and reasons about:

- repositories
- infrastructure
- hosts
- Kubernetes
- Nix
- services
- GPUs
- inference backends
- models
- OmniRoute routes
- agent activity
- logs
- metrics
- deployments
- configuration
- incidents
- experiments
- architectural decisions
- tasks
- long-running goals

Existing coding agents largely operate within disposable task contexts.

NerveCenter instead owns persistent engineering state.

It knows:

```text
what exists
what changed
what is running
what failed
what has already been tried
what evidence exists
which models work well for which workloads
which agents are currently working
which tasks can execute concurrently
what remains unfinished
```

NerveCenter is not itself primarily an inference gateway.

**OmniRoute is the inference fabric.**

NerveCenter determines:

```text
WHAT needs to happen
WHY it needs to happen
WHAT context is required
WHAT capabilities are required
WHAT evidence is relevant
HOW work should be decomposed
WHICH work can execute concurrently
HOW outputs should be verified
```

OmniRoute determines:

```text
WHERE inference executes
HOW a provider/backend is invoked
WHICH physical model instance receives the request
HOW fallback and provider routing occur
```

Zoo acts as a major engineering execution workforce.

The relationship is:

```text
               ┌──────────────────┐
               │      Human       │
               └────────┬─────────┘
                        │
              ┌─────────▼─────────┐
              │   NerveCenter     │
              │ brain / memory /  │
              │ evidence / goals  │
              └─────────┬─────────┘
                        │
           ┌────────────┼────────────┐
           │            │            │
           ▼            ▼            ▼
         Zoo        Native Agents   Tools/MCP
      workforce
           │
           └────────────┬────────────┘
                        ▼
                  ┌───────────┐
                  │ OmniRoute │
                  └─────┬─────┘
                        │
        ┌───────────────┼─────────────────┐
        ▼               ▼                 ▼
      RTX 5090       4070 Ti SUPER     M5 Max
       vLLM             local           MLX/etc.
        │
        └────────── Cloud / OAuth / API providers
```

The iPhone and Apple Watch provide remote command, observability, incident response, approvals, and lightweight queries.

---

# 2. North-Star Capability

NerveCenter should eventually answer:

> What is happening, why is it happening, what changed, what do we already know, what has already been attempted, what should happen next, and which parts can we work on simultaneously?

Every major architectural decision should improve the system's ability to answer that question.

---

# 3. Core Philosophy

The fundamental pipeline is:

```text
REAL SYSTEMS
    ↓
EVENTS
    ↓
NORMALIZED STATE
    ↓
TEMPORAL KNOWLEDGE
    ↓
CONTEXT COMPILATION
    ↓
PLANNING
    ↓
TASK DAG
    ↓
AGENTS
    ↓
OMNIROUTE
    ↓
MODELS / TOOLS
    ↓
CLAIMS + ARTIFACTS
    ↓
VERIFICATION
    ↓
NEW KNOWLEDGE
```

LLMs are workers.

They are not memory.

They are not the source of truth.

They are not trusted merely because they sound confident.

---

# 4. Product Goals

NerveCenter must provide:

## 4.1 Persistent Engineering Memory

Retain durable knowledge about:

```text
systems
repositories
hosts
services
models
deployments
failures
fixes
decisions
experiments
benchmarks
incidents
goals
tasks
agent investigations
```

Memory survives:

```text
model changes
agent changes
IDE restarts
conversation deletion
machine restart
server restart
```

---

## 4.2 Temporal Understanding

Historical state must be reconstructable.

Queries such as:

```text
"What changed between when this worked and when it broke?"

"What was running before the deployment?"

"When did GPU utilization change?"

"Which commit preceded this failure?"
```

must be first-class operations.

---

## 4.3 Evidence-Backed Reasoning

Assertions are classified.

```text
OBSERVATION
FACT
DERIVED FACT
CLAIM
HYPOTHESIS
DECISION
RECOMMENDATION
UNKNOWN
```

Example:

```json
{
  "statement": "/dev/dri/renderD128 disappeared from the workload",
  "type": "FACT",
  "confidence": 1.0,
  "evidence": [
    "snapshot:before",
    "snapshot:after"
  ]
}
```

Versus:

```json
{
  "statement": "The Nix change caused QSV access to disappear",
  "type": "HYPOTHESIS",
  "confidence": 0.91,
  "evidence": [
    "commit:81fa93",
    "deployment:29211",
    "snapshot:after"
  ]
}
```

---

## 4.4 Parallel Agent Execution

Tasks are DAGs.

Independent work executes concurrently.

The scheduler should optimize for:

```text
critical-path wall-clock time
resource availability
dependency constraints
model capability
token/context requirements
verification requirements
```

This is a core feature, not an optimization.

---

## 4.5 Persistent Goals

Goals can live for days or months.

Example:

```text
Make HelloDJ's video pipeline production ready.
```

The system remembers:

```text
definition of done
tasks
decisions
experiments
failures
blockers
acceptance criteria
artifacts
progress
```

---

## 4.6 Empirical Model Intelligence

NerveCenter observes model performance through OmniRoute.

Routing intelligence should eventually derive from actual results.

Questions should become answerable such as:

```text
Which model performs best on Kubernetes diagnosis?

At what context size does this Qwen deployment become unstable?

Does DeepSeek justify the slower inference time for architecture tasks?

Which model wastes the fewest tool calls on repository exploration?

Which quantization gives the best verified coding result per GB of VRAM?
```

---

# 5. Major Components

```text
NerveCenter
│
├── Knowledge Plane
├── Event Plane
├── Evidence Plane
├── Goal Plane
├── Execution Plane
├── Context Plane
├── Inference Control Plane
├── Policy Plane
├── Client Plane
└── Observability Plane
```

---

# 6. High-Level Architecture

```text
                       CLIENT PLANE

        ┌──────────┬─────────┬──────────┬──────────┐
        │ VS Code  │   Web   │  iPhone  │  Watch   │
        │ / Zoo    │         │          │          │
        └────┬─────┴────┬────┴────┬─────┴────┬─────┘
             │          │         │          │
             └──────────┴────┬────┴──────────┘
                             ▼

                     NerveCenter API
                             │
 ┌───────────────────────────┼────────────────────────────┐
 │                           │                            │
 ▼                           ▼                            ▼
Knowledge                Goals/Tasks                  Context
Service                  Orchestrator                 Compiler
 │                           │                            │
 │                           ▼                            │
 │                      DAG Scheduler                    │
 │                           │                            │
 │                           ▼                            │
 │                    Agent Workforce                    │
 │                  ┌────────┴─────────┐                 │
 │                  │                  │                 │
 │                  ▼                  ▼                 │
 │                 Zoo            Native Agents         │
 │                  │                  │                 │
 └──────────────────┴──────────┬───────┴─────────────────┘
                               ▼

                     Inference Policy Engine
                               │
                               ▼

                         OMNIROUTE
                               │
          ┌────────────────────┼─────────────────────┐
          ▼                    ▼                     ▼
        vLLM                 Ollama                  MLX
       RTX 5090         RTX 4070 Ti SUPER         M5 Max
          │
          ├──────────────── Cloud APIs
          ├──────────────── OAuth providers
          ├──────────────── OpenAI-compatible APIs
          └──────────────── future providers


                       OBSERVATION PLANE

Git ─────┐
K8s ─────┤
Nix ─────┤
Hosts ───┤
GPU ─────┼──► Event Bus ─► Event Processor ─► Knowledge
Logs ────┤
Metrics ─┤
Zoo ─────┤
OmniRoute┤
MCP ─────┘
```

---

# 7. Separation of Responsibilities

## NerveCenter

Owns:

```text
engineering state
goals
task graphs
context
provenance
model-performance history
agent history
evidence
policy
verification
system understanding
```

## OmniRoute

Owns:

```text
provider integration
model endpoints
authentication
provider selection mechanics
inference transport
fallback routing
model availability
OAuth/provider integrations
```

## Zoo

Owns:

```text
execution of delegated engineering work
subagent coordination
repository operations
implementation
tests
artifacts
progress reporting
```

Zoo does NOT own canonical project memory.

## Models

Models are disposable execution resources.

---

# 8. Repository Structure

Initial monorepo:

```text
nervecenter/
│
├── backend/
│   ├── api/
│   ├── events/
│   ├── knowledge/
│   ├── evidence/
│   ├── goals/
│   ├── tasks/
│   ├── context/
│   ├── orchestration/
│   ├── scheduler/
│   ├── agents/
│   ├── inference/
│   ├── policy/
│   ├── connectors/
│   ├── observability/
│   └── persistence/
│
├── schemas/
│
├── sdk/
│   ├── python/
│   └── typescript/
│
├── mcp/
│
├── cli/
│
├── web/
│
├── apple/
│   ├── Shared/
│   ├── iOS/
│   ├── Watch/
│   ├── Widgets/
│   └── Tests/
│
├── agents/
│
├── deployments/
│   ├── nix/
│   ├── kubernetes/
│   └── local/
│
├── synthetic-lab/
│
├── migrations/
│
├── tests/
│
└── docs/
```

---

# 9. Core Data Model

Core objects:

```text
Event
Entity
Relationship
Observation
Evidence
Claim
Decision
Task
TaskRun
Goal
Agent
AgentRun
Model
ModelInstance
InferenceRoute
ComputeNode
ContextPackage
Incident
Experiment
Artifact
PlannedChange
Approval
```

These interfaces should be frozen early.

---

# 10. Event Model

All meaningful activity enters NerveCenter through immutable events.

```json
{
  "event_id": "uuid",
  "event_type": "kubernetes.pod.restart",
  "source": "kubernetes",
  "source_instance": "home-cluster",
  "timestamp": "RFC3339",
  "observed_at": "RFC3339",

  "actor": {
    "type": "human|agent|system",
    "id": "..."
  },

  "entity_refs": [],

  "correlation": {
    "trace_id": "...",
    "goal_id": "...",
    "task_id": "...",
    "agent_run_id": "..."
  },

  "payload": {},
  "metadata": {},
  "hash": "sha256"
}
```

Events cannot be edited.

Corrections produce additional events.

---

# 11. Initial Event Types

```text
git.commit
git.branch.changed
git.diff
git.tag
git.checkout

filesystem.changed

kubernetes.pod.created
kubernetes.pod.deleted
kubernetes.pod.restart
kubernetes.deployment.changed
kubernetes.event
kubernetes.node.changed

nix.configuration.changed
nix.rebuild.started
nix.rebuild.completed
nix.rebuild.failed

host.started
host.stopped
host.resource.snapshot
host.device.changed

gpu.resource.snapshot
gpu.model.loaded
gpu.model.unloaded

omniroute.request
omniroute.route.selected
omniroute.fallback
omniroute.response
omniroute.failure

inference.request
inference.response

agent.started
agent.progress
agent.claim
agent.hypothesis
agent.artifact
agent.completed
agent.failed

goal.created
goal.updated
goal.completed

task.created
task.ready
task.started
task.completed
task.failed
task.blocked

decision.created
decision.superseded

incident.created
incident.updated
incident.resolved

experiment.started
experiment.measurement
experiment.completed

planned_change.created
planned_change.approved
planned_change.executed
planned_change.failed
planned_change.rolled_back
```

---

# 12. Entity Model

Initial entity classes:

```text
Host
GPU
Repository
Commit
Branch
Project
Service
Container
Pod
Deployment
Configuration
Model
ModelInstance
InferenceBackend
InferenceRoute
Agent
AgentRun
Task
Goal
Decision
Claim
Evidence
Incident
Experiment
Artifact
ExternalService
Connector
```

Example:

```json
{
  "entity_id": "uuid",
  "entity_type": "host",
  "canonical_name": "esnixi",
  "aliases": [],
  "created_at": "...",
  "metadata": {}
}
```

---

# 13. Temporal Relationships

Relationships must include validity windows.

```json
{
  "relationship_id": "uuid",
  "from": "service:hellodj",
  "type": "RUNS_ON",
  "to": "host:gremlin-1",
  "valid_from": "...",
  "valid_until": null,
  "derived_from": [
    "event:..."
  ]
}
```

Relationship examples:

```text
SERVICE RUNS_ON HOST
SERVICE DEPENDS_ON SERVICE
SERVICE DEPLOYED_FROM REPOSITORY

DEPLOYMENT DEPLOYS COMMIT

COMMIT MODIFIES CONFIGURATION

MODEL SERVED_BY BACKEND
MODEL INSTANCE_OF MODEL
MODEL_INSTANCE RUNS_ON GPU

OMNIROUTE_ROUTE TARGETS MODEL_INSTANCE

AGENT_RUN USED MODEL
AGENT_RUN PRODUCED CLAIM
AGENT_RUN PART_OF TASK

TASK PART_OF GOAL
TASK DEPENDS_ON TASK

CLAIM SUPPORTED_BY EVIDENCE

DECISION SUPERSEDES DECISION

INCIDENT AFFECTS SERVICE
```

---

# 14. Persistence

Recommended MVP stack:

```text
PostgreSQL
    canonical entities
    relationships
    tasks
    goals
    decisions
    provenance
    structured events

pgvector
    semantic retrieval

Object Storage
    logs
    diffs
    model transcripts
    artifacts
    snapshots

NATS JetStream
    event stream
```

Do not introduce a dedicated graph database until actual graph-query requirements justify it.

---

# 15. Evidence System

Evidence is immutable.

```json
{
  "evidence_id": "...",
  "type": "log|metric|event|source|diff|test|snapshot",
  "source": "...",
  "timestamp": "...",
  "content_ref": "...",
  "hash": "...",
  "entity_refs": []
}
```

Claims reference evidence.

Claims must retain provenance.

---

# 16. Claim Model

```json
{
  "claim_id": "...",

  "statement": "...",

  "classification": "FACT|DERIVED_FACT|HYPOTHESIS|RECOMMENDATION",

  "confidence": 0.91,

  "producer": {
    "agent": "...",
    "model": "...",
    "route": "...",
    "prompt_version": "..."
  },

  "evidence": [],

  "verification": {
    "state": "unverified|verified|rejected|conflicted"
  }
}
```

---

# 17. Context Compiler

The Context Compiler turns engineering reality into bounded task context.

Pipeline:

```text
request
   ↓
intent detection
   ↓
entity resolution
   ↓
active goal lookup
   ↓
temporal scope
   ↓
graph traversal
   ↓
recent changes
   ↓
semantic retrieval
   ↓
source/dependency expansion
   ↓
evidence ranking
   ↓
token budgeting
   ↓
deduplication
   ↓
ContextPackage
```

---

# 18. ContextPackage

```json
{
  "task": {},
  "goal": {},
  "entities": [],
  "current_state": {},
  "recent_changes": [],
  "decisions": [],
  "known_facts": [],
  "open_hypotheses": [],
  "source_fragments": [],
  "logs": [],
  "metrics": [],
  "prior_failures": [],
  "prior_agent_runs": [],
  "token_budget": 64000
}
```

The Context Compiler should reduce the need for indiscriminate 200K+ context windows.

---

# 19. Context Prioritization

Priority:

```text
1. directly referenced entities
2. active task requirements
3. current runtime state
4. recent related changes
5. applicable architectural decisions
6. direct supporting evidence
7. source definitions
8. dependency context
9. relevant historical incidents
10. semantic background
```

The compiler records what it excluded and why.

---

# 20. Goal Model

```json
{
  "goal_id": "...",
  "title": "Make HelloDJ video pipeline production ready",
  "status": "ACTIVE",

  "success_criteria": [],

  "tasks": [],

  "decisions": [],

  "blockers": [],

  "artifacts": [],

  "created_at": "...",
  "target_date": null
}
```

States:

```text
DRAFT
ACTIVE
BLOCKED
COMPLETE
ABANDONED
```

---

# 21. Goal Reconciliation

Goals are reconciled against actual system state.

Example:

```text
Criterion:
video pipeline survives worker restart

Evidence:
three deterministic restart tests succeeded

Status:
SATISFIED
```

When tool verification is possible, an LLM's assertion alone is insufficient.

---

# 22. Task DAG

Task:

```json
{
  "task_id": "...",
  "title": "...",
  "description": "...",

  "capabilities": [
    "repository-analysis"
  ],

  "dependencies": [],

  "resources": {},

  "priority": 50,

  "estimated_context_tokens": 20000,

  "verification_policy": "tool-based",

  "state": "READY"
}
```

States:

```text
PENDING
READY
RUNNING
BLOCKED
VERIFYING
SUCCEEDED
FAILED
CANCELLED
```

---

# 23. Parallel Scheduler

Runnable condition:

```text
task.state == READY
AND
all dependencies satisfied
AND
capability exists
AND
required resources available
AND
policy permits execution
```

The scheduler must dispatch independent tasks concurrently.

A planner generating:

```text
A → B → C → D
```

when the work could be:

```text
     ┌→ B ─┐
A ───┼→ C ─┼→ F
     ├→ D ─┤
     └→ E ─┘
```

is defective.

---

# 24. Zoo Integration

Zoo is a first-class execution workforce.

NerveCenter communicates with Zoo through:

```text
MCP
HTTP API
event stream/callback
```

Zoo receives:

```text
task
context package
permissions
resource constraints
expected artifacts
verification requirements
```

Zoo reports:

```text
started
progress
claims
hypotheses
artifacts
tool activity
blocked
completed
failed
```

NerveCenter owns canonical task state.

---

# 25. Shared Agent Blackboard

Agents communicate through structured state.

```json
{
  "task_id": "...",

  "facts": [],

  "hypotheses": [],

  "questions": [],

  "artifacts": [],

  "blockers": [],

  "agent_runs": [],

  "consensus": null
}
```

Entries are append-only.

Corrections supersede previous entries.

---

# 26. Native Agent Types

Initial agents:

```text
Planner
RepositoryInspector
RuntimeInspector
LogInvestigator
InfrastructureInspector
ArchitectureAnalyst
IncidentCorrelator
CodeReviewer
TestRunner
BenchmarkAgent
ResearchAgent
Verifier
ContextCritic
RouteCritic
```

Agents should largely consist of:

```text
capabilities
prompt
tools
policy
verification
```

Avoid constructing a huge custom agent framework.

---

# 27. OmniRoute as the Inference Fabric

OmniRoute is the preferred model access path for all NerveCenter and Zoo inference.

NerveCenter should not replicate OmniRoute's provider abstraction.

Architecture:

```text
NerveCenter
      ↓
Inference Policy Engine
      ↓
OmniRoute
      ↓
Provider / endpoint routing
      ↓
physical inference resource
```

---

# 28. Direct Inference Fallback

NerveCenter MAY support a small emergency direct-backend interface.

Purpose:

```text
OmniRoute unavailable
development bootstrap
diagnostics
minimal recovery mode
```

It should not become a duplicate provider framework.

---

# 29. OmniRoute Request Metadata

Every request initiated under NerveCenter should carry correlation metadata.

```json
{
  "nerve_trace_id": "...",
  "nerve_goal_id": "...",
  "nerve_task_id": "...",
  "nerve_agent_id": "...",
  "nerve_agent_run_id": "...",
  "verification_policy": "..."
}
```

OmniRoute must preserve this metadata through telemetry.

---

# 30. OmniRoute Telemetry

NerveCenter should consume:

```text
logical model request
physical model chosen
provider
endpoint
compute node
quantization
context length
prompt tokens
completion tokens
TTFT
tokens/second
total latency
tool-call behavior
retries
fallbacks
errors
cost
VRAM state where available
```

And later correlate with:

```text
task success
verification result
human correction
rework
agent loop behavior
```

---

# 31. Inference Policy Engine

NerveCenter should not merely request model names.

It may request capabilities.

Example:

```json
{
  "task_id": "...",

  "requirements": {
    "capabilities": [
      "coding",
      "reasoning",
      "tools"
    ],

    "context_tokens": 48000,

    "vision": false
  },

  "policy": {
    "prefer_local": true,
    "max_cost_usd": 0.05,
    "min_historical_success": 0.85,
    "max_expected_latency_ms": 120000
  },

  "preferred_nodes": [
    "esnixi",
    "stabulous"
  ]
}
```

---

# 32. Route Candidate Interface

OmniRoute should optionally expose route candidates.

Example:

```json
{
  "routes": [
    {
      "model": "deepseek-v4",
      "backend": "mlx",
      "node": "stabulous",
      "availability": "ready"
    },
    {
      "model": "qwen-code",
      "backend": "vllm",
      "node": "esnixi",
      "availability": "ready"
    }
  ]
}
```

NerveCenter may apply policy and historical intelligence before final selection.

---

# 33. Model Registry

NerveCenter maintains logical model intelligence.

```json
{
  "model_id": "...",

  "capabilities": [
    "coding",
    "reasoning",
    "tools",
    "long-context"
  ],

  "known_routes": [],

  "max_context": 200000,

  "historical_metrics": {}
}
```

Physical provider configuration remains primarily OmniRoute's responsibility.

---

# 34. Compute Registry

Nodes advertise state.

Example:

```json
{
  "node": "esnixi",
  "architecture": "x86_64",

  "accelerators": [
    {
      "type": "cuda",
      "name": "RTX 5090",
      "memory_total_mb": 32768,
      "memory_free_mb": 28000
    }
  ],

  "inference_backends": [
    "vllm"
  ],

  "load": 0.31
}
```

Apple node:

```json
{
  "node": "stabulous",
  "architecture": "arm64",
  "unified_memory_mb": 131072,
  "inference_backends": [
    "mlx"
  ]
}
```

---

# 35. Empirical Model Metrics

Track:

```text
task class
model
route
quantization
context size
input tokens
output tokens
latency
tool-call count
tool failures
loop detection
verification score
human correction
retries
VRAM
result
```

These produce user-specific intelligence.

---

# 36. Model Routing Feedback

NerveCenter may eventually recommend changes to OmniRoute.

Example:

```text
Repository-analysis workloads:

Current route:
DeepSeek on M5 Max

Observed:
Qwen-small achieved comparable verified success
at 3.7x lower latency.

Recommendation:
route repository-analysis to Qwen-small
with DeepSeek fallback.
```

Automatic changes require policy authorization.

---

# 37. Verification

Supported verification modes:

```text
none
structural
tool-based
test-based
second-model
multi-model
human
```

Example code task:

```text
agent writes patch
      ↓
lint
      ↓
typecheck
      ↓
tests
      ↓
independent review
      ↓
verified
```

---

# 38. Incident Investigation

Workflow:

```text
incident reported
      ↓
resolve affected entities
      ↓
find last known-good state
      ↓
collect recent changes
      ↓
parallel investigations
      ↓
shared blackboard
      ↓
timeline correlation
      ↓
hypothesis ranking
      ↓
verification
      ↓
remediation proposal
      ↓
post-change verification
      ↓
incident record
```

Parallel branches may include:

```text
Git
Kubernetes
logs
metrics
host state
GPU state
configuration
network
recent agent actions
```

---

# 39. "What Changed?" Engine

Given:

```text
entity
time A
time B
```

return deterministic differences:

```text
Git changes
deployment changes
configuration
host state
model changes
resource changes
dependencies
runtime events
agent actions
```

Use LLM interpretation only after computing raw change sets.

---

# 40. Planned Changes

Important mutations are represented explicitly.

```json
{
  "change_id": "...",

  "target": "...",

  "description": "...",

  "operations": [],

  "expected_effect": "...",

  "blast_radius": [],

  "rollback": [],

  "verification": []
}
```

---

# 41. Mutation Safety

Categories:

```text
SAFE
REVERSIBLE
DESTRUCTIVE
EXTERNAL
```

Examples:

```text
kubectl get pods
SAFE

restart development workload
REVERSIBLE

delete persistent volume
DESTRUCTIVE

send email
EXTERNAL
```

Policies determine approval requirements.

---

# 42. Capability Security

Example capabilities:

```text
git.read
git.write

kubernetes.read
kubernetes.restart
kubernetes.modify
kubernetes.delete

host.read
host.shell.readonly
host.shell.mutable

aws.logs.read
aws.resource.modify

omniroute.inspect
omniroute.route.recommend
omniroute.route.modify

nerve.goal.create
nerve.change.approve
```

---

# 43. Secrets

Secrets must never be persisted inside:

```text
event text
knowledge graph descriptions
embeddings
agent transcripts
training datasets
context packages
```

Use references to an external secret provider.

---

# 44. Connectors

Connector interface:

```python
class Connector:
    async def snapshot(self):
        ...

    async def subscribe(self):
        ...

    async def execute(self, action):
        ...
```

Initial connectors:

```text
Git
GitHub
filesystem
Kubernetes
Nix
Docker
systemd
NVIDIA telemetry
Apple/Metal telemetry
Prometheus
Loki
CloudWatch
OmniRoute
Zoo
MCP
```

Direct model connectors are secondary.

---

# 45. MCP

NerveCenter must function as both:

```text
MCP client
MCP server
```

Initial MCP tools exposed:

```text
search_entities
get_entity
get_entity_timeline
get_recent_changes

get_goal
create_goal
update_goal

get_task
create_task

report_claim
report_artifact

compile_context

get_compute
get_models
get_route_history

get_incident
start_investigation

create_planned_change
```

Zoo should be able to use these directly.

---

# 46. HTTP API

Groups:

```text
/api/events
/api/entities
/api/relationships
/api/evidence
/api/claims
/api/search

/api/context

/api/goals
/api/tasks

/api/agents
/api/runs

/api/incidents

/api/models
/api/compute
/api/routes

/api/changes
/api/approvals

/api/clients
```

OpenAPI is canonical.

Generate client libraries where practical.

## §63 API mapping (personal surface → flat API)

The §63 personal surface names are *mapped* (not aliased) onto the flat API:
the canonical flat route is the only implementation, and the personal name is
the conceptual label. Every mapped endpoint is registered and returns `200`
with an empty-list envelope on a fresh backend (no `NERVE_DB_URL`); no existing
route's behavior changes.

| §63 personal surface | Flat route (canonical) | Implementation module |
| --- | --- | --- |
| `/api/personal/attention` | `GET /api/attention` | `backend/api/endpoints/attention.py` |
| `/api/personal/people` | `GET /api/people` | `backend/api/endpoints/people.py` |
| `/api/personal/communications` | `GET /api/communications` | `backend/api/endpoints/communications.py` |
| `/api/personal/briefings` | `GET /api/briefing` | `backend/api/endpoints/briefing.py` |
| `/api/personal/briefings/operational` | `GET /api/briefing/operational` | `backend/api/endpoints/briefing.py` (R109) |
| `/api/personal/drafts` | `GET /api/drafts` | `backend/api/endpoints/drafts.py` |

The Daily Operational State (`GET /api/briefing/operational`, §52) is a bare
object (not the `ListResponse` envelope) consumed by the `nerve today` CLI
(§65, AC-R11) and the `get_daily_brief` MCP tool (§64, AC-R10). Its eight
sections each read from their owning service — detection (commitments /
requests / deadlines), waiting-on, calendar, and the persistence-backed
`app.state.services` map (goals / incidents / approvals); an unwired service
renders its section as an honest empty list.

---

# 47. CLI

Examples:

```bash
nerve status

nerve ask "why did hellodj restart?"

nerve entity hellodj

nerve changes hellodj --since 4h

nerve goal list
nerve goal show <id>

nerve task list

nerve agents

nerve compute

nerve routes

nerve incident investigate hellodj

nerve pause-agents

nerve stop-mutations
```

All commands support JSON output.

---

# 48. Web UI

Primary views:

```text
Overview
Systems
Timeline
Goals
Task DAG
Agents
Inference
Incidents
Decisions
Ask
```

---

# 49. Overview

Show:

```text
overall health

active incidents

active goals

running agents

task concurrency

compute utilization

OmniRoute activity

recent deployments

recent failures

pending approvals
```

---

# 50. Task DAG UI

Must visibly distinguish:

```text
PENDING
READY
RUNNING
BLOCKED
VERIFYING
SUCCEEDED
FAILED
```

Concurrent execution must be visually obvious.

---

# 51. Inference UI

Show:

```text
physical nodes
loaded models
OmniRoute routes
active inference calls
tokens/sec
context usage
latency
failures
fallbacks
historical model performance
```

Example:

```text
esnixi
RTX 5090
Qwen Code
71% GPU
64 tok/s

gremlin-1
RTX 4070 Ti SUPER
Idle

stabulous
M5 Max
DeepSeek
91 GB unified memory
35 tok/s
```

---

# 52. Apple Platform

iPhone and Apple Watch are core supported clients.

The Apple applications are built from the M5 Max macOS development system.

They are native Swift applications.

---

# 53. Apple Project Structure

```text
apple/
│
├── NerveCenter.xcodeproj
│
├── Shared/
│   ├── Models/
│   ├── API/
│   ├── Auth/
│   ├── Networking/
│   ├── Persistence/
│   ├── Realtime/
│   └── DesignSystem/
│
├── iOS/
│   ├── App/
│   ├── Features/
│   └── Resources/
│
├── Watch/
│   ├── App/
│   ├── Features/
│   └── Resources/
│
├── Widgets/
│   ├── Shared/
│   ├── iOS/
│   └── Watch/
│
└── Tests/
```

---

# 54. Apple Technology

Prefer:

```text
Swift
SwiftUI
Swift Concurrency
URLSession
WebSocket APIs
Network.framework where appropriate
Keychain
LocalAuthentication
WatchConnectivity
WidgetKit
ActivityKit
UserNotifications
Swift Package Manager
```

Avoid unnecessary dependencies.

---

# 55. Shared Swift Modules

Suggested:

```text
NerveModels
NerveAPI
NerveAuth
NervePersistence
NerveRealtime
NerveDesign
```

Swift API types should be generated from canonical OpenAPI schemas when practical.

---

# 56. iPhone Application

Primary tabs:

```text
Overview
Goals
Incidents
Agents
Systems
Ask
```

---

# 57. iPhone Overview

Example:

```text
NERVECENTER

System                HEALTHY

Agents
7 running
3 waiting

Compute
esnixi       5090        72%
gremlin-1    4070 Ti     14%
stabulous    M5 Max      58%

Incidents
1 active

Goals
HelloDJ Production       76%
NerveCenter MVP          33%

Pending approvals
2
```

---

# 58. Mobile Ask Interface

Examples:

```text
"What broke?"

"What changed since I left?"

"What's Zoo working on?"

"Why did that task go to the M5?"

"What is using the 5090?"

"Did the benchmark finish?"

"How far along is NerveCenter itself?"

"What blockers does HelloDJ have?"
```

Answers retain provenance.

Evidence is inspectable.

---

# 59. iPhone Incidents

Incident view contains:

```text
summary
timeline
affected entities
facts
hypotheses
confidence
supporting evidence
agents investigating
recommended actions
planned changes
verification
```

---

# 60. Notifications

Notification classes:

```text
INFORMATIONAL
ACTION_REQUIRED
URGENT
```

Notify for:

```text
incident detected
critical task failed
goal blocked
approval required
investigation completed
deployment failure
benchmark completed
goal completed
```

Do not notify for routine system events.

---

# 61. Live Activities

Suitable operations:

```text
incident investigation
deployment
benchmark campaign
migration
large goal operation
```

Example:

```text
HELLODJ INCIDENT

4/6 investigations complete

Git        ✓
Logs       ✓
K8s        ✓
Host       ✓
Network    …
Verify     …

73%
```

---

# 62. iPhone Widgets

Small:

```text
● HEALTHY
7 agents
0 incidents
```

Medium:

```text
5090       72%
4070       14%
M5         58%

Agents      7
Incidents   0
```

Large:

```text
goal progress
compute state
incidents
agent activity
```

---

# 63. Apple Watch

The Watch application is not a miniature dashboard.

Its goals are:

```text
glance
alert
query
acknowledge
approve
reject
stop
```

Primary question:

> Does something need me?

---

# 64. Watch Home

Healthy:

```text
NERVE

● Healthy

Agents      7
Incidents   0
Approvals   0

5090       72%
```

Problem state:

```text
NERVE

🔴 1 INCIDENT

HelloDJ
Video Worker

8m

[Details]
```

---

# 65. Watch Queries

Examples:

```text
"What broke?"

"What's running?"

"Any blockers?"

"How's HelloDJ?"

"What's using the 5090?"

"Where is Zoo?"
```

Responses should be short.

---

# 66. Watch Approvals

Watch may approve:

```text
LOW-RISK
REVERSIBLE
```

operations.

Example:

```text
Restart HelloDJ
video-worker?

Risk:
REVERSIBLE

Expected interruption:
8 sec

[Approve]
[Reject]
```

By default Watch should NOT authorize:

```text
persistent storage deletion
credential mutation
production DB destructive changes
high-blast-radius infrastructure mutation
security-policy changes
```

Those deep-link to iPhone.

---

# 67. Emergency Controls

Expose:

```text
STOP AGENTS
```

Behavior:

```text
stop dispatching new agent work
cancel cancellable jobs
revoke unused mutation authorization
keep monitoring active
```

Also:

```text
STOP MUTATIONS
```

which preserves read-only investigation.

---

# 68. Watch Complications / Smart Stack

Potential complications:

```text
system health
incident count
agent count
GPU state
goal progress
approval count
```

Priority:

```text
urgent incident
↓
approval required
↓
active incident
↓
active investigation
↓
goal progress
↓
general status
```

---

# 69. Phone ↔ Watch State

Use a compact synchronized state.

```json
{
  "system_health": "healthy",
  "active_incidents": 0,
  "running_agents": 7,
  "pending_approvals": 1,
  "primary_goal_progress": 0.76,
  "updated_at": "..."
}
```

Watch must remain useful from cached state.

Stale information must be marked clearly.

---

# 70. Local Connectivity

Supported:

```text
LAN
WireGuard
VPN
secure reverse proxy
other user-controlled secure ingress
```

NerveCenter must not require public Internet exposure.

Bonjour/local discovery may be supported.

Manual server configuration remains available.

---

# 71. Authentication

Device pairing:

```text
iPhone
   ↓
pairing challenge
   ↓
NerveCenter server
   ↓
device credential
   ↓
Keychain
```

The user should not need to repeatedly copy API tokens.

---

# 72. Sensitive Approval

Sensitive mobile operations should require biometric authorization where appropriate.

Authorization applies to an exact PlannedChange.

Example:

```json
{
  "approval_id": "...",
  "change_id": "...",
  "device_id": "...",
  "expires_at": "...",
  "nonce": "..."
}
```

Any modification to the PlannedChange invalidates approval.

---

# 73. Local Cache

iPhone cache:

```text
systems
recent incidents
goals
agent activity
compute state
pending approvals
recent responses
```

Watch cache:

```text
health
incidents
approvals
goal summary
compute summary
```

---

# 74. Observability

Use OpenTelemetry.

Every request should correlate:

```text
user request
↓
context compilation
↓
planning
↓
task DAG
↓
Zoo/native agent execution
↓
OmniRoute inference
↓
tool calls
↓
verification
↓
response
```

One trace ID should connect the entire execution.

---

# 75. Agent Loop Protection

Every agent run requires:

```text
timeout
maximum tokens
maximum tool calls
maximum retries
cancellation
progress detection
```

Loop detection examples:

```text
repeated identical tool calls
repeated identical failures
repeated equivalent model output
no blackboard state change
no new evidence
```

---

# 76. Initial Performance Targets

```text
event ingestion:
>1,000 structured events/sec

entity lookup:
<100 ms p95

basic relationship query:
<250 ms p95

context compilation:
<2 sec typical excluding expensive embedding work

task scheduling:
<100 ms

agent dispatch overhead:
<500 ms

mobile status endpoint:
<250 ms LAN target
```

---

# 77. Initial Scale Targets

Design toward:

```text
100 hosts
100 repositories
1,000 services
100 models
10 million structured events
100 million external log references
50 concurrent agent tasks
```

Do not optimize prematurely to these limits.

---

# 78. Synthetic Lab

Build a deterministic synthetic environment.

It should generate:

```text
commits
deployments
pod failures
configuration drift
model failures
GPU exhaustion
network faults
fake logs
metrics
agent failures
OmniRoute fallbacks
```

This environment becomes the evaluation harness.

---

# 79. Agent Evaluation

Known-answer cases measure:

```text
root cause correctness
evidence completeness
false claims
tool efficiency
wall-clock time
tokens consumed
verification success
parallelism
```

---

# 80. Parallel Utilization Metrics

Track:

```text
maximum concurrent tasks
average runnable tasks
average executing tasks
dependency wait
resource wait
scheduler idle time
critical path duration
total task compute time
```

Expose:

```text
Parallel Utilization Ratio
```

to detect fake parallelism.

---

# 81. Context Efficiency

Measure:

```text
tokens supplied
tokens cited
artifacts retrieved
artifacts actually used
task success
verification result
```

The goal is not maximum context.

The goal is maximum useful context.

---

# 82. Core Engineering Rules

```text
Events are immutable.

Historical state is preserved.

Models are replaceable.

OmniRoute owns provider mechanics.

NerveCenter owns inference policy intelligence.

Agents do not own canonical state.

Zoo does not own canonical project memory.

Claims require provenance.

Facts require evidence.

Tasks form DAGs.

Independent tasks execute concurrently.

Mutations are auditable.

Approval boundaries are explicit.

Cloud services are optional.

The system must remain useful without Internet connectivity.

No single model vendor becomes foundational.
```

---

# 83. Preferred Backend Stack

```text
Python 3.12+
FastAPI
Pydantic v2
SQLAlchemy
asyncpg
PostgreSQL
pgvector
NATS JetStream
OpenTelemetry
Prometheus
```

Web:

```text
TypeScript
React
WebSocket/SSE
```

Apple:

```text
Swift
SwiftUI
```

Infrastructure:

```text
Nix
containers
Kubernetes where appropriate
```

---

# 84. Engineering Standards

Require:

```text
ruff
pyright or mypy
pytest
structured logging
typed interfaces
Alembic migrations
OpenAPI
pre-commit
CI
```

Do not place business logic directly in HTTP route handlers.

---

# 85. Interfaces to Freeze First

Before significant parallel implementation, freeze v0 contracts for:

```text
Event
Entity
Relationship
Evidence
Claim
Decision
Task
TaskRun
AgentResult
Goal
ContextPackage
ModelDescriptor
ComputeNode
InferenceRoute
OmniRouteTelemetry
PlannedChange
Approval
Connector
```

These contracts unblock nearly every independent workstream.

---

# 86. Build Phases

## Phase 0 — Foundation

Deliver:

```text
monorepo
schema package
PostgreSQL
NATS
base API
event API
entity API
OpenAPI
CLI skeleton
Apple project skeleton
web skeleton
CI
```

Acceptance:

```text
system starts
event can be submitted
entity can be created
relationship can be created
CLI reads state
iPhone simulator connects to API
tests pass
```

---

# 87. Phase 1 — Observe Reality

Implement:

```text
Git
filesystem
Kubernetes
host telemetry
GPU telemetry
Nix
OmniRoute telemetry
Zoo telemetry
event timeline
temporal relationships
```

Acceptance scenario:

```text
commit
deploy
restart pod
run Zoo task
invoke model through OmniRoute
```

All appear in one correlated timeline.

---

# 88. Phase 2 — Ask Reality

Implement:

```text
entity resolution
search
vector retrieval
graph retrieval
context compiler
query interface
provenance
```

Acceptance:

```text
"What changed during the latest HelloDJ deployment?"
```

returns evidence-backed changes.

---

# 89. Phase 3 — Investigate Reality

Implement:

```text
incident workflow
native specialized agents
Zoo delegation
blackboard
DAG scheduler
verification
```

Acceptance:

A deliberately broken service triggers at least three independent concurrent investigations.

---

# 90. Phase 4 — Inference Intelligence

Implement:

```text
OmniRoute integration
route telemetry
model registry
compute registry
historical metrics
Inference Policy Engine
```

Acceptance:

NerveCenter can explain why a task was sent to a particular route.

---

# 91. Phase 5 — Goals

Implement:

```text
persistent goals
success criteria
goal decomposition
goal reconciliation
architectural decisions
progress summaries
```

Acceptance:

Goal survives process restarts and multiple agent sessions.

---

# 92. Phase 6 — Mobile Command Deck

Implement:

```text
iPhone overview
incidents
goals
agents
ask
notifications
Watch app
Watch complication
approvals
STOP AGENTS
STOP MUTATIONS
```

Acceptance:

A synthetic incident appears on phone and Watch.

A reversible planned change can be approved from the Watch.

---

# 93. Phase 7 — Controlled Action

Implement:

```text
PlannedChange
policy engine
human approval
execution
rollback metadata
post-change verification
```

Acceptance:

NerveCenter diagnoses a known fault, proposes a reversible fix, receives mobile approval, executes it, verifies recovery, and closes the incident.

---

# 94. Canonical MVP Demo

The MVP demo should intentionally break HelloDJ or an equivalent test workload.

Sequence:

```text
1. Workload healthy.

2. Commit changes required device/configuration.

3. Deployment occurs.

4. Workload fails.

5. NerveCenter records the events.

6. User asks from iPhone:

   "Why is HelloDJ video fucked?"

7. NerveCenter creates investigation DAG.

8. Parallel tasks launch:

   Git Investigator
   Kubernetes Investigator
   Log Investigator
   Host Investigator

9. Zoo/native agents execute.

10. Inference goes through OmniRoute.

11. Blackboard collects findings.

12. Correlation agent constructs timeline.

13. Verification agent validates evidence.

14. iPhone receives:

    HelloDJ video failed after deployment X.

    Commit Y changed device configuration.

    QSV disappeared from the worker.

    First matching failure:
    01:42:17

    Confidence:
    97%

15. Watch receives:

    🔴 HelloDJ
    QSV unavailable
    Fix ready.

16. Proposed reversible remediation appears.

17. User approves.

18. NerveCenter executes change.

19. Health check passes.

20. Incident resolves.

21. NerveCenter permanently records:

    cause
    fix
    evidence
    model performance
    agent performance
    timeline
```

If this demo works correctly, the architecture is proving its purpose.

---

# 95. Parallel Implementation Workstreams

After schema freeze, Zoo should create these independent workstreams.

```text
A — Core schemas

B — PostgreSQL persistence

C — Event bus / event ingestion

D — Knowledge / temporal relationships

E — Evidence / claims

F — Context compiler

G — Goal subsystem

H — Task DAG / scheduler

I — Agent API / blackboard

J — Zoo adapter

K — OmniRoute adapter

L — Inference Policy Engine

M — Compute/model registry

N — Git/filesystem connectors

O — Kubernetes/Nix/host connectors

P — MCP client/server

Q — CLI

R — Web UI

S — Apple shared SDK

T — iPhone app

U — Watch app/widgets

V — Observability

W — Synthetic lab

X — Integration/evaluation harness
```

---

# 96. Workstream Dependency Shape

```text
                      SCHEMAS
                         │
       ┌─────────────────┼─────────────────┐
       ▼                 ▼                 ▼
 Persistence         Event System        SDK/API
       │                 │                 │
       └────────┬────────┴───────┬─────────┘
                ▼                ▼
          Knowledge          Connectors
                │                │
      ┌─────────┼────────┐       │
      ▼         ▼        ▼       │
  Evidence   Context   Goals      │
      │         │        │       │
      └────┬────┴───┬────┘       │
           ▼        ▼            │
        Agents   Scheduler       │
           │        │            │
           └────┬───┘            │
                ▼                │
          Orchestrator           │
                │                │
         ┌──────┴──────┐         │
         ▼             ▼         │
       Zoo         Inference     │
                    Policy       │
                      │          │
                      ▼          │
                  OmniRoute ◄────┘

Meanwhile:

API contract
   ├── Web UI
   ├── CLI
   └── Apple SDK
         ├── iPhone
         └── Watch
```

---

# 97. Zoo Implementation Rules

Zoo MUST operate as a multi-agent engineering organization.

Before changing application code:

```text
1. inspect repository structure in parallel

2. identify architectural constraints

3. define interface/schema contracts

4. construct dependency DAG

5. assign file ownership

6. identify work that can begin immediately

7. launch dependency-independent tasks concurrently
```

During implementation:

```text
maintain shared implementation state

publish structured findings

avoid duplicated repository inspection

avoid agents independently designing shared contracts

dispatch newly-unblocked tasks immediately

use cheaper/faster models for mechanical exploration

reserve high-capability models for architecture and synthesis

verify cross-workstream integration independently
```

---

# 98. Zoo Shared Status Artifact

Maintain:

```text
docs/implementation-status.yaml
```

Conceptually:

```yaml
workstreams:

  schemas:
    owner: agent-1
    state: complete

  events:
    owner: agent-2
    state: running

  context:
    owner: agent-3
    state: blocked
    blocked_by:
      - knowledge

  apple-ios:
    owner: agent-9
    state: running

  omniroute:
    owner: agent-5
    state: running
```

Zoo should use this to avoid repeated rediscovery.

---

# 99. File Ownership

Agents working concurrently should receive expected ownership boundaries.

Example:

```text
Agent A:
schemas/**

Agent B:
backend/events/**

Agent C:
backend/knowledge/**

Agent D:
backend/inference/omniroute/**

Agent E:
apple/Shared/**
```

Shared-file edits require explicit coordination.

---

# 100. Bootstrap Prompt for Zoo

Use this instruction when starting implementation:

```text
Build NerveCenter according to the supplied specification.

Treat yourself as a multi-agent engineering organization.

The project's primary architectural components are:

- NerveCenter: persistent brain, knowledge, evidence, goals and orchestration
- Zoo: primary engineering execution workforce
- OmniRoute: primary inference fabric
- local/cloud models: replaceable compute resources
- iPhone / Apple Watch: first-class command clients

Before modifying application code:

1. Inspect independent repository areas concurrently.
2. Produce an implementation dependency DAG.
3. Freeze v0 shared schemas and interfaces.
4. Identify all immediately parallelizable work.
5. Assign clear ownership boundaries.
6. Launch dependency-independent work concurrently.
7. Maintain a machine-readable shared implementation-status artifact.
8. Dispatch newly unblocked tasks immediately.
9. Minimize critical-path wall-clock time.
10. Avoid sequential repository exploration when parallel inspection is possible.

Core architectural rules:

- events are immutable
- history is temporal
- claims carry provenance
- facts require evidence
- agents do not own canonical memory
- Zoo does not own canonical memory
- OmniRoute owns inference-provider mechanics
- NerveCenter owns inference policy and empirical intelligence
- models are replaceable
- tasks form DAGs
- independent tasks execute concurrently
- mutations are auditable
- destructive operations require explicit policy authorization

Use OmniRoute for inference unless working in explicit bootstrap/recovery mode.

Every inference call should carry NerveCenter correlation metadata where possible.

Do not create another provider-routing framework inside NerveCenter.

Do not allow several agents to independently invent incompatible shared interfaces.

Do not use expensive models for tasks that deterministic tools or smaller models can solve.

Use independent verification for architectural integration and high-risk changes.

The implementation must include tests proving actual concurrent task execution.

The first major end-to-end milestone is the deterministic incident-analysis demonstration described in the specification.

The final MVP must allow the following scenario:

A user sees an incident on their iPhone or Watch, asks NerveCenter what happened, NerveCenter launches parallel investigation through Zoo and native agents, inference travels through OmniRoute to appropriate compute, evidence is correlated into a verified explanation, a reversible remediation is proposed, the user approves it, NerveCenter executes and verifies the remediation, and the incident becomes permanent engineering knowledge.
```

---

# 101. Definition of MVP Complete

NerveCenter MVP is complete when:

```text
multiple machines are observed

Git state is captured

Kubernetes state is captured

Nix/host state is captured

GPU state is captured

OmniRoute inference traffic is captured

Zoo activity is correlated

temporal relationships are maintained

questions can be answered with evidence

facts and hypotheses are distinguished

context is automatically compiled

tasks form DAGs

independent tasks run concurrently

multiple models/routes can be used concurrently

route decisions can be explained

model effectiveness is measured empirically

agent discoveries persist

goals persist

incidents can be reconstructed

planned changes exist

reversible changes can require approval

the web UI works

the CLI works

the iPhone app works

the Watch app works

MCP access works

a deterministic fault can be diagnosed and remediated end-to-end
```

---

# 102. Future Capabilities

After MVP:

```text
automatic engineering diary

automatic postmortems

architecture drift detection

automatic documentation updates

voice-first NerveCenter

camera-assisted hardware identification

advanced mobile visualizations

predictive capacity planning

automatic benchmark campaigns

multi-user/team support

encrypted NerveCenter federation

automatic routing-policy optimization

IDE proactive context injection

project dependency intelligence

cross-project architectural reasoning

engineering habit discovery
```

---

# 103. Long-Term Recursive Capability

One of the eventual highest-value queries is:

> What do I keep doing manually that you could automate?

NerveCenter should derive the answer from:

```text
event history
repeated tool sequences
recurring incidents
repeated agent actions
repeated user approvals
repeated repository changes
repeated infrastructure workflows
```

It should identify automation candidates using observed reality rather than generic suggestions.

---

# 104. Final Product Model

The intended system can be summarized as:

```text
Zoo
=
workforce

OmniRoute
=
inference fabric

5090 / 4070 Ti SUPER / M5 Max / cloud
=
compute fabric

NerveCenter
=
memory + brain + scheduler + evidence + supervisor

iPhone
=
mobile command deck

Apple Watch
=
glance + alert + approval surface
```

NerveCenter's differentiator is not having an LLM.

Its differentiator is knowing:

```text
what exists
what happened
why it happened
what has been tried
what evidence supports it
what remains unfinished
which model should work on it
which agent should execute it
which tasks can happen simultaneously
and whether the result actually worked
```

look at existing files in this directory for available dumps on the network.
