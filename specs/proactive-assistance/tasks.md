# P3 Task List

## P3-A — Action Proposal Engine

- [ ] Freeze ActionProposal contract (schema, lifecycle, risk categories, approval requirements, rollback) — `docs/contracts/action-proposal.md`.
- [ ] Migration + ORM `backend/persistence/models/proposals.py` (ActionProposalRecord).
- [ ] `backend/proposals/service.py`: create, approve, reject, expire, execute (via PDP + ApprovalRecord), verify, rollback (reversible-internal).
- [ ] PDP binding at the execution boundary (no model-prompt authorization).
- [ ] API `backend/api/endpoints/proposals.py` + registration.
- [ ] Tests: lifecycle, risk gating, rejection, expiration, rollback, dup replay.

## P3-B — Communication Copilot

- [ ] Draft suggested replies; prepare follow-ups; summarize conversations; extract action items; detect unanswered important requests; suggest scheduling alternatives; find prior correspondence; correspondence briefing.
- [ ] Preview / edit / approve / reject; never auto-send by default.
- [ ] Reasoning uses visible source evidence only; no unrelated-conversation leakage.
- [ ] Live gate UNVERIFIED (real Gmail/Messages send).

## P3-C — Appointment & Paperwork Assistance

- [ ] Agendas, relevant records/docs, questions, forms, follow-up checklists, summary notes, source references — all drafts.
- [ ] Nextcloud/NAS/S3/document ref resolution.

## P3-D — Governed Workflows

- [ ] Bill received / Appointment scheduled / Important unanswered message / Low Energy workflows on existing event+task+scheduler infra (no second engine).
- [ ] Each step a P3-A proposal at the consequential boundary.

## P3-E — Preference Learning

- [ ] Learn from snoozed/accepted/rejected feedback; notification windows; source importance; next-step granularity; routines.
- [ ] Dismissal ≠ unimportant; explanations, reset, editable preferences; local + purpose-limited.

## P3-QA + Evidence

- [ ] E2E test: request → obligation → proposal → draft → approval → bounded op → verify → Today + audit.
- [ ] `docs/evidence/p3-live-gates.md` UNVERIFIED matrix.
