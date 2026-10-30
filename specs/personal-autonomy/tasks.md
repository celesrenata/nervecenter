# P4 Task List

## P4-A — Persistent Personal Goals

- [ ] Freeze goal contract (lifecycle, checkpoint state, budget fields, safety controls) — `docs/contracts/goal-autonomy.md`.
- [ ] Extend `backend/persistence/models/goals.py` + migration: constraints, permissions, success criteria, work breakdown, dependencies, budget, pause/cancel, next action, progress history.
- [ ] `backend/goals/service.py`: decompose, checkpoint, resume, pause/cancel, next-useful-action.
- [ ] Goals API: create from natural-language request, progress, report.

## P4-B — Background Research & Work

- [ ] `backend/autonomy/worker.py` + `loop.py`: durable goal-worker cycle (load → select subtask → compile bounded context → route via OmniRoute → dispatch Zoo workers → validate → save artifacts → update progress → decide).
- [ ] Restart-safe, idempotent, budgeted every cycle.
- [ ] No unbounded recursion / infinite loops / uncontrolled context accumulation.

## P4-C — Compute-Aware Scheduling

- [ ] Extend `backend/scheduler/utilization.py` + OmniRoute telemetry: measured GPU util, VRAM headroom, model availability, queue depth, latency.
- [ ] Schedule background work in quiet windows + token/compute budgets; yield to interactive + critical personal work.

## P4-D — Long-Term Memory & Learning

- [ ] `backend/memory/`: permission-scoped, source-linked, confidence-aware, correctable retrieval.
- [ ] Integrate skills registry + MCP discovery; do not copy raw private conversations indiscriminately.

## P4-E — Safety & Budget Control

- [ ] `backend/autonomy/budget.py` + enforcement at execution boundary: pause-all/goal, daily + per-goal budgets, tool allowlists, data-access policies, approval policies, max autonomous steps, timeout/retry limits, external-comms restrictions, human review checkpoints.

## P4-F — Personal Autonomy Interface

- [ ] Goals workspace: current goal, activity, progress, evidence, completed work, next action, blockers, approvals needed, time/compute spent, stop/pause controls.

## P4-QA + Evidence

- [ ] E2E: NL goal → subtasks → safe bg research → artifacts → pause on contention → resume after restart → approval before external change → completion report; pausable/cancelable/auditable.
- [ ] `docs/evidence/p4-live-gates.md` UNVERIFIED matrix.
