# Task 7.5 — Capability-scoped task proposals + PDP adapters results

Date: 2026-11-11. Status: done, local commit only. Library-only (no endpoint, no producer wired, no flag), so no deploy is needed.

## What changed

`backend/mail_security/task_proposals.py` defines the `mail-task-proposal@1` contract and its policy (`mail-task-proposal-policy@1`).

**Closed kinds.** Each kind has exactly one capability, one *propose* action and one resource under `nc://personal/nervecenter/<area>/proposals/<tp-id>`, which validates against the frozen `nc-grammar v1`.

| Kind | Capability | PDP action | Sensitivity |
|---|---|---|---|
| CALENDAR_EVENT | `personal.calendar.propose` | `resource.propose:calendar.events` | PERSONAL_PRIVATE |
| BILL_REMINDER | `personal.bills.propose` | `resource.propose:bills.reminders` | FINANCIAL |
| RELATIONSHIP_FOLLOWUP | `personal.relationships.propose` | `resource.propose:relationships.followups` | PERSONAL_PRIVATE |

**Minimal normalized facts.** Every model is frozen and forbids extra fields.
- **Calendar:** a template title enum (MEETING / APPOINTMENT / CALL / DEADLINE), an aware start time, 5–480 minutes, and an optional opaque counterpart ref.
- **Bill:** a due date, an optional amount in minor units (bounded) with an ISO-4217 currency (the two go together), and an opaque payee ref. There is no account number, no payment link and no pay capability.
- **Relationship:** an opaque person ref, a date, and a reason enum.

**What a proposal carries.**
- Origin `EMAIL` and the `conn:<uuid>` scope.
- The assessment id, an optional communication ref, and up to 16 opaque evidence refs.
- The disposition, which must be `SAFE_TO_PROCESS`.
- A deterministic `tp-` id over scope, assessment, kind and facts.

Capability and sensitivity are derived from the kind; the caller cannot choose them.

**No email authority (R-011.4, R-019.2).** There is no free-text field, no tool or capability list, and no "approved by" field. URLs and other non-opaque strings are rejected in every ref.

**Builder.** It refuses `NOT_SAFE` for any non-SAFE verdict (LOW_PRIORITY included) and `VERIFICATION_REQUIRED` when the verdict has open verification advice (R-013.4).

**PDP adapter (R-011.2).** `authorize_proposal` works in two stages:
1. It admits only `human` principals. Agent, workflow, function, connector, device and api_client principals are refused before the PDP.
2. It then calls the existing `PolicyDecisionPoint.evaluate` unchanged, passing:
   - the proposal's action and resource;
   - a scalar context: origin, kind, capability, sensitivity, disposition, evidence count, policy version;
   - and, when given, an approval bound to the exact payload.

**Default policy bundle.**
- ALLOW `kind:human,namespace:personal` on `resource.propose:*` / `nc://personal/nervecenter/*`, when the origin is EMAIL and the disposition is SAFE.
- An explicit DENY (priority 100) for email-origin proposals that are not SAFE.

**Proposing is not executing.** Execution actions (`resource.write:*`, `resource.pay:*`, `resource.send:*`) are not covered by the propose policy and stay default-deny. The confirmation workflow is task 7.6.

## Verification

| Check | Result |
|---|---|
| `tests/backend/test_mail_security_task_proposals.py` (new) | 48 passed |
| Full suite `uv run pytest tests/ -m "not integration" -q` | 8388 passed, 1 failed (known date-dependent `test_routines`), 3 skipped |
| `ruff` | clean |

**Payload tests:**
- Each kind maps to one capability, action and valid URI.
- The capability and sensitivity cannot be overridden, including attempts with `personal.payments.execute` and `personal.sending`.
- Extra fields (tool, capabilities, approved_by, instructions, free-text title) are rejected.
- Facts reject injection text, URLs, a location field, naive datetimes, out-of-bounds values, account-number-like payees, an amount without a currency, lowercase currency, a payment link, and unknown reasons.
- Facts must match the kind.
- Every plain-string field rejects the injection string.
- Evidence refs are bounded and must be opaque.
- The id is deterministic and changes with the facts.

**Gating tests:**
- REVIEW, RESTRICTED, INCOMPLETE, LOW_PRIORITY and UNKNOWN verdicts produce no proposal.
- Payment verification advice blocks a bill proposal.

**Policy tests:**
- The default bundle allows a human principal for all three kinds.
- An empty policy set gives `default_deny`.
- All six machine kinds are refused before the PDP, even with an allow-all policy for that kind.
- A cross-namespace principal gets `namespace_mismatch`.
- An explicit DENY wins over the ALLOW.
- An unsafe context hits the explicit DENY even if the adapter is bypassed.
- The context is scalar-only and carries the required dimensions.
- Execution actions stay default-deny.

**Approval tests:**
- An exact approval elevates default deny for that payload only. A different proposal, an expired approval or a pending approval are all denied.
- An approval never overrides an explicit DENY.

**Import guard:** no network, subprocess, calendar, bills, agents or orchestration import.

## Not verified / limits

- **No producer yet.** Nothing extracts calendar, bill or relationship facts from mail into proposals; the facts' source (deterministic detectors) is future work. The contract and policy are ready for 7.6 to consume.
- **Policies are not stored.** The default bundle is code-defined and not persisted to `nc_policies`. Wiring it into a policy store is part of activating proposals.
- **The PDP itself is unchanged and was not re-audited here.** Its existing tests cover default deny, precedence, namespace isolation and approval binding.
