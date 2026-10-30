# P3 Execution Plan — Proactive Assistance

## Approach

P3 is layered on the P2 obligation plane. Design the **ActionProposal +
approval lifecycle** contract first (frozen in `docs/contracts/
action-proposal.md`), then implement the proposal engine, the communication
copilot (draft → approve → send), paperwork assistance, governed workflows,
and preference learning. No externally consequential operation is enabled
before the authorization gate passes.

## Workstreams (file ownership)

| WS | Owner files | Depends on |
| --- | --- | --- |
| **P3-A** Proposal engine | `backend/proposals/{__init__,models,service}.py`, `backend/persistence/models/proposals.py` (+ migration), `backend/api/endpoints/proposals.py` | P2-A |
| **P3-B** Communication copilot | `backend/communications/assist.py`, `backend/drafts/service.py`, `backend/email/service.py` | P3-A |
| **P3-C** Paperwork assistance | `backend/paperwork/` (new), document/Nextcloud/NAS/S3 refs | P3-A, P2-C |
| **P3-D** Governed workflows | `backend/workflows/` (thin orchestration on existing events/tasks/scheduler) | P3-A, P2 |
| **P3-E** Preference learning | `backend/preferences/` (new), reads `notification_log` + `preferences` plane | P2 |
| **P3-QA** | `tests/backend/test_proposals*.py`, `test_communication*.py`, `test_workflow*.py` | all |

## Contract-first integration

- Freeze the ActionProposal schema + lifecycle + risk categories in
  `docs/contracts/action-proposal.md` before dispatch.
- Every execution boundary calls the PDP (`backend/policy/pdp.py`) with the
  proposal's `required_permissions` and the durable `ApprovalRecord`. No model
  prompt can authorize an action.
- P3 does not import P2 in-progress code except the frozen obligation contract.

## Verification

- Unit tests per workstream (SQLite, no network, fixture OmniRoute).
- E2E: incoming request → obligation → proposal → draft → approval → bounded
  execution → verification → Today + audit. Rejection, expiration, rollback,
  failed execution, duplicate replay all tested.
- Live gates recorded UNVERIFIED (real Gmail/Messages send, real calendar
  write, real document fetch).

## Non-goals

- No payments (P2 hard rule). No auto-send. No diagnosis. No autonomous agents
  (P4).
