# NerveCenter P3 — Proactive Assistance Engine

**Status:** Design / contract freeze (implementation gated on P2 obligations contract)
**Depends on:** P2-A obligation identity, evidence, status, and authorization gates
**Scope:** From "I noticed something" to "I've prepared a solution and can help you finish it."

## Objective

Move from passive surfacing to governed action preparation. NerveCenter drafts,
prepares, and — only with explicit, specific authorization — executes narrowly
scoped operations. Everything generated is a **draft** until approved. Nothing
externally consequential runs without an approval binding; nothing sensitive runs
automatically by default.

## Mandatory invariants

1. **No autonomous sending of personal messages** by default. Draft → preview →
   approve → send. The user can always edit or reject.
2. **Externally consequential actions** (send email, modify calendar, share
   information, change an account) require explicit, specific authorization.
3. **Sensitive / irreversible actions** (payments, destructive deletes, medical
   communications) are **not** executed automatically by default — they require
   explicit per-action approval and, for payments, are out of scope for P3
   entirely (P2 hard rule: no money movement).
4. **Rejection, expiration, and failed execution are first-class** and leave a
   durable audit record.
5. **Explanation with evidence:** every proposal shows its source evidence; it
   does not leak unrelated conversations.

## Risk categories (frozen in `docs/contracts/action-proposal.md`)

| Category | Examples | Approval |
| --- | --- | --- |
| **Read-only** | retrieval, research, summarization, classification | none (LOCAL_ONLY route) |
| **Reversible internal** | create draft, organize internal task, provisional reminder | lightweight (reversible) |
| **Externally consequential** | send email, modify calendar, share info, change account | explicit, specific approval + PDP check |
| **Sensitive / irreversible** | payment, destructive delete, medical communication | per-action explicit; default NOT auto |

## P3-A — Action Proposal Engine

`ActionProposal` record (durable):

- `why` — why the action is useful.
- `evidence` — supporting source references (obligation → evidence).
- `affected_resources` — what it touches.
- `intended_result`, `required_permissions`, `risk_category`, `reversibility`.
- `approval_requirements` (who / how / expiry).
- `estimated_effort`, `estimated_cost`.
- `verification_plan`, `audit_record`.
- Lifecycle: `proposed → approved → executing → verified | failed | rejected |
  expired`, with rollback semantics for reversible-internal.

Reuses the existing durable approval model
(`backend/persistence/models/auth.py: ApprovalRecord`) and the PDP
(`backend/policy/pdp.py`) at the actual execution boundary. An action cannot
expand its own permissions; no automatic approval escalation.

## P3-B — Communication Copilot

Uses live Gmail/IMAP/Messages integrations (already present in P1).

- Draft suggested replies; prepare follow-ups; summarize long conversations;
  extract action items; detect unanswered important requests; suggest scheduling
  alternatives; find prior correspondence; produce correspondence briefings.
- Preview / edit / approve / reject. Never auto-send by default.
- Reasoning uses visible source evidence only.

## P3-C — Appointment & Paperwork Assistance

Proactively prepare appointment agendas, relevant records/documents, questions to
ask, forms needing attention, follow-up checklists, summary notes, source
references. All drafts until approved. Uses existing Nextcloud/NAS/S3/document
integrations.

## P3-D — Personal Workflow Automation (governed)

Reuses the existing event/task/scheduler infrastructure — no second workflow
engine. Governed workflows:

- **Bill received:** detect → extract → verify → create obligation → schedule
  reminders → wait for payment evidence → request confirmation.
- **Appointment scheduled:** detect → check conflicts → identify prep → produce
  briefing → remind → request follow-up completion.
- **Important unanswered message:** detect → determine obligation → suggest
  reply → draft → request approval → record outcome.
- **Overwhelmed / Low Energy:** choose one next action → reduce decisions → help
  complete → stop prompting.

## P3-E — Preference Learning

Learns from explicit feedback: snoozed vs completed reminders, rejected vs
accepted suggestions, preferred notification windows, important sources, helpful
next-step granularity, configured routines.

- Repeated dismissal does **not** mean unimportant.
- Provides explanations, reset controls, editable preferences.
- Sensitive learning is local and purpose-limited.

## P3 Definition of Done

Demonstrate an incoming request that: (1) becomes an actionable obligation, (2)
triggers a safe action proposal, (3) produces a useful draft, (4) requests
approval, (5) performs only the specifically authorized operation, (6) verifies
the outcome, (7) updates Today and the audit history. Also test rejection,
expiration, rollback, failed execution, and duplicate-event replay.

## Source-control

Branch `feat/p3-proactive-assistance`. No externally consequential operation is
enabled before authorization gates pass. Design proceeds read-only during P2;
implementation dispatches after P2's obligation/approval contracts are stable.
