# Phase 2 — Task 2.7: G2 Gate summary (owner-facing) — STOP, decision requested

**Status: STOPPED AT G2. Phase 3 has NOT been started and will not start without an explicit owner decision recorded below.**
**HEAD:** `7e35136` on `main`. Seven Phase 2 commits since the G1 baseline `c9ea19e`. Feature flag `NERVE_MAIL_SECURITY_ENABLED` still defaults OFF.
**Legend:** VERIFIED = observed by running a command or reading code during orchestrator review. REPORTED = taken from a task report and not independently re-run.

## Verdict in one paragraph

Phase 2 added the reliability and defensive building blocks: a leased, fenced, bounded-retry queue with dead-letter and a sweeper; an expiring threat-intel store; disabled-by-default feed adapters; static attachment inspection with a scanner interface; an SSRF-hardened egress policy client; and privacy-safe observability with health and alert evaluation. Most of these are deliberately **not connected to the live pipeline**: intel, feeds, attachments and egress are imported by nothing outside their own modules and tests. No real feed, scanner, DNS resolver or network transport has been exercised. The system still produces no production data (see "Not established").

## Gate G2 checklist (requirements.md "Gate G2")

| Criterion | Evidence | Status |
| --- | --- | --- |
| Bounded queues | attempt ceiling, deterministic backoff, DEAD_LETTER with safe reason code (2.1) | VERIFIED (tests) |
| Typed failure status | typed job states; timeout/error => INCOMPLETE/REVIEW, never clean (2.1, 2.4) | VERIFIED |
| Worker restart and replay | crash-after-claim, crash-after-verdict, double-delivery, replay, manual dead-letter replay with lineage (21 queue tests) | VERIFIED (66 passed across queue+harness+worker) |
| Redaction and authorization | full-pipeline canary test (message id, sender, subject, URL, filename, body, account) absent from all telemetry; health endpoint auth + cross-account tests (2.6) | VERIFIED (read test source, ran suite) |
| Internal rollup metrics work | depth/age/attempts/latency/fidelity/completeness/feed/egress aggregates, enum labels only (2.6) | VERIFIED (tests) |
| Existing suites green | full non-integration suite **5604 passed, 0 failed** (G1: 5162; original baseline 1850) | VERIFIED |
| Feed and sandbox failure modes reviewed | see "Failure modes reviewed" | VERIFIED by tests and orchestrator probing |

## Verification run for this document

- `uv run pytest tests/ -m "not integration" -q` -> **5604 passed**.
- `uv run pytest tests/backend -k mail_security -q` -> **3754 passed**.
- Queue + harness + worker (restart/replay/completeness) -> **66 passed**.
- `uv run pyright backend/mail_security backend/api/endpoints/mail_security.py` -> **0 errors**.
- Alembic: `016_mail_security`, `017_mail_intel` (single head, up/down tested per task).
- Orchestrator independent probes: ~35 IP address forms against `is_address_blocked` (0 leaks, public addresses not over-blocked); end-to-end `EgressClient` probes with fake resolver/transport covering mixed and private DNS answers, DNS rebinding, userinfo/backslash/percent-dot/CRLF/numeric-host tricks, bad scheme/port, redirect abuse and loops, default-deny, master-flag-off, consent missing/other-account, and path/query stripping. All denied or minimized as designed. Rebinding is denied by resolving twice per hop and requiring identical answer sets before pinning.
- Ruff: clean on changed lines per task reports (REPORTED).

## What each task delivered, and whether it is connected

| Task | Delivered | Connected to live path? |
| --- | --- | --- |
| 2.1 | queue/lease/fencing/retry/dead-letter, sweeper + explicit flag-gated runner | worker.py updated; **runner not started anywhere** (not in app startup) |
| 2.2 | expiring threat-intel store, migration 017, dry-run/rollback, scoped overrides | **No.** Match->finding function exists; not called by worker/inspections/policy |
| 2.3 | feed parsers, integrity/license gate, poisoning guards | **No.** No fetcher implementation; nothing imports it |
| 2.4 | static attachment checks, `AttachmentScanner` Protocol, null scanner | **No.** Nothing imports it; no real scanner |
| 2.5 | egress policy + client, consent gate, SSRF matrix | **No.** No real transport or resolver; nothing imports it |
| 2.6 | telemetry, observability, health endpoint, `evaluate_alerts` | Health endpoint registered (read-only, scoped); worker latency hook is a no-op unless a sink is set |

## Failure modes reviewed (task 2.7 asks for this)

**Queue / worker:** claim races are fenced by a (lease owner, attempts) compare-and-swap; a stale worker cannot overwrite a newer claim at claim time. **Gap:** the ownership check and the verdict write are separate transactions, leaving a small stale-worker window. It is bounded by deterministic assessment ids and the rule that an INCOMPLETE verdict never downgrades a complete one. The stale-worker test mostly exercises the lease-expired path, not the verdict-write path.

**Threat-feed:** integrity mismatch rejects the whole payload; oversized/malformed payloads are bounded; protected domains cannot be marked malicious; TTL cap makes a silent feed age out; a failed fetch keeps the previous live set and is never treated as safe; ingestion is versioned with rollback. **Gaps:** the change-ratio guard counts additions only, so a mass **removal** of indicators (which would silently reduce detection) is not flagged; the first load into an empty store is not ratio-guarded (documented); one clock-source mismatch (`ingest_feed` uses an injected `now`, live-set reads use the repository clock). No real feed was validated and no real feed license was checked.

**Scanner / sandbox:** only an engine that returns a clean verdict counts as "scanned clean by engine X version Y"; error, timeout, unavailable, too-large and not-run are all incomplete and never clean, and even a clean verdict is evidence not safety. **Gaps:** no scanner exists. ClamAV/YARA availability on NixOS is still UNKNOWN (decisions.md open question 10). Static checks are heuristic. There is no detonation sandbox (Phase 8 and out of scope).

**Egress:** default deny, consent-gated, minimized URLs, double resolution, pinned IP, per-hop revalidation. **Gap:** every guarantee depends on a future real transport connecting only to `pinned_ip`, ignoring proxy env, not following redirects itself, and enforcing the size cap and timeouts. That contract is documented, not exercised against real sockets.

## Not established (read before approving)

1. **Still no production data.** The read API remains deny-by-default because production tokens do not mint `account_scopes`. Unchanged since G1 and still unowned.
2. **Still nothing runs the worker or sweeper in a deployment.** Task 2.1 built the runner but it is explicit-start only. Jobs would stay PENDING.
3. **T-26 attention bypass still live** (untrusted body text mints Today items). Phase 7.
4. **Auth evidence still all UNKNOWN**; raw-RFC822 capture seam (NS-ADR-002) still not scoped.
5. **Claim atomicity and fencing proven on scratch SQLite only.** Postgres row-locking behavior is unverified.
6. **No alert delivery mechanism exists in the repo.** `evaluate_alerts` is a pure function plus Prometheus gauges; nothing notifies anyone. No alert rules file was added because the repo keeps none.
7. **Health thresholds are defaults, not tuned** on real load.
8. **Open design question for you (from 2.4):** how should a NOT_RUN attachment scan surface in policy? If it makes every message with an attachment INCOMPLETE, review load balloons; if it is merely a "check not run" note, a message can look more assessed than it is. Needs your decision before attachments are wired.
9. **Threats owned by later phases remain uncovered:** T-10/T-12/T-13 (LLM and OmniRoute routing, Phase 4), T-20/T-21/T-25 (history and training, Phases 5-6), T-22/T-23/T-24/T-27/T-30 partially.
10. **Pre-existing:** `test_schema_param_types.py` fails 2 tests when run alone (passes in the full suite); web lint toolchain absent.

## Housekeeping

The spec files, Phase 0/1/2 evidence docs, `decisions.md` (holding NS-ADR-013) and review files remain **untracked in git** (28 entries). Only code and tests are committed. I have not committed them because you have not asked. `decisions.md` has not yet been updated with Phase 2 decisions (NS-ADR candidates: no-transport-yet egress design; additions-only ratio guard as accepted limitation; pure-function alerts with no delivery).

## Recommended next step (for your decision, not a plan I am executing)

Phase 3 (Intel Arc classifier mesh) needs real hardware inventory on the GTi14 hosts (task 3.1: "No installs until inventory reviewed") and benchmarking. Much of it cannot be verified in this environment and must be reported `NOT VERIFIED ON HARDWARE` rather than simulated. Before spending effort there, the higher-value gaps are arguably the unowned **integration tasks** that make Phases 1-2 produce real data: minting `account_scopes`, running the worker/sweeper in a deployment, wiring intel and attachment findings into the assessment, a real egress transport, and the raw-RFC822 capture seam. Those are not in tasks.md as numbered tasks; they would need your direction and probably a small spec amendment.

## Owner decision (to be completed by owner)

```
G2 decision:  APPROVE Phase 3 | AMEND SPEC FIRST (integration tasks) | REQUEST CHANGES | HOLD
Reviewer / date:
Answer to open design question 8 (not-run attachment scan in policy):
Notes / conditions:
```
