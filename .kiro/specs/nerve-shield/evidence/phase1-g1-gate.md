# Phase 1 — Task 1.13: G1 Gate summary (owner-facing) — STOP, decision requested

**Status: STOPPED AT G1. Phase 2 has NOT been started and will not start without an explicit owner decision recorded below.**
**HEAD:** `c9ea19e` on `main`, 12 Phase 1 commits since baseline `45ab762`. Feature flag `NERVE_MAIL_SECURITY_ENABLED` defaults OFF.
**Legend:** VERIFIED = observed by running a command at the time of writing. REPORTED = taken from a task report/review and not re-run for this document.

## Verdict in one paragraph

Synthetic mail flows through the real ingestion seam, becomes a durable account-scoped job, is parsed under resource caps, inspected offline, and receives a metadata-only SHADOW verdict. That verdict is readable through a read-only, account-scoped API and a read-only UI indicator. The existing attention/Today and cursor behavior is unchanged (regression-tested). `SAFE_TO_PROCESS` is unreachable by design in Phase 1 (NS-ADR-013). Nothing here has been run against real accounts, real mail, a real model, or GPU hardware.

## Gate G1 checklist (requirements.md "Gate G1")

| Criterion | Evidence | Status |
| --- | --- | --- |
| Synthetic mail -> parser/evidence | `parser.py` (1.3); `test_mail_security_parser.py` | VERIFIED |
| -> deterministic findings | `inspections.py` (1.5), `auth_evidence.py` (1.4) | VERIFIED |
| -> persisted SHADOW verdict | `worker.py` (1.9) + tables/migration 016 (1.7); `test_full_slice_shadow_and_attention_unchanged` | VERIFIED |
| -> read-only API | `/api/mail-security` GET list + detail (1.10); 17 API tests | VERIFIED |
| No net fetch | socket-block guards + static import checks (parser, inspections, worker, harness) | VERIFIED |
| No action/mutation | GET-only routes; SHADOW => empty capabilities; zero attention change vs flag-off | VERIFIED |
| Idempotent replay | deterministic ids (1.2); replay => 1 job, 1 assessment revision | VERIFIED |
| Existing suites green | full non-integration suite **5162 passed, 0 failed** (baseline 1850) | VERIFIED |
| Reproducible commands + observed results | `evidence/phase1-security-harness.md` | VERIFIED (file exists, committed in `c9ea19e`) |
| UI indicator renders, no remote resources | Playwright spec asserts zero non-same-origin requests; `npm run build` passes | REPORTED for e2e; build re-run in 1.11 |

## Verification run for this document

- `git log --oneline 45ab762..HEAD` -> 12 Phase 1 commits (1.1 through 1.12; 1.5 has a review-fix commit).
- `uv run pytest tests/ -m "not integration" -q` -> **5162 passed**.
- `uv run pytest tests/backend -k mail_security -q` -> **3312 passed** (most are table-driven identifier cases from Task 1.2).
- `uv run pyright backend/mail_security backend/api/endpoints/mail_security.py` -> **0 errors**.
- Ruff: clean on changed lines per task reports (REPORTED). Repo-wide ruff debt (416 errors at baseline) is pre-existing and not a CI gate.

## Invariants proven in code and tests

I-01 email is data; I-02 absent/failed/incomplete is never clean (policy plus model validator); I-03 SHADOW has no provider mutation; I-06 DKIM pass is not a trust flag; I-08 model output cannot decide (stub-model test); I-11 idempotent replay. Threats covered: T-01, T-02, T-03 (partial, see below), T-04, T-05, T-06, T-07 (descriptor only), T-11, T-14, T-15, T-16, T-17, T-18, T-26 (not worsened), T-28, T-29.

## What this does NOT establish (read before approving)

1. **The system produces no data in production today.** The read API is deny-by-default because production tokens do not mint the `account_scopes` claim. The UI therefore shows "Not assessed". Minting that claim is an unowned integration task.
2. **T-26 is still live.** Untrusted body text still mints Today/attention items through `CommunicationPipeline`. Phase 1 deliberately did not touch it. The gate is Phase 7, behind an OFF flag.
3. **No real auth evidence exists.** Transports keep no auth headers, so every auth axis is UNKNOWN. `INDEPENDENT` provenance is unreachable. Fixing this needs the raw-RFC822 capture seam (NS-ADR-002), which is not scoped.
4. **Object-store encryption is absent from code** and deployment encryption is UNKNOWN (`deployments/` not reviewed). Phase 1 sidestepped this by never storing raw content in SQL.
5. **Job scheduling is not a true transactional outbox.** It is a best-effort write plus a sweeper (`scan_missing_jobs`). A crash in the window between the committed communication and the job insert leaves no job until the sweeper runs. **No sweeper is scheduled to run anywhere yet.** This is a real gap for Phase 2 task 2.1.
6. **Threat coverage is partial by design.** Not covered here: T-08 attachment scanning, T-09 SSRF/egress (Phase 2); T-10/T-12/T-13 LLM and OmniRoute routing (Phase 4); T-19 threat-feed poisoning (Phase 2); T-20/T-21/T-25 history and training (Phases 5-6); T-22, T-23, T-24, T-27, T-30 (Phases 3-8). Granite/Qwen/hybrid routes remain UNKNOWN.
7. **Typosquat recall is narrow** (small brand map, edit distance 1). The policy never reads a clean result as safe.
8. **Test-method limits:** deterministic corpora, not property-based (Hypothesis is not a repo dependency). Postgres-specific behavior is untested (tests use scratch SQLite). `eslint` is not installed in this checkout, so web lint did not run.
9. **Pre-existing test fragility:** `test_schema_param_types.py` fails 2 tests when run alone (passes in the full suite). It predates this work.

## Housekeeping risk: spec and evidence are not all under version control

`git status` shows the spec files (`requirements.md`, `design.md`, `tasks.md`, `threat-model.md`, `decisions.md`, `schemas/`), the Phase 0 evidence, and several review files as **untracked**. `decisions.md` holds NS-ADR-013 and the carried-forward items. Only the code and `phase1-security-harness.md` are committed. I have not committed these because you did not ask me to. Recommend deciding whether to commit them before Phase 2.

## Carried-forward items (not silently dropped)

- Mint the `account_scopes` token claim (blocks any real data reaching the API/UI).
- Schedule/run the sweeper and a worker invocation path (no runner exists; jobs stay PENDING in real deployments).
- Raw-RFC822 capture seam decision (NS-ADR-002) and the `message/rfc822` content-type mislabel on stored body text.
- Auth observation `evidence_refs` are empty; per-axis vs per-message untrusted finding granularity.
- Standalone PROMPT_INJECTION leaves `threat_likelihood` NOT_EVALUATED (pinned as intended in 1.12).
- Per-part MIME header caps; link-based remote resources beyond images are handled in 1.5 only.
- Object-store SSE and `deployments/` review before any raw-evidence storage (Zone P0).
- Restore web lint toolchain (out of NerveShield scope).

## Recommended Phase 2 entry conditions (for your decision, not a plan I am executing)

Phase 2 (tasks.md 2.1-2.7) is reliability and threat intel. Task 2.1 (queue/lease/dead-letter plus sweeper) directly closes gap 5 and is the sensible first slice. Tasks 2.3 and 2.5 introduce network surface (feeds, URL lookup) and are disabled-by-default by spec. Before any of that, consider scoping the `account_scopes` minting and the raw-evidence capture seam, since without them real mail never reaches the verdict path.

## Owner decision (to be completed by owner)

```
G1 decision:  APPROVE Phase 2
Reviewer / date: Owner (via orchestrator) / 2026-11-05
Notes / conditions:
  - Owner direction: iterate through each Phase 2 task in order (2.1 -> 2.6),
    verifying each, and STOP at the G2 gate (Task 2.7). Do not enter Phase 3.
  - Phase 2 network surfaces (feeds 2.3, URL lookup 2.5) stay disabled by default
    per spec; no personal mail content or personalized URLs sent to any third party.
  - Spec/evidence files remain uncommitted pending an explicit owner request.
```
