# NerveCenter P4 — Personal Autonomy & Continuous Goals

**Status:** Design / contract freeze (implementation gated on P2 obligations + P3 approvals)
**Depends on:** P2-A obligation plane (durable task/evidence contracts), P3
approval + policy enforcement, real compute telemetry, privacy-scoped evidence
access
**Scope:** Bounded autonomous progress over hours/days/weeks — persistent goals,
independent background work, checkpointing, resource budgets, governed autonomy.

## Objective

NerveCenter makes bounded progress toward long-term personal goals without
requiring continuous manual supervision. P4 is **not** permission for agents to
act without limits. It adds persistent goals, independent work, checkpointing,
resource budgets, and governed autonomy with human review checkpoints.

## Mandatory invariants

1. **Goals survive restarts and model switches.** State is durable (Postgres);
   progress history, artifacts, and evidence are checkpointed every cycle.
2. **Every cycle is restart-safe, idempotent, and budgeted.** Loading a goal,
   selecting a subtask, and recording progress are all idempotent on replay.
3. **Policies are enforced at actual execution boundaries**, not only via model
   prompts (PDP `backend/policy/pdp.py`; tool allowlists; data-access policies).
4. **A goal cannot expand its own permissions. Automatic approval escalation is
   prohibited.** All externally consequential work goes through the P3
   ActionProposal + approval lifecycle.
5. **No unbounded recursion or infinite loops:** maximum autonomous steps,
   timeout/retry limits, per-goal and daily token/compute budgets, and human
   review checkpoints cap the loop.
6. **Private data is permission-scoped.** Memory retrieval is permission-scoped,
   source-linked, confidence-aware, and correctable; raw private conversations
   are not copied indiscriminately into shared agent memory.

## P4-A — Persistent Personal Goals

Extends the existing Goals/Tasks system (`backend/persistence/models/goals.py`,
`backend/goals/service.py`, `backend/tasks/`).

A goal contains: explicit user intention; desired outcome; constraints +
permissions; success criteria; work breakdown; dependencies; deadline (optional);
evidence + sources; progress history; resource/token budget; pause/cancel
state; next useful action.

Lifecycle: `proposed → active → paused | blocked | needs_approval → done |
cancelled`, with durable checkpoint state after every cycle.

## P4-B — Background Research & Work (durable goal-worker loop)

Per cycle:
1. Load an active goal.
2. Review its state + approved permissions.
3. Select one useful, bounded subtask.
4. Compile focused context from evidence + memory.
5. Route the task through OmniRoute (INFER_ROUTE PDP decision, LOCAL_ONLY
   default, telemetry recorded).
6. Dispatch parallel Zoo workers where appropriate.
7. Validate results independently.
8. Save artifacts + evidence.
9. Update progress + checkpoint.
10. Decide: continue / pause / request approval / stop.

Every cycle restart-safe, idempotent, budgeted. Avoids unbounded recursive
delegation, infinite research loops, and uncontrolled context accumulation
(context compiled per-subtask from a bounded evidence window, not whole
conversation history).

## P4-C — Compute-Aware Scheduling

Leverages NerveCenter/OmniRoute monitoring + local hardware (RTX 5090 for
demanding coding/reasoning; RTX 4070 Ti SUPER for concurrent readers; M5 Max for
planning/review; Intel Xe iGPU for lightweight inference/embeddings/preprocessing).

Uses **measured availability** (GPU utilization, VRAM headroom, model
availability, queue depth, latency), not static assumptions. Background work
yields to interactive work and critical personal workflows. Schedules optional
work inside user-defined quiet windows and per-token/compute budgets.

## P4-D — Long-Term Memory & Learning

Extends the evidence-backed memory system. Remembers: goals + progress, prior
failed attempts, successful approaches, user corrections, reusable scripts/skills,
tool capabilities, model effectiveness, known preferences/constraints, completed
actions + approvals. Integrates the existing skills registry + MCP discovery.

Memory retrieval is permission-scoped, source-linked, confidence-aware,
correctable. Raw private conversations are not copied into shared agent memory
without an explicit, narrow, audited purpose.

## P4-E — Autonomous Safety & Budget Control

Controls (enforced at execution boundaries):

- Pause All Agents / Pause Goal / Cancel Goal.
- Daily inference budget + per-goal resource budget.
- Tool allowlists + data-access policies + approval policies.
- Maximum autonomous steps + timeout/retry limits.
- External communication restrictions + human review checkpoints.

## P4-F — Personal Autonomy Interface

Goals workspace: current goal, current activity, progress, evidence gathered,
completed work, next planned action, blockers, approvals needed, time + compute
spent, stop/pause controls. The user never needs raw agent logs to understand
progress.

## P4 Definition of Done

Demonstrate a persistent personal goal that: (1) is created from a natural-language
request, (2) decomposes into bounded subtasks, (3) executes safe background
research, (4) uses existing OmniRoute + Zoo workers, (5) persists useful findings
+ artifacts, (6) pauses when higher-priority work needs resources, (7) resumes
after an application/worker restart, (8) requests approval before external
changes, (9) produces a concise actionable completion report, (10) remains fully
pausable, cancelable, and auditable.

## Source-control

Branch `feat/p4-personal-autonomy`. Implementation dependencies: goal workers on
durable task/evidence contracts (P2-A); autonomous actions on P3 approvals +
policy enforcement; background scheduling on real compute telemetry; long-term
memory on privacy-scoped evidence access. Dispatch independent research/memory/
compute-scheduling/UI work as soon as those contracts are stable.
