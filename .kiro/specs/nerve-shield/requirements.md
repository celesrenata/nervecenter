# NerveShield — Requirements (Kiro Feature Spec)

**Version:** 1.0 (2026-11-04)  
**Owner:** NerveCenter  
**Status:** Design-approved direction; individual phases not yet implemented  
**Product vision:** A zero-trust, local-first **communications immune system** that separates sender identity, content safety, intention, importance and permission to act. Email is the first channel, not the last.

## Product outcome and non-goals

A person with many accounts, subscriptions, bills, relationships and obligations should no longer have to manually sift every email to recognize what is dangerous, what is meaningful and what requires action. NerveShield should defend the user *and* increase NerveCenter's useful autonomy without allowing malicious content to control that autonomy. It must be explainable: **why was this email judged suspicious, why did it matter, and why was (or wasn't) an action proposed?**

**Immediate scope:** defensive assessment of already-received mail, shadow-mode verdicts, safe explanation and later gated task/attention integration. **Future scope:** message-family campaign graph, personal expectedness model, cross-channel risk, continuous learning, optional SMTP gateway. **Explicitly not promised:** perfect phishing detection; proving human intent; retrospectively validating all SPF/DKIM; secure sanitization by merely prompting an LLM; enterprise pre-delivery enforcement while using Gmail/IMAP.

## Actors and authorities

- **Mailbox owner:** independently controls accounts, consent, training datasets, privacy settings and action approvals.
- **Source connector:** imports messages and records provenance; its content is untrusted by default.
- **Deterministic security plane:** validates/normalizes evidence; never infers private user intention as fact.
- **Accelerated classifier plane:** produces versioned, measured estimates; cannot issue tool commands.
- **Semantic analysis plane:** generates schema-constrained observations and referenced evidence; cannot authorize itself.
- **Policy decision point:** only authority to allow sensitive processing and downstream actions.
- **Human reviewer:** can correct verdicts and explicitly approve consequential actions.
- **Agent orchestration:** receives a narrow, policy-minted capability and safe task facts, not executable instructions from an email.

## Requirement 1 — Preserve the existing platform (R-001)

**User story:** As the operator, I want security features integrated into NerveCenter's existing services so updates don't break communication, follow-up, mail sync or attention.

1. **WHEN** a developer starts work **THE SYSTEM SHALL** revalidate current HEAD against the documented baseline and record any divergence.
2. **WHEN** mail is ingested **THE SYSTEM SHALL** reuse existing connector, deduplication, object-storage, durable communication and cursor facilities where supported.
3. **IF** security processing fails **THE SYSTEM SHALL** preserve existing durable ingestion semantics and an accurate `PENDING`/`ERROR` assessment state, not silently mark the email safe.
4. **WHEN** shadow mode is enabled **THE SYSTEM SHALL** preserve existing user-visible behavior while recording independent security decisions without privileged effects.
5. **WHEN** promotion to enforcing mode is proposed **THE SYSTEM SHALL** demonstrate tested integration with existing attention/commitment paths before permitting untrusted messages to create actionable work.

## Requirement 2 — Explicit post-delivery scope and evidence fidelity (R-002)

**User story:** As the operator, I need to know what mail evidence NerveShield actually possesses and what security conclusions it can justify.

1. **WHEN** a message enters the security pipeline **THE SYSTEM SHALL** record account, provider, folder/label, unique source reference, time, source format, and evidence-fidelity level.
2. **IF** full RFC 5322 or MIME bytes are available **THE SYSTEM SHALL** preserve them by protected reference and hash before lossy normalization.
3. **IF** only selected headers or decoded body text are available **THE SYSTEM SHALL** explicitly mark missing envelope, original-header, signature, part or attachment evidence.
4. **WHEN** classification examines provider-reported authentication **THE SYSTEM SHALL** distinguish trusted-provider assertions, independently verified results and unverified claims.
5. **IF** historical SPF/DKIM or DMARC cannot be independently validated **THE SYSTEM SHALL** report `UNKNOWN` or `PROVIDER_REPORTED` rather than fabricated verified states.

## Requirement 3 — Safe bounded MIME and content parsing (R-003)

1. **WHEN** a message is parsed **THE SYSTEM SHALL** preserve raw-to-normalized provenance, decode MIME with bounded resources, and identify HTML/plain parts, links, images and attachments without executing them.
2. **WHEN** a parser encounters malformed or adversarial input **THE SYSTEM SHALL** return typed errors and safely bounded partial evidence instead of crashing the worker.
3. **IF** decoding detects excessive size, nested parts, archives, complex encodings or decompression risk **THE SYSTEM SHALL** stop at configured limits and attach an `INSPECTION_INCOMPLETE` finding.
4. **WHEN** parsing HTML **THE SYSTEM SHALL** refrain from rendering remote content, executing scripts or following resources.
5. **WHEN** canonical URLs are created **THE SYSTEM SHALL** retain both display and actual target evidence without opening them.

## Requirement 4 — Authentication and sender provenance (R-004)

1. **WHEN** authentication evidence exists **THE SYSTEM SHALL** represent SPF, DKIM, DMARC, ARC and trust-source confidence independently.
2. **IF** an `Authentication-Results` field originates outside a configured trusted boundary **THE SYSTEM SHALL NOT** treat it as verified authentication.
3. **WHEN** sender metadata is analyzed **THE SYSTEM SHALL** inspect visible From/Reply-To/Return-Path mismatches, display-name fraud, Unicode/confusable domains, bulk flags, and history.
4. **IF** a signed or authenticated sender is compromised **THE SYSTEM SHALL** allow content and intent evidence to override any simplistic trust assumption.
5. **WHEN** domain reputation data is unavailable **THE SYSTEM SHALL** say `UNKNOWN` instead of interpreting absence as safe or malicious.

## Requirement 5 — Deterministic threat inspections (R-005)

1. **WHEN** an email is inspected **THE SYSTEM SHALL** produce typed findings for suspicious URLs, tracking pixels, remote resource fetches, dangerous MIME types, attachment concerns, deceptive claims and prompt injection indicators as observable evidence allows.
2. **WHEN** checking links **THE SYSTEM SHALL** perform offline lexical, Unicode, host, path and text-target analysis before considering any network resolution.
3. **IF** network intelligence is enabled **THE SYSTEM SHALL** use constrained egress with SSRF prevention, redirect limits, DNS rebinding defense, timeouts, privacy policy and auditing.
4. **IF** attachment scanning is enabled **THE SYSTEM SHALL** use a separated unprivileged scanner and explicit malware-engine version; no arbitrary execution on GPU/inference nodes.
5. **WHEN** deterministic inspections finish **THE SYSTEM SHALL** preserve detector versions, evidence references, severity and completeness.

## Requirement 6 — Threat intelligence with integrity and expiration (R-006)

1. **WHEN** a threat feed is ingested **THE SYSTEM SHALL** validate its provenance, signature or transport integrity as applicable, licensing, parser bounds and indicator schema.
2. **WHEN** an indicator is matched **THE SYSTEM SHALL** attach source, match method, confidence, feed version, observation time and expiry rather than immediately performing an irreversible action.
3. **IF** intelligence expires or feeds are unavailable **THE SYSTEM SHALL** invalidate stale assertions without erasing the historical audit evidence.
4. **WHEN** a trusted user supplies an override **THE SYSTEM SHALL** scope the override by account, sender, indicator, expiry and policy approval.
5. **WHEN** indicators are promoted **THE SYSTEM SHALL** support dry-run replay and rollback.

## Requirement 7 — Distinct score axes and abstention (R-007)

1. **WHEN** any assessment is produced **THE SYSTEM SHALL** represent technical authenticity, threat likelihood, threat severity, personal relevance, urgency, actionability and uncertainty as distinct fields.
2. **WHEN** an ML score is displayed as a probability **THE SYSTEM SHALL** use a separately validated calibration mapping and record its version; otherwise label it as an uncalibrated score.
3. **IF** evidence is insufficient, contradictory or a dependency times out **THE SYSTEM SHALL** permit `ABSTAIN`, `UNKNOWN`, or `REVIEW` instead of manufacturing certainty.
4. **WHEN** signals are fused **THE SYSTEM SHALL** use versioned, testable policy/calibration logic rather than naively averaging LLM numbers and heterogeneous heuristics.
5. **WHEN** a user reviews a decision **THE SYSTEM SHALL** show key supporting and contradicting signals and which checks were not performed.

## Requirement 8 — Intel Arc distributed detection plane (R-008)

**User story:** As the operator, I want the four Intel Core Ultra 9 185H / Arc iGPU systems to absorb high-volume low-cost mail assessment without wasting the RTX 5090 or 4070 Ti SUPER.

1. **WHEN** the Arc tier is configured **THE SYSTEM SHALL** discover/validate actual GPU availability and expose measured runtime health.
2. **WHEN** a candidate detector is evaluated **THE SYSTEM SHALL** compare calibrated model quality, throughput, latency, memory footprint, quantization and CPU fallback against a baseline.
3. **WHEN** requests arrive **THE SYSTEM SHALL** batch with bounded wait, observe backpressure, distribute by worker health and preserve message/account isolation.
4. **IF** SR-IOV virtual-function deployments prove unreliable **THE SYSTEM SHALL** support host physical-function deployment rather than requiring SR-IOV.
5. **IF** the GPU is unavailable **THE SYSTEM SHALL** either use a documented CPU fallback or yield an incomplete assessment without asserting safety.
6. **WHEN** a model runs **THE SYSTEM SHALL** report exact model identity, artifact digest, compiler/runtime version and latency metrics.

## Requirement 9 — Selective, schema-constrained Granite extraction (R-009)

1. **WHEN** a message meets a semantic-analysis escalation policy **THE SYSTEM SHALL** invoke the configured `hybrid/reader` route only after evaluating the actual provider, model, fallback chain and data policy.
2. **WHEN** a reader responds **THE SYSTEM SHALL** validate output against a strict semantic-observation schema with evidence references and bounded input/output sizes.
3. **IF** the route would cross to an unauthorized remote model **THE SYSTEM SHALL** deny inference, emit a policy finding and preserve an honest incomplete status.
4. **IF** extraction fails schema validation **THE SYSTEM SHALL** retry at most the configured bounded number and retain a typed error; it SHALL NOT deserialize free-form prose into agent actions.
5. **WHEN** semantic content includes apparent instructions to NerveCenter **THE SYSTEM SHALL** treat them as email claims, not executable instructions.

## Requirement 10 — Conditional Qwen adversarial intent reasoning (R-010)

1. **WHEN** a calibrated escalation rule selects high-impact or contradictory messages **THE SYSTEM SHALL** optionally invoke `hybrid/code` with minimal sanitized summary and cited evidence.
2. **WHEN** Qwen analyzes a message **THE SYSTEM SHALL** assess consistency among claimed sender, authenticated evidence, requested action, conversation history and independent context.
3. **WHEN** the second model disagrees with lower layers **THE SYSTEM SHALL** preserve both opinions and escalate the discrepancy, not automatically let the larger model win.
4. **WHEN** the model outputs an intent hypothesis **THE SYSTEM SHALL** distinguish inferred intent from proven identity or authorization.
5. **IF** Qwen cannot be reached safely or fails **THE SYSTEM SHALL** preserve the deterministic/Arc findings with `ANALYSIS_INCOMPLETE` status.

## Requirement 11 — Risk/decision separation and policy authority (R-011)

1. **WHEN** analysis is complete **THE SYSTEM SHALL** derive a versioned security disposition separately from the assessment evidence.
2. **WHEN** a downstream action is proposed **THE SYSTEM SHALL** evaluate it through NerveCenter's existing PDP with principal, capability, resource, sensitivity, evidence and approval context.
3. **IF** a message is suspected malicious or insufficiently assessed **THE SYSTEM SHALL** prevent autonomous execution of unsafe mail-derived actions; it MAY still create a safe security review item.
4. **IF** a message claims an emergency, internal privilege or prior approval **THE SYSTEM SHALL NOT** expand permissions based on that claim alone.
5. **WHEN** decisions are reevaluated following new evidence **THE SYSTEM SHALL** retain previous verdict versions and their explanation.

## Requirement 12 — Shadow-first enforcement and reversible handling (R-012)

1. **WHEN** NerveShield is first deployed **THE SYSTEM SHALL** operate in `SHADOW` and not move, delete, mark read, respond to, or quarantine provider mail.
2. **WHEN** moving to `NOTIFY`, `REVIEW_GATED`, or `ENFORCE` mode **THE SYSTEM SHALL** require an explicit configuration change and documented acceptance evidence.
3. **IF** an automatic disposition is allowed in future phases **THE SYSTEM SHALL** provide rollback, auditability and a recovery route; permanent deletion is disabled by default.
4. **WHEN** a high-risk message arrives **THE SYSTEM SHALL** retain a recoverable reference and generate a safe, sanitized security alert instead of letting content initiate external tools.
5. **WHEN** an existing legacy detector would mint an attention item **THE SYSTEM SHALL** ensure enforcing-mode trust gates precede actionable promotion without breaking idempotent commitment/follow-up behavior.

## Requirement 13 — Personal intent, relevance and expectedness (R-013)

1. **WHEN** a message is analyzed **THE SYSTEM SHALL** distinguish marketing, machine-generated, human request, personal correspondence, receipt, security alert, billing, appointment and other useful classes.
2. **WHEN** personal context is available **THE SYSTEM SHALL** check whether a request is **expected** from the relationship and workflow (rather than treating a familiar sender as automatically safe).
3. **WHEN** expectations are inferred **THE SYSTEM SHALL** provide source/reason metadata, uncertainty and privacy boundaries.
4. **IF** a verified source makes an out-of-pattern financial or credential demand **THE SYSTEM SHALL** trigger independent verification policy rather than silently authorizing the demand.
5. **WHEN** evaluating relevance **THE SYSTEM SHALL** retain the existing VIP, bulk, automated, no-reply and importance semantics unless explicitly migrated and tested.

## Requirement 14 — Google Takeout and historical data (R-014)

1. **WHEN** a user supplies an opt-in Google Takeout/MBOX/EML archive **THE SYSTEM SHALL** import it locally without network upload and maintain account/folder/label provenance.
2. **WHEN** historical imports run **THE SYSTEM SHALL** use bounded streaming parse, checkpoints, cancellation, deduplication, integrity checks and transparent failures.
3. **WHEN** Gmail labels are imported **THE SYSTEM SHALL** mark provider spam/important labels as weak labels rather than objective ground truth.
4. **IF** the archive contains attachments or malformed payloads **THE SYSTEM SHALL** prevent extraction outside its sandbox, enforce resource limits and never execute content.
5. **WHEN** an archive is processed **THE SYSTEM SHALL** expose a manifest of records, parse failures, skipped content, duplicates and evidence completeness without revealing raw secrets.
6. **IF** multiple accounts contain the same message **THE SYSTEM SHALL** preserve each account's provenance and access permissions even when deduplicating shared bytes.

## Requirement 15 — Three separate memory/learning stores (R-015)

1. **WHEN** security indicators are refreshed **THE SYSTEM SHALL** store them in an expiring, versioned threat-intelligence store, not bake them into model weights as the sole source.
2. **WHEN** relationship history is consulted **THE SYSTEM SHALL** use a protected, account-aware personal trust graph/retrieval layer, not publishable model parameters.
3. **WHEN** training examples are approved **THE SYSTEM SHALL** place them into a distinct de-identified dataset registry with provenance, consent, label confidence and retention rules.
4. **WHEN** embedding search is used **THE SYSTEM SHALL** treat embeddings and metadata as potentially sensitive, enforcing isolation, deletion and access audit.
5. **WHEN** previously learned facts are corrected **THE SYSTEM SHALL** support versioned overrides and delete/reindex operations.

## Requirement 16 — Controlled model lifecycle (R-016)

1. **WHEN** mail is received **THE SYSTEM SHALL NOT** train, fine-tune or automatically modify model weights from that message.
2. **WHEN** candidate training data is assembled **THE SYSTEM SHALL** require opt-in consent, account selection, deduplication, redaction, poisoning review and a versioned manifest.
3. **WHEN** models or LoRA adapters are trained **THE SYSTEM SHALL** log artifact hashes, dataset version, training configuration, evaluation results and permissions without embedding secrets in telemetry.
4. **IF** a candidate improves aggregate metrics but regresses on safety-critical groups, privacy tests or false positives **THE SYSTEM SHALL** block promotion.
5. **WHEN** a candidate is promoted **THE SYSTEM SHALL** support shadow/canary/A-B evaluation and rollback to the last known-good version.

## Requirement 17 — Strong privacy and context firewall (R-017)

1. **WHEN** private mail content is processed **THE SYSTEM SHALL** default to `LOCAL_ONLY` inference and least-privilege, per-account access.
2. **WHEN** security findings, traces, events or API responses are emitted **THE SYSTEM SHALL** persist only safe structured metadata and opaque evidence references unless an explicit authorized detailed view is requested.
3. **IF** remote inference, remote reputation lookups or third-party attachment scanning are considered **THE SYSTEM SHALL** require explicit policy grants and clearly scoped consent.
4. **WHEN** sensitive content is stored **THE SYSTEM SHALL** verify actual encryption/secret-management guarantees rather than assuming `content_ref` implies encryption.
5. **WHEN** a user requests deletion **THE SYSTEM SHALL** implement documented removal of content, derived metadata and eligible embeddings/datasets, with explicit limits for already-exported or trained artifacts.

## Requirement 18 — Robust workflow and event integration (R-018)

1. **WHEN** security jobs are queued **THE SYSTEM SHALL** use durable, idempotent delivery with trace IDs, retry ceilings, timeouts, dead-letter visibility and replay controls.
2. **WHEN** the same source message is processed twice **THE SYSTEM SHALL** produce a stable logical identity and avoid duplicate events/tasks/attention items for a fixed version.
3. **IF** a worker crashes between persistence and acknowledgement **THE SYSTEM SHALL** recover without losing the message or forging completion.
4. **WHEN** a message has not completed security assessment **THE SYSTEM SHALL** display progress honestly and enforce stage-appropriate action limits.
5. **WHEN** the pipeline is replayed **THE SYSTEM SHALL** preserve historical decision lineage and distinguish dry-run replay from live action execution.

## Requirement 19 — Safe agent delegation (R-019)

1. **WHEN** a safe actionable message is identified **THE SYSTEM SHALL** emit a **proposed** task with minimal normalized facts, evidence refs, sensitivity class and requested capability.
2. **WHEN** a task is delegated **THE SYSTEM SHALL** mint only a policy-scoped, purpose-bound capability and NEVER grant tool authority from email text.
3. **IF** the action involves payment, outbound messaging, credential use, external disclosure, destructive operations or changes to access controls **THE SYSTEM SHALL** require independently authenticated user approval.
4. **IF** an agent encounters email-contained instructions to bypass policies **THE SYSTEM SHALL** ignore them as authority and preserve the attempt as a security finding.
5. **WHEN** the user rejects a proposed action **THE SYSTEM SHALL** preserve a safe audit record and capture feedback without training automatically.

## Requirement 20 — Security review and explanation UX (R-020)

1. **WHEN** the user views a message's security card **THE SYSTEM SHALL** show disposition, actionable reason codes, confidence/calibration, provenance, and unavailable checks.
2. **WHEN** a message is disputed **THE SYSTEM SHALL** permit explicit feedback (`safe`, `suspicious`, `misclassified`, `not relevant`) and correction scoped to the right account.
3. **WHEN** external images or HTML are displayed **THE SYSTEM SHALL** default to safe rendering without remote fetch, scripts or passive read receipts.
4. **WHEN** a high-risk item is presented **THE SYSTEM SHALL** distinguish `review message`, `independently verify sender`, and `approve action` so reviewing does not imply authorization.
5. **WHEN** the user is overwhelmed **THE SYSTEM SHALL** offer a calm, prioritized explanation without presenting every noisy automated threat as an urgent interruption.

## Requirement 21 — Metrics, evaluation and reliability (R-021)

1. **WHEN** a release candidate is evaluated **THE SYSTEM SHALL** report precision, recall, PR-AUC, calibration, false-positive/negative costs, failure coverage, end-to-end latency and escalation cost on declared evaluation sets.
2. **WHEN** data is split for evaluation **THE SYSTEM SHALL** avoid leakage via duplicate threads, near-duplicate campaigns, sender leakage and future data in training.
3. **WHEN** a phase is accepted **THE SYSTEM SHALL** produce reproducible test/benchmark commands with observed results and disclose which tests required real hardware, accounts or outside services.
4. **WHEN** operational failures occur **THE SYSTEM SHALL** surface detector/model feed version, failure reason and recovery state without sensitive payloads in telemetry.
5. **WHEN** comparing GPU hardware/runtime options **THE SYSTEM SHALL** measure realistic throughput, concurrency and utilization rather than rely on vendor TOPS alone.

## Requirement 22 — Adversarial resilience (R-022)

1. **WHEN** encountering malicious instructions in a message **THE SYSTEM SHALL** maintain a strict untrusted-data boundary through parsing, retrieval, prompting, JSON output, policy and agent calls.
2. **WHEN** evaluating the system **THE SYSTEM SHALL** include forged auth headers, signed-but-compromised senders, homoglyph attacks, benign SPF failures, encrypted attachments, link deception and poisoned datasets.
3. **WHEN** conducting tests **THE SYSTEM SHALL** verify security invariants even if an ML model or LLM emits an incorrect classification.
4. **IF** a finding references malicious content **THE SYSTEM SHALL** use redacted evidence IDs or controlled access, not echo dangerous raw content into the general task or notification store.
5. **WHEN** new dependencies or model artifacts are installed **THE SYSTEM SHALL** require artifact integrity, version pins and supply-chain review.

## Requirement 23 — Multi-account and tenant boundaries (R-023)

1. **WHEN** two accounts contain the same sender or message content **THE SYSTEM SHALL** keep account policy, access permissions, review history, trust features and actions separated.
2. **WHEN** cross-account insights could improve detection **THE SYSTEM SHALL** use explicitly authorized, privacy-preserving aggregate statistics only.
3. **IF** an account is removed or access revoked **THE SYSTEM SHALL** prevent new processing and provide a documented cleanup plan for derived records.
4. **WHEN** an actor requests a message outside its account namespace **THE SYSTEM SHALL** deny access even if it knows the message ID or content hash.

## Requirement 24 — Extensible future communications perimeter (R-024)

1. **WHEN** new channels (iMessage, SMS, social messages, documents, web captures) are considered **THE SYSTEM SHALL** allow reuse of evidence, policy, threat findings and security UI via channel adapters, without treating mail-specific authentication as universal.
2. **WHEN** a future SMTP ingress is designed **THE SYSTEM SHALL** treat it as a separately reviewed enforcement boundary with envelope/IP/DNS evidence and delivery semantics.
3. **WHEN** new models or feeds appear **THE SYSTEM SHALL** support versioned replacement and benchmark comparison without rewiring the central policy engine.

## Requirement 25 — Proof-driven delivery and controlled ambition (R-025)

1. **WHEN** Kiro implements a task **THE SYSTEM SHALL** require an observable deliverable, tests and explicit result notes rather than accepting a stub or mocked success as production readiness.
2. **WHEN** a task depends on unavailable credentials, devices or services **THE SYSTEM SHALL** implement offline fixtures and report `NOT VERIFIED ON HARDWARE`/`NOT DEPLOYED`, not claim success.
3. **WHEN** a phase completes **THE SYSTEM SHALL** require its gate checklist before progressing to activation or the next phase.
4. **WHEN** Kiro encounters a design gap **THE SYSTEM SHALL** record the choice in `decisions.md` with evidence and impact, not silently shrink scope.
5. **WHEN** any proposed feature would exceed available proof of safety **THE SYSTEM SHALL** remain in shadow mode and leave the ambitious feature behind an explicit flag.

## Global invariants — apply to every requirement

**I-01:** Email is never an authority source. Parsing / classification / LLM output must not grant permissions.  
**I-02:** Content absent, unscanned, failed or inconclusive is NEVER equivalent to clean.  
**I-03:** `SHADOW` mode has no provider mutations or consequential agent actions.  
**I-04:** No content or secret may leave the local trust domain through unverified OmniRoute fallback.  
**I-05:** The original message and message/account provenance cannot be replaced by a lossy summary.  
**I-06:** A single high-reputation sender is not a bypass for independent malicious-content signals.  
**I-07:** A failed content-security check cannot delete or advance the only recoverable source of mail without recorded retry/recovery.  
**I-08:** Model outputs are observations; deterministic policy remains the ultimate authority.  
**I-09:** Training samples, weights and vector indexes are sensitive artifacts, not inherently anonymized data.  
**I-10:** Every user-visible classification must be traceable to detector/model/policy versions and evidence IDs.  
**I-11:** Idempotent reprocessing cannot duplicate consequential effects.  
**I-12:** Security filtering must not silently destroy or suppress legitimate personal obligations.

## Phase acceptance targets

These are **release gates, not fabricated baseline measurements**; where a numeric target is not established, measure a baseline first and propose an explicit threshold for review.

- **Gate G0 (audit):** Current source and durable data flow mapped; existing tests run; evidence limitations documented; action/attention bypass identified; security threat model approved.
- **Gate G1 (safe vertical slice):** Synthetic mail → parser/evidence → deterministic findings → persisted `SHADOW` verdict → read-only API; no net fetch, no action/mutation, idempotent replay; existing suites green.
- **Gate G2 (reliability):** Bounded queues, typed failure status, worker restart and replay tests; redaction/authorization tests pass; internal rollup metrics work.
- **Gate G3 (Arc):** At least one real small classifier benchmarked on an Arc iGPU; CPU fallback verified; exact hardware/runtime evidence recorded; negative effects on privacy and routing tested.
- **Gate G4 (semantic):** Schema-validated Granite path and conditional Qwen path proven with malicious prompt-injection fixtures; remote fallback denied for private mail; model failure does not authorize action.
- **Gate G5 (history):** Takeout/MBOX import with multiple-account provenance, checkpointing and no network; data review tooling and weak-label evaluation in place.
- **Gate G6 (learning):** Reproducible, versioned dataset and candidate model with no promoted regression, documented leakage tests and rollback.
- **Gate G7 (enforcement):** Gated integration with attention/actions demonstrated; dry-run rollout, review UI, signed-off abuse-case suite, reversible rollback; explicit human approval.
- **Gate G8 (vision):** Cross-channel and prospective trust features demonstrated through separate proof-of-concept acceptance, with no presumption of access to additional personal data.

## Traceability

Implementation tasks MUST cite the relevant R-IDs, gate, changed components and tests. A Kiro agent must not close a task solely by writing documentation when the task's acceptance criteria demand functioning code. Treat all unverified capabilities as hypotheses until measured.
