# P4 Execution Plan — Personal Autonomy

## Approach

P4 layers a durable goal-worker loop on the P2 obligation plane and the P3
proposal/approval boundary. Design the **goal contract + budget/safety
policy** first (frozen in `docs/contracts/goal-autonomy.md`), then implement
persistent goals, the background worker, compute-aware scheduling, long-term
memory, safety/budget enforcement, and the goals UI. Implementation dispatches
only after the P2 durable task/evidence contracts and P3 approval enforcement
are stable.

## Workstreams (file ownership)

| WS | Owner files | Depends on |
| --- | --- | --- |
| **P4-A** Persistent goals | `backend/goals/service.py` (extend), `backend/persistence/models/goals.py` (extend + migration), `backend/api/endpoints/goals.py` | P2-A |
| **P4-B** Background worker | `backend/autonomy/{__init__,worker,loop}.py` (new), checkpoint state | P4-A, P3-A |
| **P4-C** Compute-aware scheduling | `backend/scheduler/` (extend utilization), OmniRoute telemetry | P4-B |
| **P4-D** Long-term memory | `backend/memory/` (new), skills registry + MCP discovery | P2-A |
| **P4-E** Safety/budget control | `backend/autonomy/budget.py`, policy enforcement at execution boundary | P3-A |
| **P4-F** Goals workspace UI | `web/src/views/GoalsView.tsx` (or Apple), reads goal API | P4-A..E |
| **P4-QA** | `tests/backend/test_goals_autonomy*.py` | all |

## Contract-first integration

- Freeze goal lifecycle + checkpoint state + budget fields + safety controls in
  `docs/contracts/goal-autonomy.md` before dispatch.
- Every externally consequential step inside a goal cycle goes through the P3
  ActionProposal + PDP boundary. The worker cannot bypass it.
- Background scheduling reads *measured* compute availability, not static
  assumptions; it yields to interactive work.

## Verification

- Unit tests: goal lifecycle, idempotent replay, budget caps, pause/cancel.
- E2E: natural-language goal → subtasks → safe background research → artifact
  persistence → pause on contention → resume after restart → approval before
  external change → completion report.
- Live gates UNVERIFIED (real GPU fleet, real OmniRoute routes, real device
  delivery).

## Non-goals

- No unbounded recursion/loops. No self-expansion of permissions. No automatic
  approval escalation. No payments. No diagnosis.
