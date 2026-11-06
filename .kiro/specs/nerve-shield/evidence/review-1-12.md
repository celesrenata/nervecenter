# Consolidated Phase-1 security + regression harness (Task 1.12)

Task 1.12 adds a cross-cutting adversarial/regression harness proving the invariants that only emerge when the whole Phase-1 `mail_security` slice runs together, plus the committed evidence file the done-criterion demands. The diff is three new files — `tests/backend/test_mail_security_harness.py` (32 tests), `tests/backend/_mail_security_harness_helpers.py` (shared synthetic corpora + durable harness), and `.kiro/specs/nerve-shield/evidence/phase1-security-harness.md`. No file under `backend/` is touched: `git diff e3cf25a...HEAD --name-status` shows three additions and `git diff` of `backend/mail_security/policy.py` is empty, so the "tests + evidence only, no production change" scope claim is confirmed at the git level, not just asserted. The harness drives the real parser, auth classifier, inspections, policy, worker, repository, and read-only API against adversarial fixtures and asserts against the real contract (real reason codes, real dispositions, real enum members), not invented test doubles.

Watch for: the two deliberate-but-reviewable scope calls, both honest in the evidence and defensible for a tests-only task — (1) **confirmed** the "property-based" malformed-MIME class is a deterministic parametrized corpus, not Hypothesis-driven randomization, because Hypothesis is not a repo dependency and none was added; (2) **confirmed** the PROMPT_INJECTION disposition=REVIEW / threat_likelihood=NOT_EVALUATED asymmetry is asserted-and-documented as the current safe contract rather than changed, which is the correct choice for a task forbidden from altering policy semantics. Neither is a defect. No new production feature, contract, policy constant, schema, migration, or wiring was introduced.

**Verdict**: APPROVED

## High-level view

The scope is clean. The change is additive and test-only, verified by the name-status diff and by `policy.py` being byte-identical to the base. Every invariant the task enumerates (a)–(k) maps to at least one test, and each test asserts against the production contract rather than a trivial restatement, so the harness proves behavior rather than tautologies.

The adversarial coverage is real. Malformed and oversized MIME terminate within tiny caps as typed `INSPECTION_INCOMPLETE` and never crash; forged `Authentication-Results` tops out at `UNTRUSTED_HEADER`/`PROVIDER_REPORTED` and never reaches `INDEPENDENT`, legacy body-only collapses to UNKNOWN; a provider-PASS (compromised-but-signed) sender plus a payment/credential ask is reviewed with an explicit `UNTRUSTED_AUTHORITY_CLAIM_IGNORED` and zero granted capabilities; content-absent, scan-timeout, and dead-letter paths all stay off `SAFE_TO_PROCESS`. These assertions line up with the `decide_policy` source I read, so they pin the actual rules.

The isolation and privacy proofs are the strongest part. Cross-account isolation is proven at both the repository and the API layer, including the case where the caller supplies account B's exact derived id — the API returns a 404 byte-identical to the "does not exist" response. The headline canary test seeds a searchable secret into subject, body, HTML comment, and sender display name, runs the full ingress→worker→persist→read-only-API pipeline, and asserts the canary is absent from every persisted cell across the four security tables, both API response bodies, the API error body, and every captured structured-log line (plus the raw sender address absent from logs).

The broken-model invariant is proven correctly via a stub estimate, not a real model: a maximally-confident benign `model_benign_estimate=0.0` cannot pull high-impact deterministic evidence off a restrictive disposition, and the uncalibrated number is recorded as `UNCALIBRATED_MODEL_ESTIMATE` evidence rather than acted on. No-network is proven both positively (socket constructors monkeypatched to raise, full offline pipeline still completes) and statically (parser/inspections/auth/worker import no socket/http/url/dns/model-client token). Legacy-attention parity asserts the durable attention snapshot is identical flag-on vs flag-off with zero provider writes.

The prompt-injection concern the owner flagged is addressed on two axes: the RESTRICTED escalation for injection+deception is pinned exactly, standalone injection is pinned to REVIEW with a documented NOT_EVALUATED threat axis, and a separate test proves the injection signal is actually surfaced by the offline inspections rather than silently dropped — so any future change to the asymmetry becomes a conscious, reviewed policy decision.

The commit message records the verification the task requires and I did not re-run: harness module 32 passed; full non-integration suite 5162 passed / 0 failed (baseline 5130 + 32); ruff clean on the two new files; pyright `backend/mail_security` 0 errors. The evidence file records the same commands with observed results and a per-test T-id/I-id/R-id map, satisfying R-025.

<details>
<summary>Issues (3)</summary>

1. **Property-based class is a deterministic corpus** — Hypothesis is not a repo dependency and was correctly not added; the malformed-MIME class is a parametrized corpus exhaustive over the parser's typed `IncompleteReason` modes. Honestly disclosed in both the test module and the evidence file. No action required for Task 1.12; if true randomized property testing is wanted later it is a scoped follow-up, not a gate blocker. (**confirmed**, non-blocking)
2. **PROMPT_INJECTION axis asymmetry asserted, not changed** — standalone PROMPT_INJECTION yields disposition REVIEW but leaves `threat_likelihood` NOT_EVALUATED because the category is intentionally outside `_DECEPTION_CATEGORIES` in the unchanged policy. The harness locks this in as the current safe contract and documents that surfacing it on the threat axis would be a reviewed policy change. Correct call for a tests-only task; carry it forward as a conscious policy question for a later phase. (**confirmed**, non-blocking, by design)
3. **Deferred threats explicitly out of scope** — T-08, T-09, T-13, T-19/T-20, T-21, T-22, T-27 are listed as deferred to Phases 2–6. Correct per the phase gating (Phase 1 performs no model/remote inference, no feeds, no training). Recorded so the G1 gate review does not mistake deferral for a gap. (**confirmed**, non-blocking, by design)

</details>

<details>
<summary>Details</summary>

### Malformed + oversized MIME terminates typed, never crashes (a1/a2 — T-06/T-07, R-003, I-02)

The corpus runs eight adversarial inputs through `parse_mime_evidence` under a deliberately tiny `ParserLimits` (512 source bytes, depth 3, 6 parts, 8 headers): empty, oversized source, header-count-exceeded, giant header, 10-deep nesting against a depth-3 cap, an unexpanded archive attachment, a truncated multipart, and raw binary garbage. The invariant asserted is strong and matches R-003.2/R-003.3: the parser must never raise, must emit a typed `IncompleteReason` completeness code, and every resulting finding must be `SCANNER_FAILURE` / severity `UNKNOWN` / status `INCOMPLETE` with `inspection_complete == False`. The parser→policy seam test then feeds the incomplete evidence into `decide_policy` and pins the disposition out of `SAFE_TO_PROCESS` and into `{INCOMPLETE, REVIEW, RESTRICTED}`. This proves the "malformed input cannot produce a clean result" rule rather than merely "the parser returns something."

The deterministic-corpus substitution for Hypothesis is honest and reasonable: the corpus is exhaustive over the enumerated `IncompleteReason` modes, which is what matters for proving typed termination. It is weaker than randomized fuzzing at finding unknown crash inputs, but adding a dependency would itself violate the tests-only scope.

### Forged auth never independent; legacy body-only UNKNOWN (b1–b4 — T-01/T-28, R-004)

A header-only `dkim=pass` with the default empty `TrustedReceiverBoundary` never yields `INDEPENDENT` provenance on any axis (b1). Even with the authserv-id placed inside a trusted boundary, a header-derived PASS tops out at `PROVIDER_REPORTED` — it can never reach `INDEPENDENT`, which requires transport/crypto evidence the header path cannot carry (b2). This is exactly the T-01 red-team expectation ("at most `UNTRUSTED_HEADER`, never independently verified") and the "never SPF declared independently verified without transport evidence" item in the tasks "never mark complete" list. Legacy body-only mail (no auth headers) collapses every axis to UNKNOWN/UNKNOWN with a `NO_AUTH_EVIDENCE_CAPTURED` completeness code, never defaulting to PASS (b3, T-28). The end-to-end worker variant confirms a forged-auth message persists a disposition that is not `SAFE_TO_PROCESS` (b4).

### Compromised signed sender is reviewed, never auto-trusted (c1 — T-02/T-04)

Provider-reported PASS on SPF/DKIM/DMARC plus a payment ask and a credential ask, with `claims_prior_approval=True`, lands in `{REVIEW, RESTRICTED}` and never `SAFE_TO_PROCESS`. The test goes beyond the disposition and pins the security consequences: `action_capabilities == []`, `human_approval_required is True`, and `UNTRUSTED_AUTHORITY_CLAIM_IGNORED` in the reason codes. This directly encodes the invariant "strong sender authentication is not proof of benevolent intent" and the "review action treated as payment approval" must-fail, and the asserted reason code exists in the unchanged policy source.

### Content-absent / timeout / dead-letter never clean (d1–d4 — I-02, T-18)

Four paths are covered: a policy call with `inspection_complete=False` and a `SCAN_TIMEOUT` code (disposition off SAFE, threat axis NOT_EVALUATED); a worker run with the object store wiped so content is unresolvable (ERROR_RETRYABLE, disposition not SAFE); a simulated scan timeout via an injected monotonic clock (ERROR_RETRYABLE, `last_error_code == "SCAN_TIMEOUT"`, processing state INCOMPLETE); and a retries-exhausted dead-letter (DEAD_LETTER, still INCOMPLETE). This is the "denied services cannot produce clean" / "a new scanner fails or times out but the message is marked clean" must-fail, proven on the real worker rather than the policy seam alone.

### Idempotent outbox replay (e1 — R-018, T-16, T-17)

The ingress is replayed three times and still yields exactly one job (idempotent on the outbox key, T-16). The worker then processes to DONE, the job is forced back to PENDING to simulate a crash-before-ack, and reprocessing yields exactly one assessment revision with no duplicated finding keys (T-17). This is the "duplicate attention mint on replay" must-fail and the R-018.3 crash-recovery clause, exercised through the real repository.

### Cross-account isolation at repo and API (f1/f2 — R-023, T-15)

The repo test derives distinct assessment ids for the same logical message key under two accounts (proving the id derivation is account-scoped), then confirms account B handed A's exact id gets `None` and empty findings while A still reads its own row. The API test is the sharper one: a caller scoped to account A requesting B's exact derived id receives a 404 whose JSON body is byte-identical to the "does-not-exist" 404, and the list endpoint returns no B rows. This satisfies R-023.4 ("deny access even if it knows the message ID or content hash") and closes the information-leak-via-status-code gap that a plain 403-vs-404 split would open.

### Canary absent pipeline-wide (g1 — T-14, R-022.4, headline)

The headline privacy proof seeds a distinct canary into the subject, body, HTML comment, and sender display name, runs the full pipeline on one owned event loop (ingress seam → durable job → worker parse/auth/inspection/policy → persisted evidence+assessment+findings → read-only API), and then asserts absence across four surfaces: every persisted cell in all four security tables (serialized through `json.dumps` so nested/JSON columns are covered), both API response bodies (list + detail), the API error body, and every captured structlog line. It additionally asserts the raw sender address `evil-login.example` is absent from logs, and that the detail endpoint still returns 200 — proving the row is readable-but-redacted, not simply withheld. This is the "body copied to trace" / "raw-body logs" must-fail closed end to end, and the no-egress half is backed by the no-network tests below.

### No network: positive block + static scan (h1/h2 — T-05, R-005.2, closure rule)

The positive proof monkeypatches `socket.socket`, `create_connection`, and `getaddrinfo` to raise, verifies the guard actually bites, then runs parse→inspections over an adversarial HTML message (remote pixel + deceptive link + injection alt) to completion with zero socket attempts; the remote resource is recorded as evidence (`has_remote_resources`) but never fetched, and the alt-text injection is an inert PROMPT_INJECTION finding. The static scan reads the source of parser/inspections/auth/worker and asserts none reference `socket`/`http`/`urllib`/`requests`/`dns`/`omniroute`, and that the worker contains no `inference` reference. The static scan is a coarse substring check (it would miss, e.g., an indirect import through a helper module), but combined with the positive socket-block proof it is adequate evidence for the Phase-1 "network off by default" posture and directly serves the closure rule (mitigation in typed code, not a prompt).

### Legacy attention parity / zero provider writes (i1 — T-29/T-26, R-001.4)

The same incoming-request message is run flag-off and flag-on. Flag-off schedules and assesses nothing; flag-on schedules and assesses exactly one job while the security worker also runs. The durable `attention_items` snapshot (id/title/state/importance) is asserted byte-identical between the two runs, proving the security plane in SHADOW neither creates, alters, nor suppresses attention, and the read-only stub transport makes no provider write. This is the T-29 "zero provider writes" and T-26 "malicious mail cannot bypass the attention gate" pair, and the "existing ingestion and attention regression tests red" must-fail guarded directly.

### Broken-model invariant via stub (j1/j2 — R-022.3, R-007.2)

A stub `model_benign_estimate=0.0` (a maximally-confident wrong "SAFE") on evidence carrying a payment ask and a deceptive link cannot move the disposition off `{REVIEW, RESTRICTED}`, and the number is recorded as `UNCALIBRATED_MODEL_ESTIMATE` rather than acted on (j1). A benign model number on otherwise-clean, complete mail still cannot reach SAFE_TO_PROCESS without INDEPENDENT auth — it lands at LOW_PRIORITY (j2). This encodes "a model can report SAFE while deterministic evidence reports high-impact fraud; policy must still review" using a stub on the policy input exactly as R-022.3 requires, with no real model, GPU, or network — correct for Phase 1, which performs no inference at all.

### Carried-forward pins: RESTRICTED branches and injection asymmetry (k1–k5)

The two RESTRICTED branches are now pinned exactly (previously only membership in `{REVIEW, RESTRICTED}`): payment-ask + deceptive-link is exactly RESTRICTED with both `CREDENTIAL_OR_PAYMENT_ASK` and `DECEPTIVE_URL_FINDING` reason codes (k1), and injection + deception is exactly RESTRICTED with both `PROMPT_INJECTION_FINDING` and `DECEPTIVE_URL_FINDING` (k2). The contrast pin proves a credential ask *alone* is exactly REVIEW, tying the RESTRICTED escalation to the combination rather than the ask (k3). I traced these against the unchanged `decide_policy`: Rule 1 fires on the high-impact ask and escalates to RESTRICTED on a deception/injection co-signal, Rule 2 escalates to RESTRICTED only when injection *and* deception co-occur — the assertions match the code exactly, so a regression from RESTRICTED down to REVIEW would now fail.

For the injection asymmetry, the standalone PROMPT_INJECTION test pins disposition REVIEW with `human_approval_required`, the `PROMPT_INJECTION_FINDING` reason code, not-SAFE, and `threat_likelihood` NOT_EVALUATED (k4). The accompanying test proves the injection signal is emitted by the offline inspections for an injection phrase in a body part (k5), so the NOT_EVALUATED threat axis is a deliberate policy choice (PROMPT_INJECTION is intentionally outside `_DECEPTION_CATEGORIES` in the unchanged source), not a dropped signal. Choosing to assert-and-document rather than change the axis is the right call for a task forbidden from altering policy semantics, and it honors the owner's "don't forget about prompt injection" by locking the behavior so any future change is reviewed.

### Verification recorded by the implementer (R-025)

Per the commit message and evidence file (not re-run here): `uv run pytest tests/backend/test_mail_security_harness.py -q` → 32 passed; `uv run pytest tests/ -m "not integration" -q` → 5162 passed / 0 failed (baseline 5130 + 32 new); `uv run ruff check` on the two new files → clean (repository-wide ruff has ~201 pre-existing findings in unrelated test files, disclosed); `uv run pyright backend/mail_security` → 0 errors. The evidence file carries the per-test T-id/I-id/R-id coverage map, the honest coverage-limitations section (fixtures/stubs only, deterministic corpus, deferred threats), and the "no real defect found / schema-migration impact none" close-out. The counts and clean-check claims in the evidence match what the commit message records.

</details>

<details>
<summary>File map</summary>

- `tests/backend/test_mail_security_harness.py` — the 32-test consolidated adversarial + regression suite (invariants a–k).
- `tests/backend/_mail_security_harness_helpers.py` — test-only shared synthetic MIME corpora (benign, forged-auth, phishing deceptive-link, alt-text injection), the canary constant, and the durable SQLite + InMemoryObjectStore + real-worker harness factory.
- `.kiro/specs/nerve-shield/evidence/phase1-security-harness.md` — committed evidence: exact verification commands + observed results, per-test coverage map, carried-forward items, honest scope limitations.

Full diff: `git -C /Users/celes/sources/celesrenata/nervcenter diff e3cf25a...HEAD`

</details>
