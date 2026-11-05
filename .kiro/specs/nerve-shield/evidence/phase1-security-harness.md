# Phase 1 — Security & Regression Harness (Task 1.12) evidence

**Date:** 2026-11-05
**Parent commit (HEAD before this task):** `e3cf25a` (Task 1.11, on `main`)
**Requirements:** R-001, R-021, R-022, R-025
**Task done-criterion:** "results with exact commands committed as evidence."

This file is the committed evidence for Task 1.12. It records the exact
verification commands, their observed results, and a table mapping every
harness test to the threat/invariant/requirement it proves. Task 1.12 adds a
**consolidated adversarial + regression harness** across the whole Phase-1
slice (Tasks 1.1–1.11). It adds **no new production behavior** — it is tests +
evidence only. No contract, policy semantics, schema, migration, or wiring was
changed.

## Files added (tests only)

| File | Role |
| --- | --- |
| `tests/backend/test_mail_security_harness.py` | The consolidated adversarial + regression suite (32 tests). |
| `tests/backend/_mail_security_harness_helpers.py` | Test-only shared synthetic corpora + durable harness factory (reuses the existing per-task harness patterns verbatim). |
| `.kiro/specs/nerve-shield/evidence/phase1-security-harness.md` | This evidence file. |

No files under `backend/` were modified. Schema/migration impact: **none**.

## Property-based testing note (R-021/R-022)

Hypothesis is **not** a repository dependency (re-checked at Task 1.12 — still
absent; `uv run python -c "import hypothesis"` → `ModuleNotFoundError`). Per the
owner-approved scope, no dependency was added. The "property-based malformed
MIME" class is therefore realized as a **deterministic, parametrized
adversarial corpus** that is exhaustive over the parser's typed
`IncompleteReason` failure modes (empty, oversized source, header count/size,
deep nesting, archive-not-inspected, truncated multipart, binary garbage). Each
case asserts the parser terminates within caps, never raises, and emits a typed
`INSPECTION_INCOMPLETE` finding — never a clean result.

## Exact verification commands and observed results (R-025)

### 1. New harness module — all tests pass

```
$ uv run pytest tests/backend/test_mail_security_harness.py -q
32 passed, 2 warnings in 0.93s
```

(The 2 warnings are pre-existing, project-wide: a SQLAlchemy `metadata`
attribute name reservation in `backend/persistence/models/evidence.py` and a
Starlette `TestClient`/httpx deprecation — neither is introduced by this task.)

### 2. Full non-integration Python suite — still green, count increased by 32

```
$ uv run pytest tests/ -m "not integration" -q
5162 passed, 3 warnings in 49.32s
```

Baseline before this task was **5130 passed / 0 failed**; the 32 new harness
tests bring the total to **5162 passed / 0 failed**. All prior mail-security
suites stay green (ran together as a cross-check:
`test_mail_security_harness/worker/ingestion_seam/api/repo/policy` →
`102 passed`), as do the cursor/attention ingestion regressions exercised by
the end-to-end harness tests (the legacy-attention-parity test asserts the
durable `attention_items` mint is byte-identical flag-on vs flag-off). The
pre-existing `tests/backend/test_schema_param_types.py` run-alone isolation
fragility is unrelated to this task and passes in the full suite.

### 3. Lint — clean on the changed files

```
$ uv run ruff check tests/backend/test_mail_security_harness.py tests/backend/_mail_security_harness_helpers.py
All checks passed!
```

(The repository-wide `uv run ruff check backend/mail_security tests/backend`
reports ~201 **pre-existing** findings in other, unrelated test files; the two
files added by this task are clean. `backend/mail_security` is unchanged.)

### 4. Type check — no errors

```
$ uv run pyright backend/mail_security
0 errors, 0 warnings, 0 informations
```

## Coverage map — each harness test → T-id / I-id / R-id

| # | Test (class → method) | Proves | Threat / invariant / req |
| --- | --- | --- | --- |
| a1 | `TestMalformedMimeTerminates::test_corpus_terminates_typed_never_crashes` (8 params) | Malformed/oversized MIME terminates within caps, never crashes, emits typed `INSPECTION_INCOMPLETE` | T-06, T-07, R-003, I-02 |
| a2 | `TestMalformedMimeTerminates::test_oversized_and_malformed_feed_policy_incomplete_not_clean` | Incomplete parse → policy never `SAFE_TO_PROCESS` | T-18, I-02, R-003 |
| b1 | `TestForgedAuthNeverVerified::test_forged_results_header_is_untrusted_never_independent` | Forged `Authentication-Results` never INDEPENDENT | T-01, R-004 |
| b2 | `TestForgedAuthNeverVerified::test_even_trusted_boundary_is_at_most_provider_reported` | Header-derived PASS tops out at PROVIDER_REPORTED | T-01, R-004 |
| b3 | `TestForgedAuthNeverVerified::test_legacy_body_only_is_unknown` | Body-only legacy mail → every axis UNKNOWN | T-28, I-02, R-004 |
| b4 | `TestForgedAuthNeverVerified::test_forged_auth_through_worker_does_not_become_clean` | End-to-end forged-auth message never clean | T-01, I-02 |
| c1 | `TestCompromisedSignedSender::test_provider_pass_plus_payment_ask_is_reviewed_never_safe` | Compromised signed sender + payment/credential ask → REVIEW/RESTRICTED, never auto-trusted; claim ignored | T-02, T-04, R-011 |
| d1 | `TestContentAbsentNeverClean::test_policy_incomplete_is_never_clean` | Incomplete policy input → never clean; axis NOT_EVALUATED | I-02, T-18 |
| d2 | `TestContentAbsentNeverClean::test_worker_resolution_failure_is_incomplete` | Content-absent run → ERROR_RETRYABLE, never clean | T-18, I-02 |
| d3 | `TestContentAbsentNeverClean::test_worker_simulated_timeout_is_incomplete` | Scan timeout → INCOMPLETE + `SCAN_TIMEOUT`, never clean | T-18, I-02 |
| d4 | `TestContentAbsentNeverClean::test_worker_dead_letter_still_never_clean` | Retries exhausted → DEAD_LETTER, still INCOMPLETE | T-18, I-02 |
| e1 | `TestIdempotentReplay::test_replay_yields_one_job_and_one_assessment` | Outbox replay → one job, one assessment revision, no dup findings; verdict before ack | R-018, T-16, T-17 |
| f1 | `TestCrossAccountIsolation::test_repo_denies_cross_account_even_with_exact_id` | Repo: account A cannot read B with B's exact id/hash | R-023, T-15 |
| f2 | `TestCrossAccountIsolation::test_api_denies_cross_account_with_404` | API: cross-account 404 indistinguishable from absent | R-023, T-15 |
| g1 | `TestCanaryNeverLeaksPipeline::test_canary_absent_from_db_and_api_and_logs` | **Headline**: canary absent from every persisted row, API response, API error, and log line pipeline-wide | T-14, R-022.4 |
| h1 | `TestNoNetwork::test_socket_block_bites_and_offline_pipeline_runs` | Socket-block proof: full parse→auth→inspections over adversarial HTML with zero socket attempts | T-05, R-005.2 |
| h2 | `TestNoNetwork::test_offline_modules_import_no_network_libraries` | Static scan: parser/inspections/auth/worker import no socket/http/url/dns/model client | closure-rule, R-005.2 |
| i1 | `TestLegacyAttentionParity::test_attention_identical_flag_on_vs_off_and_no_provider_write` | SHADOW: zero provider writes; durable attention mint byte-identical flag-on vs flag-off | T-29, T-26, R-001.4 |
| j1 | `TestBrokenModelInvariant::test_model_says_benign_but_deterministic_evidence_still_reviewed` | A wrong 'SAFE' model estimate cannot override high-impact deterministic evidence | R-022.3 |
| j2 | `TestBrokenModelInvariant::test_model_benign_cannot_manufacture_safe_on_clean_mail` | Benign model number cannot reach SAFE_TO_PROCESS without INDEPENDENT auth | R-007.2 |
| k1 | `TestRestrictedPinnedExactly::test_deceptive_link_plus_high_impact_ask_is_exactly_restricted` | **Carried #1**: deceptive-link + ask pinned exactly RESTRICTED | T-02/T-04, R-011 |
| k2 | `TestRestrictedPinnedExactly::test_injection_plus_deception_is_exactly_restricted` | **Carried #1**: injection + deception pinned exactly RESTRICTED | T-10/T-11, R-022 |
| k3 | `TestRestrictedPinnedExactly::test_credential_ask_alone_is_exactly_review_not_restricted` | Contrast pin: ask alone is REVIEW (ties RESTRICTED to the combination) | R-011 |
| k4 | `TestPromptInjectionAsymmetry::test_standalone_prompt_injection_is_review_with_safe_axis` | **Carried #2**: standalone PROMPT_INJECTION → REVIEW (safe), threat axis NOT_EVALUATED (documented) | T-10, R-022.3, NS-ADR-013 |
| k5 | `TestPromptInjectionAsymmetry::test_prompt_injection_signal_preserved_through_inspections` | The injection signal is surfaced upstream (not dropped) | T-10/T-11, R-022 |

## Carried-forward test-pinning items (both closed; no behavior change)

1. **RESTRICTED pinned exactly (Task 1.6 review).** Previously RESTRICTED
   outcomes were asserted only as membership in `{REVIEW, RESTRICTED}`. The
   harness now pins both RESTRICTED branches **exactly** so a regression from
   RESTRICTED to the weaker REVIEW fails: `deceptive-link-plus-ask`
   (policy Rule 1) and `injection-plus-deception` (policy Rule 2), plus a
   contrast test proving a credential ask alone is exactly REVIEW. Tests k1–k3.

2. **PROMPT_INJECTION disposition/axis asymmetry (NS-ADR-013 follow-up).** A
   standalone `PROMPT_INJECTION` finding escalates disposition to REVIEW but
   leaves `threat_likelihood` at `NOT_EVALUATED` (PROMPT_INJECTION is excluded
   from `_DECEPTION_CATEGORIES`). **Resolution: ASSERT + DOCUMENT the current
   intentional asymmetry** as the expected contract (the owner default). The
   disposition is safe (REVIEW, injection reason code present,
   `human_approval_required=True`, never SAFE_TO_PROCESS). Surfacing the signal
   on the threat axis would be a policy-semantics change, which a tests-only
   task must not make silently. Per the owner reminder "don't forget about
   prompt injection", the behavior is locked in as a regression test (k4) and a
   second test (k5) proves the injection signal is surfaced by the offline
   inspections rather than dropped — so any future change to the asymmetry is a
   conscious, reviewed policy decision, not a silent drift. **No policy code was
   changed.**

## Real-defect findings

**None.** Every invariant the harness exercises held on the existing Task
1.1–1.11 code. No production code required a fix; the two carried-forward items
were test-tightening only.

## Privacy / data movement (R-022.4, T-14)

The headline privacy regression (g1) seeds a canary secret into a synthetic
message's subject, body, HTML comment, and sender display name, runs it through
the full pipeline (ingress seam → durable job → worker parse/auth/inspection/
policy → persisted evidence+assessment+findings → read-only API), and proves
the canary is **absent** from: every persisted cell across the four security
tables, every API response body (list + detail), the API error body, and every
captured structured-log line (ingress + worker). The raw sender address is also
asserted absent from logs. No egress occurs — the no-network tests (h1/h2)
prove the parser/inspections/worker open no socket and import no network/model
client.

## Coverage limitations (honest scope)

- **Fixtures/stubs only — no real model, GPU, account, or network.** The
  "broken model" invariant (j1/j2) uses a stub uncalibrated estimate on the
  policy input, not a real model run. The end-to-end tests use the house
  SQLite scratch DB + `InMemoryObjectStore` + `StubMailTransport`, exactly as
  the per-task suites.
- **Property-based = deterministic corpus** (Hypothesis absent; see note).
- **Threats DEFERRED to later phases (NOT covered here, by design):**
  - **T-09 (URL SSRF / DNS rebinding)** → Phase 2 (feeds/scanners/SSRF).
  - **T-13 (OmniRoute hybrid → cloud fallback refusal)** → Phase 4 (remote
    route/refusal). Phase 1 performs no model/remote inference at all.
  - **T-21 (training-data memorization / weight extraction)** → Phase 6
    (training-data leakage). No training occurs in Phase 1.
  - T-08 attachment detonation, T-19/T-20 feed/label poisoning, T-22 RAG
    namespace leakage, T-27 artifact supply chain also belong to Phases 2–6 and
    are out of the Phase-1 slice.

## Schema / migration impact

**None.** No contract model, policy constant, DB schema, alembic revision, or
wiring was changed. The two added files are test-only.

## Next eligible task

**1.13 — the G1 gate (STOP/approval step).** NOT started. Per the steering file
and tasks.md, the phase gate requires explicit owner approval before any later
task is activated.
