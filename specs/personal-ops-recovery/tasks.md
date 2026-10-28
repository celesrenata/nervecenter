# Personal Operations Recovery — Work Checklist

- [x] T-0.1 — Freeze the shared contract in [`spec.md`](spec.md:1), execution and ownership in [`plan.md`](plan.md:1), and this checklist. **Owner:** architect. **Gates:** all acceptance rows.

## Wave 1 — Foundation

- [x] T-1.1 — Create migration 006, shared personal models/repositories, registry seam, wiring injection, and restart persistence test. **Owner:** F. **Gates:** 2, 9. *(Landed in `134936a`; durable seam extended in this session — `tests/backend/test_commitment_restart.py`, `backend/communications/detection/wiring.py`.)*
- [x] T-1.2 — Implement connection records, Fernet vault, secret references, status lifecycle, diagnostics, permissions, and relay control-plane routes. **Owner:** B. **Gates:** 1, 2, 13. *(Landed in `134936a`; mail OAuth rotation added this session — `backend/connectors/mail/oauth_refresh.py`, `tests/backend/test_mail_oauth_refresh.py`.)*
- [x] T-1.3 — Implement supervised Gmail/IMAP imports and incremental workers with durable cursors/jobs, deduplication, and communication ingestion. **Owner:** C. **Gates:** 1, 3, 4. *(Landed in `134936a`; supervisor reconcile loop added this session — `backend/ingest/supervisor.py`, `tests/backend/test_supervisor_reconcile.py`.)*
- [x] T-1.4 — Inspect ownership, run backend suite plus web tests/build, and prove Wave-1 coverage. **Owner:** parent. **Gates:** 1–4, 9, 13. *(Re-verified this session: backend 1375 passed, web build green, Playwright 69 passed.)*

## Wave 2 — User entry points

- [x] T-2.1 — Build Connection Center with separate auth/operational state, all five states, diagnostics, and `/connections` navigation. **Owner:** A. **Gates:** 1, 5, 13, 15. *(Landed in `5365e09`; connection health exposed in this session's `GET /api/health` — `backend/api/health.py`.)*
- [x] T-2.2 — Add Relay install/uninstall, plist, pairing, credential storage, FDA guidance, consented import, durable outbox, and signed release flow. **Owner:** D. **Gates:** 5, 6. *(Landed in `9350f35`; Mac relay install NOT yet verified on a physical device — blocker recorded in evidence file.)*
- [x] T-2.3 — Implement calendar accounts/providers, event persistence/sync, event/conflict APIs, and Today input. **Owner:** E. **Gates:** 7. *(Landed in `5365e09`; `tests/backend/test_today.py` fixture pinned this session.)*
- [x] T-2.4 — Make Today the `/` default and implement sections plus persistent low-energy UI/API. **Owner:** L. **Gates:** 7, 11. *(Landed in `c66f4c8`; e2e specs updated this session for the `/` → Today root.)*
- [x] T-2.5 — Verify disjoint patches, full suites/build, and Wave-2 acceptance coverage. **Owner:** parent. **Gates:** 5, 7, 15. *(Re-verified this session: backend 1375, web build, Playwright 69.)*

## Wave 3 — Personal domains

- [x] T-3.1 — Implement bills/evidence/confidence, explicit confirm-paid transition, calendar/upcoming APIs, UI, and escalation source. **Owner:** G. **Gates:** 8.
- [x] T-3.2 — Implement routines, deterministic wake fallback, check-ins/completion, gentle missed-routine behavior, and UI. **Owner:** H. **Gates:** 11.
- [x] T-3.3 — Implement private wellness entries, trends, explicit export/deletion, and no external sharing/model path. **Owner:** I. **Gates:** 12.
- [x] T-3.4 — Implement durable relationship views, reflective reminders, commitments, notes, boundaries, VIP, and unanswered/social APIs. **Owner:** J. **Gates:** 9.
- [x] T-3.5 — Verify disjoint patches, full suites/build, and Wave-3 coverage. **Owner:** parent. **Gates:** 8, 9, 12. *(Re-verified this session: 4 disjoint scope patches integrated cleanly (disjoint file ownership, `git apply --check` clean), backend 1375 passed / 0 failed, web build green, Playwright 69 passed.)*

## Wave 4 — Prioritization and validation

- [x] T-4.1 — Implement Attention v2 aggregate/evidence/action/dedup/transitions and notification-log escalation limits. **Owner:** K. **Gates:** 4, 8, 10, 11.
- [x] T-4.2 — Add iOS/Watch cached stale-aware priorities, actions, reminders, approvals, preferences, and widgets while preserving Event integrity. **Owner:** M. **Gates:** 10, 11.
- [x] T-4.3 — Add e2e/privacy tests and review, extend restart test, eliminate demo-record dependency, and update MCP count test with any N-only tool addition. **Owner:** N. **Gates:** 12, 14. *(Note: MCP count test needed no update — no N-only tools were added; the 29-tool count in `tests/mcp/test_mcp_tools.py` is unchanged.)*
- [x] T-4.4 — Verify disjoint patches, full suites/build, and Wave-4 coverage. **Owner:** parent. **Gates:** 4, 8, 10–14.

## Integration wave

- [x] T-I.1 — Inspect and apply approved patches in wave order; reject frozen-path and ownership violations. **Owner:** parent. **Gates:** all rows. *(Waves 1–3 landed in `134936a`, wave 4 in `a99aa29`; each wave commit records disjoint-patch verification and its gate matrix.)*
- [x] T-I.2 — Run `uv run pytest -m "not integration"`, `cd web && npm test`, `npm run build`, and restart persistence coverage. **Owner:** parent. **Gates:** all rows. *(Re-executed this session on the integrated tree: `uv run pytest -m "not integration"` → 1375 passed / 0 failed; `cd web && npm run build` → `tsc` + `vite` green; `cd web && npm run e2e` (Playwright) → 69 passed; restart-persistence tests green. `npm test` has no script in `web/` (dev/build/preview/lint/e2e only); the web driver is the Playwright e2e suite.)*
- [x] T-I.3 — Execute and evidence the acceptance sweep 1–15, migration ordering, privacy review, and no-demo flow before commit. **Owner:** parent. **Gates:** all rows. *(Sweep: `acceptance.md` §"P0-09 acceptance sweep" — 15/15 evidenced, with five live-only gaps recorded as Unverified in `privacy-review.md` §(h); migration ordering: `tests/backend/test_restart_persistence.py::TestSchema::test_migration_head_is_linear_after_005`; privacy: `privacy-review.md` (audit index row added); no-demo: e2e PF-2 exercises the degraded create path with no demo-record dependency.)*
