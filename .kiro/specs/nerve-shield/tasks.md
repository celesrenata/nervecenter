# NerveShield — Executable, Phase-Gated Implementation Tasks

**This file is for Kiro Spec task execution.** Each `- [ ]` is actionable and must be checked only after its described evidence exists. These are **not** claims that the repo already contains the new code. Do not run all tasks across phases; execute one approved phase at a time. Keep parent phase headings unchecklisted so Kiro can execute individual numbered tasks. Every task should fit one small reviewable unit; if it grows, split it before writing code. Update task status and `decisions.md` as discoveries occur.

**Prerequisites for every task:** inspect HEAD; obey the [threat model](threat-model.md), [contracts](schemas/security-assessment.schema.json), [requirements](requirements.md), and [design](design.md). No account credentials, inbox samples, raw messages or production data required to run CI tests. No network fetch on test import. Prefer fixtures, fakes and loopback services.

## Phase 0 — Audit and engineering closure (Gate G0; NO production activation)

- [x] 0.1 Establish current HEAD and baseline. Record commit SHA, branch state, existing tests and lint/test commands; identify import or build blockers unrelated to this feature. Deliver `.kiro/specs/nerve-shield/evidence/phase0-baseline.md`. **Req:** R-001, R-025. **Done:** commands and real pass/fail counts documented.
- [x] 0.2 Trace complete Gmail and IMAP source fidelity: API modes, IMAP `RFC822` use, `MailItem` fields, selected headers, MIME bytes, attachments, source UID/labels, original/raw preservation and trusted authentication evidence. Annotate exact functions and constraints in evidence report. **Req:** R-002, R-004. **Depends:** 0.1. **Done:** evidence-level matrix exists; verified vs inferred clearly marked.
- [x] 0.3 Trace existing ingest/detection/attention transaction and cursor semantics for `MailIngestWorker`, API ingest, durable commits, follow-ups and Today visibility. Include event order diagrams and security bypass paths. **Req:** R-001, R-011, R-012, R-018. **Depends:** 0.1. **Done:** no guessed integrations; replay/failure semantics enumerated.
- [x] 0.4 Audit object storage encryption, PII redaction, DB/migrations, account isolation, backups and actual deployment configuration. Deliver data-classification matrix and list any unverifiable guarantees. **Req:** R-017, R-023. **Depends:** 0.1. **Done:** assertions backed by code/config/tests, not comments alone.
- [x] 0.5 Audit OmniRoute/PDP path and `hybrid/reader`, `hybrid/code` fallback policy; verify exact model identifiers or record unknown. Trace existing inference transport and supported structured-output patterns. **Req:** R-009, R-010, R-017. **Depends:** 0.1. **Done:** local-only test plan and route-policy gap list.
- [x] 0.6 Produce integration map, schema/migration strategy, threat-model update and final ADR recommendations in `decisions.md`; identify the smallest safe Phase 1 patchset and ownership boundaries. **Req:** R-001, R-025. **Depends:** 0.2–0.5. **Done:** file-level plan, explicit unknowns, rollback and gate risks.
- [x] 0.7 **G0 gate — STOP.** Summarize evidence to owner, request approval for Phase 1; do not silently begin implementation. **Req:** R-025. **Depends:** 0.6. **Done:** documented owner decision and gate signoff.

## Phase 1 — Zero-network security spine and shadow verdicts (Gate G1)

- [x] 1.1 Define versioned Pydantic models for message-evidence manifests, typed security findings, security assessment/disposition, and schema validation. Align with existing model conventions and JSON schema examples; forbid unexpected fields and cap user-controlled string lengths. **Req:** R-002, R-005, R-007, R-025. **Depends:** G0. **Done:** strict serialization and invalid-input tests.
- [x] 1.2 Implement pure deterministic identifier/revision functions scoped by account/source, plus stable analysis-profile/version keys; distinguish same RFC Message-ID delivered to different accounts. **Req:** R-018, R-023. **Depends:** 1.1. **Done:** property tests prove dedup, version change and cross-account isolation.
- [x] 1.3 Build offline bounded MIME evidence parser using existing `email` library or reviewed equivalent. Cap headers, bytes, MIME depth, parts and decoded content; extract plain/html, URLs, images, attachment descriptors and parse-failure metadata without network/execution. **Req:** R-002, R-003. **Depends:** 1.1. **Done:** test corpus of maliciously malformed, oversized and benign mixed MIME examples.
- [x] 1.4 Implement authentication-evidence representation: classify source of SPF/DKIM/DMARC/ARC claims, identify trusted receiver boundary config, reject forged `Authentication-Results` as verified; set unknown correctly for legacy-body-only. **Req:** R-002, R-004. **Depends:** 1.3. **Done:** forged and provider-reported fixture tests.
- [x] 1.5 Implement pure offline sender/domain/URL inspections: IDNA and Unicode confusables, Reply-To/display mismatch, deceptive URL text, hidden/remote HTML resources and basic tracking pixels. Do not resolve URLs. **Req:** R-005, R-006. **Depends:** 1.3. **Done:** deterministic findings and explainable rule IDs; no network in CI.
- [x] 1.6 Implement conservative security decision policy v1: separate threat/importance/auth/action fields, `UNKNOWN` handling, `SHADOW` disposition, reasons and model/not-evaluated statuses; no single score shortcuts. **Req:** R-007, R-011, R-012. **Depends:** 1.1, 1.4, 1.5. **Done:** table-driven cases (benign SPF failure, compromised signed sender, credential ask, missing content, junk newsletter).
- [x] 1.7 Add minimal durable repositories/migrations for account-scoped security jobs, evidence manifest and assessment/finding *metadata* (no raw body). Respect existing repository patterns and sensitive-data constraints. **Req:** R-017, R-018, R-023. **Depends:** 0.4, 1.1. **Done:** migration up/down or documented forward-only policy, DB tests and cross-account access denials.
- [x] 1.8 Add an opt-in ingestion seam that schedules idempotent security work from the current worker/API paths. Do not reimplement fetch, leak secrets or change cursor advancement. Use verified transactional outbox mechanism where possible; otherwise a reconcilable pending-job scanner with failure evidence. **Req:** R-001, R-018. **Depends:** 0.3, 1.2, 1.7. **Done:** synthetic message triggers one job; retry/restart produces no duplicate; existing attention unaffected in SHADOW.
- [x] 1.9 Implement local-only security worker: resolve content under policy, scan bounded evidence, write assessment and typed failures; default feature flag off. Do not call GPU or LLM. **Req:** R-005, R-011, R-012, R-017, R-018. **Depends:** 1.3–1.8. **Done:** full synthetic ingress-to-assessment integration test.
- [x] 1.10 Expose read-only, paginated, account-scoped security assessments API with safe redacted error/response fields. No actions or mail mutations. **Req:** R-020, R-023. **Depends:** 1.7, 1.9. **Done:** API authorization, rate and redaction tests.
- [x] 1.11 Add minimal UI security indicator in current Email or Communications view: `SHADOW`, `UNKNOWN`, `REVIEW`, `SAFE_TO_PROCESS` and skipped-check explanations; render no external resources. **Req:** R-020. **Depends:** 1.10. **Done:** existing views and relevant web tests pass.
- [x] 1.12 Security and regression harness: property-based malformed MIME, outbox replay, forged auth, idempotency, content-leak scans, no network, no provider mutations, legacy attention parity. **Req:** R-001, R-021, R-022, R-025. **Depends:** 1.1–1.11. **Done:** results with exact commands committed as evidence.
- [x] 1.13 **G1 gate — STOP.** Demonstrate offline synthetic mail → durable job → bounded parsing → findings → metadata-only SHADOW verdict → read-only UI; no lost/duplicated messages and no agent tool action. Obtain approval for Phase 2. **Req:** R-025. **Depends:** 1.12.

## Phase 2 — Durable operational security and threat intel (Gate G2)

- [x] 2.1 Implement reliable bounded queue/retry/lease/dead-letter semantics using existing event infrastructure; add a sweeper to repair missing jobs without duplicate side effects. **Req:** R-018. **Depends:** G1. **Done:** crash, timeout, double-delivery and replay tests.
- [x] 2.2 Implement time-limited threat-intelligence indicator store with explicit source, confidence, observed date, expiry, hash/canonical value, scope, version and rollback. **Req:** R-006, R-015. **Depends:** G1. **Done:** expired entries never remain live detections.
- [x] 2.3 Implement optional, disabled-by-default feed adapters; verify source license/integrity and offline fixtures; no personalized link submissions or unsafe network fetch. **Req:** R-006, R-017. **Depends:** 2.2. **Done:** failure and poisoning tests.
- [x] 2.4 Implement safe static attachment descriptor pipeline and reviewed ClamAV/YARA service interface (scanners optional; never execute attachments). **Req:** R-003, R-005, R-022. **Depends:** G1. **Done:** suspicious file fixtures, scanner timeouts and incomplete status.
- [x] 2.5 Implement optional URL lookup policy and egress interface with host/port/DNS/IP allow/deny, rebinding, redirects, private address blocks, privacy audit and timeout controls. Do not enable by default. **Req:** R-005, R-017. **Depends:** G1. **Done:** SSRF test matrix and no-egress default.
- [x] 2.6 Add per-stage health, queue age/depth, attempts, latency, source fidelity, scan completeness and safe trace IDs to existing observability. **Req:** R-018, R-021. **Depends:** 2.1–2.5. **Done:** no PII in metrics/logs; failure alert proof.
- [x] 2.7 **G2 gate — STOP.** Run restart/replay/security-completeness tests and review threat feed/sandbox failure modes; approve Phase 3. **Req:** R-025. **Depends:** 2.6.

## Phase 3 — Intel Arc classifier mesh (Gate G3)

- [x] 3.1 Inventory current NixOS / Intel GPU driver/OpenVINO/OVMS environment on all four GTi14 hosts, including PF vs VF reliability. No installs until inventory reviewed. **Req:** R-008. **Depends:** G2. **Done:** actual device IDs/runtime health and reproducible commands.
- [x] 3.2 Build CPU classifier baseline and dataset-free synthetic/authorized benchmark harness, with per-message latency and measured quality on held-out labels; record limitations. **Req:** R-008, R-021. **Depends:** G2. **Done:** measured baseline report.
- [x] 3.3 Benchmark two or more appropriate small-model approaches (text/URL/embedding+head) on OpenVINO Arc and CPU; compare data preprocessing, accuracy, throughput and quantization artifacts. **Req:** R-008. **Depends:** 3.1, 3.2. **Done:** actual model/artifact digests, wall time, resource metrics; no fabricated TOPS inference.
- [x] 3.4 Implement host PF inference service with authenticated private access, constrained request schema, safe features, dynamic batching, queue limits, rate control, health checks and explicit CPU fallback. **Req:** R-008, R-017. **Depends:** 3.3. **Done:** NixOS service config, firewall documentation, replayable benchmark fixture.
- [x] 3.5 Implement orchestrator client/pool using existing routing patterns, with per-host readiness, bounded retries, circuit breaker and per-account isolation; consume optional model evidence without making it an authority. **Req:** R-007, R-008, R-018, R-023. **Depends:** 3.4. **Done:** failure/multi-worker tests and no phantom successful scans.
- [ ] 3.6 Implement calibration/abstention mapping from held-out labels, versioned with artifacts; if data is insufficient, retain `UNCALIBRATED` and refrain from publishing probabilities. **Req:** R-007, R-021. **Depends:** 3.2–3.5. **Done:** reliability/coverage plots and regression tests. _Open: G3 marked NOT DONE; needs real labeled data, scores stay UNCALIBRATED._
- [x] 3.7 **G3 gate — STOP.** Prove at least one real Intel Arc GPU execution, compare CPU, test fallback and data privacy; approve Phase 4. **Req:** R-025. **Depends:** 3.6.

## Phase 4 — Schema-locked Granite and Qwen reasoning (Gate G4)

- [x] 4.1 Implement strict `SemanticObservation` Pydantic/schema contract with evidence refs, bounded free text and no executable instruction fields. **Req:** R-009, R-022. **Depends:** G3. **Done:** unknown keys/long strings/invalid confidence rejected.
- [x] 4.2 Add explicit local-only OmniRoute resolver and fail-closed policy guard for all alias candidates/fallback hops, with log-safe provider identity. **Req:** R-009, R-010, R-017. **Depends:** 0.5, 4.1. **Done:** cloud-fallback attack test demonstrates no remote content transfer.
- [x] 4.3 Implement minimal-evidence Granite `hybrid/reader` observer, bounded prompts without tools, output validator and typed timeout/repair failure. **Req:** R-009, R-017. **Depends:** 4.2. **Done:** real local route if provisioned; fixture path otherwise explicitly unverified.
- [x] 4.4 Implement selective Qwen `hybrid/code` challenger focusing on identity/request/expectedness mismatch; keep observations distinct and no tool authority. **Req:** R-010, R-013. **Depends:** 4.3. **Done:** message challenge fixtures and model disagreement handling.
- [x] 4.5 Implement versioned escalation policy: deterministic only -> Arc -> Granite -> optional Qwen; high-impact flags and uncertainty review; test model outage and rate budget. **Req:** R-007, R-009, R-010. **Depends:** 4.3, 4.4. **Done:** cost, high-risk coverage and safe abstention metrics.
- [x] 4.6 Test prompt injection through body, HTML alt, MIME header, link query and quoted prior thread; model outputs cannot induce tool calls, schema changes, PDP bypass or leaking content. **Req:** R-011, R-019, R-022. **Depends:** 4.5. **Done:** red-team suite green independently of model verdict.
- [x] 4.7 **G4 gate — STOP.** Demonstrate all processing local-only and bounded, including failure/route-fallback tests, and approve Phase 5. **Req:** R-025. **Depends:** 4.6.

## Phase 5 — Google Takeout, historical corpus and personalized retrieval (Gate G5)

- [x] 5.1 Implement offline, user-opt-in MBOX/EML import with explicit account selection, bounded streaming parser, archive policy, manifests and dry-run preview. **Req:** R-014, R-017. **Depends:** G4. **Done:** multi-account synthetic fixtures; no network calls.
- [x] 5.2 Add checkpoint/restart/cancel/idempotency and error quarantine for historical imports; preserve raw evidence where supported and label source fidelity honestly. **Req:** R-002, R-014, R-018. **Depends:** 5.1. **Done:** crash at every checkpoint and cross-account duplicate tests.
- [x] 5.3 Normalize Gmail labels from MBOX weakly, including Spam/Inbox/Important/Sent/Trash and custom labels, without treating labels as ground truth. **Req:** R-014, R-016. **Depends:** 5.1. **Done:** label parsing fixtures, ambiguity handled explicitly.
- [x] 5.4 Implement privacy-scoped personal trust/relationship feature index (sender history, verified channels, expected thread continuity) using existing graph/retrieval, no raw content in general graph. **Req:** R-013, R-015, R-017, R-023. **Depends:** 5.2. **Done:** allow/deny by account, sparse-history uncertainty and deletion tests.
- [x] 5.5 Implement threat-intelligence vs personal memory vs training-data boundary tests, feedback review sources and retained provenance. **Req:** R-015, R-016, R-017. **Depends:** 5.3, 5.4. **Done:** test that feedback does not change model weights or global indicators automatically.
- [x] 5.6 **G5 gate — STOP.** Local import demonstrably safe, resumable, account-isolated and non-actionable; approve Phase 6. **Req:** R-025. **Depends:** 5.5.

## Phase 6 — Privacy-preserving model training lifecycle (Gate G6)

- [x] 6.1 Build explicit opt-in data-selection flow with per-account/source consent, secret/PII detection, pseudonymization, duplicates and poison-sample screening. **Req:** R-014, R-015, R-016, R-017. **Depends:** G5. **Done:** secret-bearing examples excluded from approved training dataset.
- [x] 6.2 Implement signed/versioned manifest and immutable train/eval splits that avoid temporal/thread/campaign leakage; record weak-label source and human review. **Req:** R-016, R-021. **Depends:** 6.1. **Done:** reproducible manifest and leakage test.
- [x] 6.3 Benchmark classical baseline vs small fine-tuned encoder vs feasible LoRA candidate, with calibrated metrics, false-positive costs and GPU runtime compatibility. **Req:** R-008, R-016, R-021. **Depends:** 6.2. **Done:** real baseline comparison; no claim that LoRA is always best.
- [x] 6.4 Implement privacy evaluation: canary memorization/extraction, membership inference risk investigation, artifact access, retrieval leakage and redaction regressions; document residual risk. **Req:** R-016, R-017, R-022. **Depends:** 6.3. **Done:** reports and blocked-promotion criteria.
- [x] 6.5 Implement model registry with lineage, policy, safety metrics, shadow/canary deployment and rollback. Expose no auto-promote path. **Req:** R-016, R-021. **Depends:** 6.3, 6.4. **Done:** simulated regression prevents release and rolls back.
- [x] 6.6 **G6 gate — STOP.** Present approved datasets, privacy report and model comparisons; owner decides whether any model may enter shadow/canary. **Req:** R-025. **Depends:** 6.5.

## Phase 7 — Safe, useful actions and review UX (Gate G7)

- [x] 7.1 Implement clear safety card, structured provenance/explanation, source-fidelity and `NOT_EVALUATED` badges in Email UI; no remote image or active HTML. **Req:** R-020. **Depends:** G6. **Done:** accessibility/security rendering tests.
- [x] 7.2 Implement reviewer feedback (safe/suspicious/unimportant/wrong intent) with scope, audit and separation from training; show contextual independent-verification advice for financial/credential requests. **Req:** R-013, R-016, R-020. **Depends:** 7.1. **Done:** per-account tests, no implicit trust allowlist.
- [x] 7.3 Implement security review queue with impact prioritization and attention budgeting, preserving real commitments behind restricted messages as review candidates. **Req:** R-012, R-013, R-020. **Depends:** 7.2. **Done:** quiet-hour/important-human-message tests.
- [x] 7.4 Implement pre-promotion trust gate in legacy communication → finding → followup/attention flow for `REVIEW_GATED` **behind an OFF flag**; preserve shadow behavior and retries/ids exactly. **Req:** R-001, R-011, R-012, R-018. **Depends:** 0.3, 7.3. **Done:** malicious email cannot auto-mint an actionable non-review obligation, safe mail still can, no duplicate/replayed tasks.
- [x] 7.5 Build narrow capability-scoped task proposal contract and existing-PDP authorization adapters for calendar/bill/relationship suggestions; no email-origin tool power. **Req:** R-011, R-019. **Depends:** 7.4. **Done:** secure proposed-task payload and policy tests.
- [x] 7.6 Implement explicit confirmation workflow with action-specific approvals, expiry, audit and independent sender verification, testing sending, payments and external disclosure remain locked without explicit approval. **Req:** R-011, R-019. **Depends:** 7.5. **Done:** malicious instruction cannot escalate to approval.
- [x] 7.7 Add operations dashboard: queue status, Arc utilization, model/feeds version, false-positive review and privacy-safe cost/failure metrics. **Req:** R-020, R-021. **Depends:** 7.3. **Done:** live data wiring and redaction tests.
- [x] 7.8 Conduct adversarial release review, migration/rollback rehearsal, real-account observation in SHADOW and opt-in REVIEW_GATED canary; no provider-state modifications. **Req:** R-012, R-021, R-022. **Depends:** 7.4–7.7. **Done:** written evidence and selected accounts/rules.
- [x] 7.9 **G7 gate — STOP.** Owner explicitly approves or rejects any non-shadow activation. **Req:** R-025. **Depends:** 7.8.

## Phase 8 — Vision research spikes, each separately approved (Gate G8)

- [x] 8.1 Research threat-campaign clustering via privacy-safe feature fingerprints and time-limited evidence; build offline prototype only. **Req:** R-006, R-015, R-024. **Depends:** G7. **Done:** privacy/quality feasibility report.
- [x] 8.2 Research conversation continuity / thread hijack detection using provider evidence, message references and verified prior channel; explicitly account for fabricated thread headers. **Req:** R-004, R-013, R-024. **Depends:** G7. **Done:** synthetic high-signal tests and false-positive analysis.
- [x] 8.3 Research personal expectedness engine with opt-in per-account features, uncertainty and fair/reversible feedback. **Req:** R-013, R-015, R-024. **Depends:** G7. **Done:** baseline against unpersonalized model, privacy report.
- [x] 8.4 Research cross-channel adapter without cross-contaminating permissions; avoid access to SMS/iMessage without explicit authorization. **Req:** R-017, R-023, R-024. **Depends:** G7. **Done:** contract/adapter-only proof, no new external connector assumptions.
- [x] 8.5 Independently scope optional SMTP ingress and dedicated detonation microVMs. These are NEW security boundaries, not extension tasks to implement opportunistically. **Req:** R-005, R-024. **Depends:** G7. **Done:** separate specs, measurable security and operation tradeoffs.
- [x] 8.6 **G8 gate — STOP.** Review findings, approve any follow-on product specs; never auto-enable research features. **Req:** R-025. **Depends:** 8.1–8.5.

## Required completion report template (every code task)

```text
Task: <number and title>
Commit/branch: <SHA or uncommitted files>
Implementation: <file paths + behavior; no speculative claims>
Security invariants tested: <I-IDs and passing evidence>
Tests/benchmarks: <exact commands + observed results>
Coverage limitations: <fixtures vs hardware/accounts/services>
Privacy and data movement: <what content accessed; any egress; expected defaults>
Schema/migration impact: <contract version and rollback>
Risks or deviations: <decision register entry or none>
Next eligible task: <and dependency/gate status>
```

### Never mark a task complete when...

- It only adds a TODO, uncalled service or mocked result for a required working behavior.
- Hardware performance was estimated from TOPS instead of measured.
- A local-only test never checks resolved remote fallback routes.
- A mail security verdict is written after an untrusted message has already triggered authorized agent tools.
- Lossy content is represented as original signed MIME or SPF is declared independently verified without necessary transport evidence.
- A new scanner fails or times out but the message is marked clean.
- A migration or new endpoint bypasses account isolation or creates raw-body logs.
- Existing ingestion and attention regression tests are red or not run without explicit explanation.
