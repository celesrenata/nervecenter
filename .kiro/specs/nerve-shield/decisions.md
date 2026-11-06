# NerveShield — Decision Register and Research Agenda

Use this as a living ADR appendix. Mark choices as **PROPOSED / ACCEPTED / DEFERRED / REJECTED** with date, evidence, responsible party and rollback plan. No phase may silently override an ACCEPTED security decision.

## Architectural decisions proposed for approval

| ID | Status | Recommendation | Why / acceptance evidence |
| --- | --- | --- | --- |
| NS-ADR-001 | PROPOSED | Keep post-delivery assessor separate from future SMTP ingress gateway | Gmail/IMAP lack trustworthy envelope-level auth/transport evidence; prevents false promise of IronPort-equivalent enforcement |
| NS-ADR-002 | PROPOSED | Preserve raw RFC5322 evidence when available and explicitly label lossy sources | Needed for signature/replay/forensics, under protected object storage |
| NS-ADR-003 | PROPOSED | Separate technical authenticity, harm, urgency, relevance, expectedness, action eligibility | Stops 'legitimacy score' from conflating security and inbox management |
| NS-ADR-004 | PROPOSED | SHADOW default, no deletion/provider writes, manual gate to enforce | Avoid catastrophic legitimate-mail loss |
| NS-ADR-005 | PROPOSED | Metadata-only durable verdict and protected evidence refs | Preserve current personal-data privacy contract |
| NS-ADR-006 | PROPOSED | Async outbox/reconciliation, no external inference in cursor critical path | Preserve durable mailbox progress and replayability |
| NS-ADR-007 | PROPOSED | Physical-function Intel Arc host service first, CPU fallback | Prior SR-IOV VF instability; actual benchmarking required |
| NS-ADR-008 | PROPOSED | Verifiable local-only routes for private content; inspect all fallback hops | `hybrid/*` alias is not privacy enforcement |
| NS-ADR-009 | PROPOSED | LLM = evidence observer; PDP = sole action authority | Prompt injection and model mistakes must not become tool execution |
| NS-ADR-010 | PROPOSED | Preserve three distinct stores: threat intel, personal retrieval, training manifests | TTL updates, private deletion, avoidance of weight memorization |
| NS-ADR-011 | PROPOSED | Human/independent authorization for payments, sending, credentials, destructive/external actions | Email is not permission |
| NS-ADR-012 | PROPOSED | Versioned calibrated assessment and review/abstention | Evidence not always sufficient; raw LLM scores not calibrated |

## Required research and open questions

1. **Current transport fidelity.** Determine Gmail API `format` and IMAP fetched bytes; are all MIME parts and original DKIM signatures available? Must be proven in HEAD, not assumed. What happens when Gmail history tokens expire?
2. **Storage encryption.** Is the object store actually configured with encryption and keys, or merely `content_ref` abstraction? What can be protected without rearchitecting storage? Which backups contain copies?
3. **Cursor and durable-write correctness.** How can an outbox be added without changing current failure modes or generating duplicate messages? Is an existing transactional repository usable?
4. **Attention safety gate.** Which exact path emits durable attention and commitments? How can a pre-promotion security gate be inserted behind feature flag while keeping shadow behavior bit-for-bit compatible?
5. **Inference privacy.** How does the installed OmniRoute client resolve `hybrid/reader` and `hybrid/code`? Is real Granite 4.2 deployed? Is Qwen 3.8 27B deployed? Does any fallback go to a cloud provider? What is the enforcement point before request serialization?
6. **Arc PF support.** On each GTi14 host, which driver/kernel/firmware/OpenVINO version gives stable low-latency GPU inference? Any lingering SR-IOV wedging? Actual model warmup/cold-start numbers?
7. **Which models?** Compare lightweight lexical detector, encoder classifier, embedding + linear model and LoRA rather than fixating on a generative architecture. Is there a licensed, suitable calibration dataset?
8. **Private email training ethics.** Consent, rights in third-party messages, retention and ability to stop/retrain, membership-inference and extraction. Distinguish redaction from anonymization.
9. **Threat-intel sources.** What feeds are safe, current, licensed for a personal local deployment, and maintainable without leaking personal URLs to lookup providers?
10. **Attachment scanning.** Choose a static scanner interface; are ClamAV/YARA packages available through NixOS? Can attachment metadata be parsed without execution; where would a later microVM detonation boundary live?
11. **Scoring thresholds.** No ungrounded numbers. Evaluate class imbalance, benign mail false positives, high-impact fraud recall and confidence calibration; use data to propose release gates.
12. **Inbox action model.** Decide whether and when to gate *attention promotion* versus simply annotate. User may rely on original detection; block only once parity and recovery route are proven.
13. **Cross-account sharing.** Even hashes and embeddings can expose identity. Should aggregation be limited to publicly observable threat indicators and human-approved features?
14. **Naming and product UX.** NerveShield is a working name. Should users see a Security tab, a contextual safety strip in mail, or an integrated Today digest? Usability benchmark required.
15. **Operational costs.** Measure worker queues and model calls per 1,000 messages; optimize threat-recall per millisecond/energy instead of maximizing LLM call count.

## Phase 0 audit — ADR recommendations (G0, HEAD 45ab762)

Evidence-backed recommendations from the Phase 0 read-only audit. Each cites the evidence doc under `evidence/`. These are **recommendations for owner sign-off at G0**, not yet `ACCEPTED` (no phase may auto-advance). State changes to `ACCEPTED` only on the recorded G0 decision in `evidence/phase0-g0-gate.md`.

| ID | Phase-0 recommendation | Evidence at HEAD | Residual unknown |
| --- | --- | --- | --- |
| NS-ADR-001 | **Recommend ACCEPT.** Keep post-delivery assessor separate from any SMTP gateway. | 0.2: no RFC822/envelope/DKIM captured; Gmail=decoded text, IMAP discards raw → no trustworthy transport/envelope evidence exists. | none material |
| NS-ADR-002 | **Stay PROPOSED — owner choice.** Preserve raw RFC822 when available *requires a new optional capture seam* (Gmail `format=raw`; persist IMAP `raw`). Not present today; body is even mislabeled `message/rfc822`. | 0.2/0.4 | whether owner wants the capture seam in Phase 1 |
| NS-ADR-003 | **Recommend ACCEPT.** Separate axes (auth/threat/impact/relevance/expectedness/action). Existing detection has only relevance/importance; schema already encodes 6 axes. | 0.3/0.5; `security-assessment.schema.json` | none |
| NS-ADR-004 | **Recommend ACCEPT + reinforce.** SHADOW default, no provider writes. The live T-26 attention bypass makes shadow-first mandatory; schema enforces SHADOW⇒empty `action_capabilities`. | 0.3 | none |
| NS-ADR-005 | **Recommend ACCEPT.** Metadata-only durable verdict + opaque refs. Durable attention/commitment rows are already templated/provenance-only (P0-I). | 0.3/0.4 | in-memory reason + §8 `matched` carry body fragments — tighten when UI/events added |
| NS-ADR-006 | **Recommend ACCEPT.** Async outbox / reconciliation; no external inference in cursor critical path. Existing P0-D commit-gate + idempotent cursor is a usable transactional base. | 0.3 | outbox table design (Phase 1.7/2.1) |
| NS-ADR-007 | **Stay PROPOSED.** Arc PF-first + CPU fallback — unprovable from repo; no GPU/OpenVINO code or inventory. | 0.5 | all Arc hardware facts UNKNOWN (Phase 3) |
| NS-ADR-008 | **Recommend ACCEPT + build resolver.** Verifiable local-only routes; inspect all fallback hops. PDP `INFER_ROUTE`+sensitivity give a LOCAL_ONLY signal, but the OmniRoute transport has **no** enforcement — a fail-closed resolver (task 4.2) is required. | 0.5 | real route graph UNKNOWN |
| NS-ADR-009 | **Recommend ACCEPT.** LLM = observer; PDP = sole authority. PDP is fail-closed with namespace isolation + approval binding; no LLM mints capabilities today. | 0.5 | semantic path unimplemented |
| NS-ADR-010 | **Recommend ACCEPT (directional).** Three distinct stores. None exist yet; pgvector dep present. | 0.4 | store schemas (Phases 2/5/6) |
| NS-ADR-011 | **Recommend ACCEPT.** Human/independent auth for payments/sending/etc. `verify_approval_binding` already enforces exact payload/target/action/expiry/APPROVED. | 0.5 (pdp.py) | none |
| NS-ADR-012 | **Recommend ACCEPT.** Versioned calibrated assessment + review/abstention. Schema has calibration/abstention semantics; no calibration data yet (begin with baseline). | 0.5 | calibration dataset UNKNOWN |

**New decisions surfaced by the audit (recorded as PROPOSED):**

- **NS-ADR-016 (PROPOSED; renumbered from 013 to avoid a collision with the Phase 1 decision below, which code and tests cite as NS-ADR-013) — Account-scope new NerveShield tables explicitly.** HEAD `communications` has **no `account_id`**; person id is shared per normalized handle across accounts (R-023/T-15 risk). Model new tables on the `calendar_events.account_id` precedent. Evidence: 0.4. Rollback: tables are additive.
- **NS-ADR-017 (PROPOSED; renumbered from 014 to avoid a collision with the scan-not-run decision, which code cites as NS-ADR-014) — Treat object-store encryption as unverified until deployment proves SSE/TLS/keys/backups.** No SSE header or client crypto in `S3ObjectStore`; `http://` transits plaintext. Zone-P0 guarantee is `UNKNOWN`. Evidence: 0.4. Do not claim encryption in any finding/UX until proven.
- **NS-ADR-015 (PROPOSED) — First Phase 1 slice = Task 1.1 strict Pydantic models, tests-first, flag-off, no migration/wiring.** Lowest blast radius; dependency root; cannot perturb ingestion/attention/cursor. Evidence: 0.6.

## Experiment templates

### EXP-01: Source fidelity matrix

- Input: synthetic Gmail/API and IMAP fixtures plus provider docs, using no real account required initially.
- Output: map each source mode to available raw bytes, envelope/IP, header origin, attachment fidelity and evidence status.
- Pass: no independently VERIFIED auth without necessary cryptographic/transport proof.

### EXP-02: Arc detection bake-off

- Inputs: synthetic or appropriately licensed public examples; versioned held-out datasets.
- Variants: lexical+rules, small ONNX/OpenVINO encoder, embedding+linear head, CPU fallback.
- Record: p50/p95 latency, throughput, batch efficiency, model memory, calibration, false positives, robustness, verified GPU utilization.
- Pass: actual measured improvement or documented decision to retain CPU; a worse GPU path is not deployed merely for novelty.

### EXP-03: Strong model escalation return-on-compute

- Inputs: mixed safe, ambiguous, high-impact and adversarial messages without real private content.
- Compare: deterministic only, Arc, Arc+Granite, Arc+Granite+Qwen.
- Record: incremental error reduction, decision abstention, latency/cost, prompt-injection resistance.
- Pass: higher model tier materially improves measured outcomes; model disagreement preserved.

### EXP-04: Private model memorization

- Inputs: synthetic canary strings and reviewed non-sensitive train examples only.
- Tests: exact-string extraction attempts, membership inference research, nearest-neighbor retrieval leakage, adapter and tokenizer artifact scanning.
- Pass: published risk analysis and promotion gate; don't claim training privacy can be mathematically guaranteed without formal methods and validated conditions.

### EXP-05: Real-life attention utility

- Inputs: synthetic mail from human contacts, appointments, receipts, subscriptions, newsletters and phishing; scenarios with fatigue and time pressure.
- Compare: baseline NerveCenter relevance versus NerveShield layered triage.
- Measure: important obligation recall, unwarranted alert burden and review accuracy; correctness on high-impact security alerts.
- Pass: no silent loss of important human messages; fewer disruptive low-value alerts with explainable control.

## Change decision form

```text
Decision ID:
Date / reviewers:
State: PROPOSED | ACCEPTED | DEFERRED | REJECTED
Problem and constraints:
Options considered:
Evidence (benchmarks/tests/docs):
Choice:
Risks, privacy, compatibility and reversibility:
Requirement IDs:
Migration and rollback plan:
Follow-up task IDs:
```

## Phase 1 implementation decisions

| ID | Status | Date | Decision | Evidence / rationale | Rollback |
| --- | --- | --- | --- | --- | --- |
| NS-ADR-013 | ACCEPTED | 2026-11-05 | `SAFE_TO_PROCESS` is intentionally UNREACHABLE in Phase 1 | The decision policy only reaches `SAFE_TO_PROCESS` when `_has_independent_pass` is true, which requires `INDEPENDENT` auth provenance. Phase 0 proved the current transports capture no auth headers (BODY_ONLY fidelity), and Task 1.4 made `INDEPENDENT` structurally unreachable from headers alone. So the clean path tops out at `LOW_PRIORITY`. This is the correct fail-closed posture (I-02: absent/unscanned is never clean), **not dead code**. A later phase that adds real transport/crypto evidence (raw RFC822 capture per NS-ADR-002, or SMTP ingress) is the only thing that may legitimately enable this branch. Verified in Task 1.6 (`test_clean_but_unassessed_is_not_safe_to_process`; commit a0cc35a). | n/a — removing the branch would break forward-compatibility; leave as-is. |

**Phase 1 non-blocking items carried for later tasks (do not silently drop):**
- Task 1.6 review: a standalone `PROMPT_INJECTION` finding escalates disposition to REVIEW but leaves `threat_likelihood` at `NOT_EVALUATED` (PROMPT_INJECTION excluded from `_DECEPTION_CATEGORIES`). Disposition is safe; consider surfacing the signal on the threat axis for consistency. Candidate for Task 1.9/1.12.
- Task 1.6 review: `RESTRICTED` outcomes are asserted as membership in `{REVIEW, RESTRICTED}` rather than pinned exactly; tighten in the Task 1.12 security/regression harness.
- Task 1.5: ASCII typosquat recall is narrow by construction (small brand map + edit-distance-1); the policy must never read a clean typosquat result as proof of legitimacy (already honored by NS-ADR-013). Broaden recall only with measured false-positive data (Phase 3+).
- Task 1.4: `evidence_refs` on auth observations are currently empty; populate when auth evidence feeds a persisted assessment (Task 1.7+).

| NS-ADR-014 | PROPOSED | 2026-11-05 | Scan-not-run surfacing: how attachments reach the assessment when a scanner is absent or fails (the owner may reverse this) | (a) Static attachment inspection (`attachments.py`) always runs on parsed attachment descriptors and its findings flow to the policy like any other finding; policy@3 maps HIGH+ `MALWARE` (executable disguised, scanner detection) and CRITICAL attachment/malware findings to `RESTRICTED`, and MEDIUM+ `ATTACHMENT`/`MALWARE` findings to `REVIEW` (reason codes `MALWARE_FINDING`, `ATTACHMENT_FINDING`). (b) No scanner configured: a message with attachments records the explicit check `ATTACHMENT_SCAN_NOT_RUN` in `completeness.checks_not_run` and is NOT made `INCOMPLETE` by that alone (keeps review load sane; `SAFE_TO_PROCESS` is already unreachable, NS-ADR-013). (c) Scanner configured but it errors, times out, is unavailable, too large or has stale signatures: `ATTACHMENT_SCAN_INCOMPLETE`; the assessment is `INCOMPLETE` (or `REVIEW` when threat findings exist), never clean (I-02). (d) A scanner "clean" is recorded as an INFO finding with engine and signature version as the detector version, evidence only, never a safety claim. (e) Scanner configuration is environment-only: `NERVE_CLAMD_SOCKET` (absolute unix socket path) or `NERVE_CLAMD_HOST` (loopback only) plus optional `NERVE_CLAMD_PORT`, validated by `ClamdConfig`. Absent config means no scanner (nothing opens a socket by default); invalid config logs a safe code and means no scanner (never crashes startup, never falls back to a remote endpoint). The parser retains decoded attachment bytes (bounded by its per-part cap, never persisted/logged/repr'd, cleared after the scan) only when a scanner is configured, so without a scanner payload-level indicators (macro, zip bomb, PDF active content) are not evaluated and only descriptor-level findings apply. | Alternative: treat "no scanner" as `INCOMPLETE` for every message with attachments (stricter, larger review load). Rollback: make `collect_attachment_results` return `ATTACHMENT_SCAN_NOT_RUN` in `incomplete_codes` instead of `checks_not_run`; no data migration. |

## Phase 2 and G2 gap-closure decisions (recorded 2026-11-05)

NS-ADR-014 (scan-not-run surfacing) is recorded in the Phase 1 table above by the wiring work; its summary is repeated here for convenience. All entries below are PROPOSED unless marked; none has been reviewed by the owner except where stated.

| ID | Status | Decision | Evidence / rationale | Reversal |
| --- | --- | --- | --- | --- |
| NS-ADR-014 | PROPOSED | Attachments reach the assessment through static inspection always. No scanner configured means an explicit `ATTACHMENT_SCAN_NOT_RUN` not-evaluated note that does NOT force INCOMPLETE. A configured scanner that errors, times out, is unavailable, too large or stale means `ATTACHMENT_SCAN_INCOMPLETE` and INCOMPLETE/REVIEW, never clean. | Keeps review load sane while SAFE_TO_PROCESS is unreachable (NS-ADR-013); preserves I-02 where a scan was actually attempted. Commit 54cb61f. | Make not-run force INCOMPLETE in `attachment_wiring.py` and policy; bump policy version. |
| NS-ADR-018 | PROPOSED | Egress is a policy engine plus client around an injected transport and resolver. NO real network transport is shipped until a feed or lookup is chosen. Default deny, consent-gated for mail-derived URLs, URLs minimized, resolve twice per hop and pin the validated IP. | Commit bb8d6f9. Orchestrator probed ~35 address forms and URL, rebinding and redirect attacks; none passed. All guarantees depend on a future transport that connects only to `pinned_ip`, ignores proxy env and does not follow redirects itself. | Add a transport only after a deployment-time review. |
| NS-ADR-019 | PROPOSED | Feed update guard counts additions, removals and early expiries against a configurable fraction; an initial load into an empty live set requires an explicit opaque approval reference; one injected clock. | Commit a1e0621. Closes the G2 note that mass removal was unguarded. | Relax thresholds in feed config. |
| NS-ADR-020 | PROPOSED | ClamAV is the first attachment scanner, via a clamd client restricted to a unix socket or loopback literal (`localhost` maps to 127.0.0.1, no DNS). A clean verdict from an engine with stale or unparseable signatures (default max age 3 days) is not clean. | Commit b261991. Tested against a fake clamd AND (2026-11-06) against a real clamd 1.5.4 over loopback TCP in a throwaway local container (see evidence/phase2-clamav-real-daemon-verification.md: PING, VERSION parsing, EICAR detection, clean verdict with versions, staleness, concurrency, oversize, closed port). Still NOT VERIFIED: unix-socket path on a real daemon, in-cluster deployment, clamd-side stream limits. No deployment manifest exists; clamd must run unprivileged with no egress and signatures need a separate updater. | Remove the scanner config; static checks still run. |
| NS-ADR-021 | PROPOSED | Alerts are a pure `evaluate_alerts` function plus Prometheus gauges. No delivery mechanism exists in the repo and none was invented. | Commit 7e35136. | Add delivery once the owner picks a channel. |
| NS-ADR-022 | PROPOSED | The pipeline runner makes one deliberate internal cross-scope read (`list_scopes_with_runnable_jobs`, scope strings only) to find work; every worker is then bound to exactly one scope. The method is not exposed through any API route or observability path. | Commit a9ab805; verified by grep that API and observability do not call it. | Replace with an explicit scope registry if one is built. |
| NS-ADR-023 | ACCEPTED (engineering fact) | Verdict persistence and the job transition run in one transaction behind the `(lease_owner, attempts)` fence; a stale worker gets `STALE_FENCE` and writes nothing. | Commit dca6c79. Claim and fence verified on PostgreSQL 16.15 (7 concurrency tests, reproduced by the orchestrator in a throwaway local container, 0 tables left behind). Single conditional UPDATE, correct under READ COMMITTED. Not tested on other PG versions, replicas or pgbouncer. | n/a |

Policy version history: `mail-security-policy@1` (Task 1.6) -> `@3` (live intel match floors REVIEW; HIGH/CRITICAL malware finding is RESTRICTED). Policy changes only raise dispositions; absence of a match or finding never lowers or clears one.

**OPEN owner decision (not yet an ADR): authorization for the read-only assessments API.** `connections` has no owner column, no endpoint checks roles, and Authentik-issued tokens cannot carry an `account_scopes` claim, so nothing mints the claim and the API returns nothing. Options: (1) single-tenant, any authenticated human access token, scopes resolved server-side per request, device tokens excluded; (2) an explicit role such as `security-reviewer`; (3) leave closed. Auth was deliberately not changed.

**Known unresolved gaps carried forward:** raw RFC822 capture (NS-ADR-002) is blocked on an object-store encryption decision; no real egress transport; no alert delivery; no real clamd or feed exercised; `deployments/` not reviewed.
## Evidence-fidelity rule (recorded after live cluster verification)
| ID | Status | Decision | Evidence / rationale | Reversal |
| --- | --- | --- | --- | --- |
| NS-ADR-024 | ACCEPTED (engineering fact) | Evidence fidelity is derived ONLY from the trusted acquisition mode recorded by the ingest seam in the job meta (`acquisition_mode`, design enum names), never from the stored content. Today both seam sites (`MailIngestWorker._schedule_security_job`, `communications._schedule_security_job`) record `LEGACY_BODY_ONLY`, which maps to `BODY_ONLY`. A missing, unknown, mistyped or non-string mode fails safe to `LEGACY_BODY_ONLY`/`BODY_ONLY`. Raw-bytes fidelities (`ORIGINAL_RFC822`, `PROVIDER_FULL`) are intentionally UNREACHABLE from stored body text today: `acquisition.RAW_CAPTURE_SEAM_ENABLED` is `False` and both the seam (on write) and the worker (on read) demote any raw mode to `LEGACY_BODY_ONLY` until a future trusted raw-capture seam (NS-ADR-002) flips it with its own tests. This is like NS-ADR-013's unreachable `SAFE_TO_PROCESS`, not dead code. For body-only the worker also never feeds header-looking lines in the body into authentication evidence (all axes stay `UNKNOWN`, never `INDEPENDENT`), and the evidence manifest records `missing_parts` for headers, MIME structure, attachments, authentication evidence and envelope (R-002.3). The parser still bounds and parses any bytes it is given but no longer labels its input: `evidence_fidelity_for(evidence, acquired=BODY_ONLY)` takes the trusted fidelity from the caller and may only DEMOTE (source-bytes truncation). | Live defect: 5 real stored Gmail objects were headerless decoded UTF-8 text yet all 5 assessments said `ORIGINAL_RFC822` (the parser labelled any complete parse original). Phase 0 (`evidence/phase0-source-fidelity.md`) proved the Gmail path stores decoded text parts and the IMAP path discards RFC822. Satisfies R-002.1-.3, I-02, I-05, T-28. Tests: `tests/backend/test_mail_security_fidelity.py`. Limits: existing jobs have no recorded mode and fail safe to body-only; previously persisted `ORIGINAL_RFC822` rows are not rewritten by this change. Open follow-up: the ingest worker still stores decoded text under object content_type `message/rfc822` (not changed here). | Revert the commit; no migration (mode lives in the existing job `meta` JSON and manifest columns). |

## API authorization scope mode (recorded after owner direction, option 1 with secure default)

| ID | Status | Decision | Evidence / rationale | Reversal |
| --- | --- | --- | --- | --- |
| NS-ADR-025 | PROPOSED | Opt-in `NERVE_MAIL_SECURITY_API_SCOPE_MODE` for the read-only assessments API. Values `claim_only` and `single_tenant`; the code default (unset, empty, unknown or garbled; only exactly `single_tenant` after trim/lowercase enables it) is `claim_only`, identical to the previous behavior. In `single_tenant` the effective scopes are `conn:<id>` for every ENABLED connection whose kind is a mail kind the ingest supervisor serves (`gmail`, `imap`; same definition as `_KIND_TO_CURSOR`), resolved per request by an ids-only query (no config, label or secret column), no caching. A token `account_scopes` claim, if present, is INTERSECTED (it can only narrow). Excluded from server-resolved scopes (claim-only semantics apply): device principals (`typ` device, `sub` starting `device:`, role `device`) and any token without a non-empty `sub`. `src:` scopes are client-influenced and are NEVER server-resolved; they remain reachable only through an explicit claim in `claim_only` mode (and are not reachable in `single_tenant` unless also resolved, which they are not). Lookup failure or absent durable plane returns no scopes and logs only a safe code. 404-indistinguishability, allow-list redaction, safe errors, GET-only routes, MAX_PAGE_SIZE and the `/health` no-scope 404 are unchanged. | Tests: `tests/backend/test_mail_security_api_scope_mode.py`. Limit: the platform has no per-user ownership model, so in `single_tenant` every authenticated human sees metadata-only assessments for every enabled mail connection. No auth issuance, token minting or SSO code changed. Does not close the owner question of per-user ownership; this is a deployment-level single-tenant assertion. | Set `NERVE_MAIL_SECURITY_API_SCOPE_MODE=claim_only` (or remove it) and restart the api. |
| NS-ADR-026 | PROPOSED | First-party precision for the ASCII typosquat and subdomain-deception rules. A host is first-party only when its registrable domain EXACTLY equals the brand's canonical domain or an entry in `_FIRST_PARTY_REGISTRABLE_DOMAINS` (Apple: icloud.com, cdn-apple.com, mzstatic.com), or its public suffix is a brand-owned TLD in `_BRAND_OWNED_TLDS` (apple, google, microsoft, amazon). Both tables are owner-reviewable DATA, not logic. First-party hosts are exempt; a non-first-party host embedding a first-party domain at a label boundary (`cdn-apple.com.evil.test`, `cdn-apple.com-login.net`) is flagged. `INSPECTIONS_VERSION` bumped to `offline-inspections@2`; `analysis_profile_version` NOT bumped. | Real Apple Card mail linking `applecard.cdn-apple.com` got a MEDIUM ASCII_BRAND_TYPOSQUAT and REVIEW. Precision evidence is anecdotal (1 false positive in 71 messages in one mailbox), NOT a measured rate. The allowlist needs periodic owner review; wrongly listing a hostable domain would create a blind spot. Existing verdicts are not re-assessed (open follow-up). | Empty the two tables to restore the previous behavior; revert the version bump. |
