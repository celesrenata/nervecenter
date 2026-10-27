# Personal Operations Recovery — Work Checklist

- [x] T-0.1 — Freeze the shared contract in [`spec.md`](spec.md:1), execution and ownership in [`plan.md`](plan.md:1), and this checklist. **Owner:** architect. **Gates:** all acceptance rows.

## Wave 1 — Foundation

- [ ] T-1.1 — Create migration 006, shared personal models/repositories, registry seam, wiring injection, and restart persistence test. **Owner:** F. **Gates:** 2, 9.
- [ ] T-1.2 — Implement connection records, Fernet vault, secret references, status lifecycle, diagnostics, permissions, and relay control-plane routes. **Owner:** B. **Gates:** 1, 2, 13.
- [ ] T-1.3 — Implement supervised Gmail/IMAP imports and incremental workers with durable cursors/jobs, deduplication, and communication ingestion. **Owner:** C. **Gates:** 1, 3, 4.
- [ ] T-1.4 — Inspect ownership, run backend suite plus web tests/build, and prove Wave-1 coverage. **Owner:** parent. **Gates:** 1–4, 9, 13.

## Wave 2 — User entry points

- [ ] T-2.1 — Build Connection Center with separate auth/operational state, all five states, diagnostics, and `/connections` navigation. **Owner:** A. **Gates:** 1, 5, 13, 15.
- [ ] T-2.2 — Add Relay install/uninstall, plist, pairing, credential storage, FDA guidance, consented import, durable outbox, and signed release flow. **Owner:** D. **Gates:** 5, 6.
- [ ] T-2.3 — Implement calendar accounts/providers, event persistence/sync, event/conflict APIs, and Today input. **Owner:** E. **Gates:** 7.
- [ ] T-2.4 — Make Today the `/` default and implement sections plus persistent low-energy UI/API. **Owner:** L. **Gates:** 7, 11.
- [ ] T-2.5 — Verify disjoint patches, full suites/build, and Wave-2 acceptance coverage. **Owner:** parent. **Gates:** 5, 7, 15.

## Wave 3 — Personal domains

- [x] T-3.1 — Implement bills/evidence/confidence, explicit confirm-paid transition, calendar/upcoming APIs, UI, and escalation source. **Owner:** G. **Gates:** 8.
- [x] T-3.2 — Implement routines, deterministic wake fallback, check-ins/completion, gentle missed-routine behavior, and UI. **Owner:** H. **Gates:** 11.
- [x] T-3.3 — Implement private wellness entries, trends, explicit export/deletion, and no external sharing/model path. **Owner:** I. **Gates:** 12.
- [x] T-3.4 — Implement durable relationship views, reflective reminders, commitments, notes, boundaries, VIP, and unanswered/social APIs. **Owner:** J. **Gates:** 9.
- [ ] T-3.5 — Verify disjoint patches, full suites/build, and Wave-3 coverage. **Owner:** parent. **Gates:** 8, 9, 12.

## Wave 4 — Prioritization and validation

- [x] T-4.1 — Implement Attention v2 aggregate/evidence/action/dedup/transitions and notification-log escalation limits. **Owner:** K. **Gates:** 4, 8, 10, 11.
- [x] T-4.2 — Add iOS/Watch cached stale-aware priorities, actions, reminders, approvals, preferences, and widgets while preserving Event integrity. **Owner:** M. **Gates:** 10, 11.
- [x] T-4.3 — Add e2e/privacy tests and review, extend restart test, eliminate demo-record dependency, and update MCP count test with any N-only tool addition. **Owner:** N. **Gates:** 12, 14. *(Note: MCP count test needed no update — no N-only tools were added; the 29-tool count in `tests/mcp/test_mcp_tools.py` is unchanged.)*
- [x] T-4.4 — Verify disjoint patches, full suites/build, and Wave-4 coverage. **Owner:** parent. **Gates:** 4, 8, 10–14.

## Integration wave

- [x] T-I.1 — Inspect and apply approved patches in wave order; reject frozen-path and ownership violations. **Owner:** parent. **Gates:** all rows. *(Waves 1–3 landed in `134936a`, wave 4 in `a99aa29`; each wave commit records disjoint-patch verification and its gate matrix.)*
- [ ] T-I.2 — Run `uv run pytest -m "not integration"`, `cd web && npm test`, `npm run build`, and restart persistence coverage. **Owner:** parent. **Gates:** all rows. *(Executed in this session: backend suite 1313 passed, `npm run build` green, restart-persistence tests green. Left unticked because `npm test` has no driver in `web/` (scripts are dev/build/preview/lint/e2e only); web coverage is Playwright e2e 6/6 plus the `tsc` build, not a unit-test run.)*
- [x] T-I.3 — Execute and evidence the acceptance sweep 1–15, migration ordering, privacy review, and no-demo flow before commit. **Owner:** parent. **Gates:** all rows. *(Sweep: `acceptance.md` §"P0-09 acceptance sweep" — 15/15 evidenced, with five live-only gaps recorded as Unverified in `privacy-review.md` §(h); migration ordering: `tests/backend/test_restart_persistence.py::TestSchema::test_migration_head_is_linear_after_005`; privacy: `privacy-review.md` (audit index row added); no-demo: e2e PF-2 exercises the degraded create path with no demo-record dependency.)*
