# Phase 0.7 — G0 Gate summary (owner-facing) — STOP, approval requested

**Task:** 0.7 Summarize evidence to owner; request Phase 1 approval; DO NOT begin Phase 1. **Req:** R-025.
**Status: STOPPED AT G0. No production code, migrations, or wiring were created. Awaiting explicit owner approval.**

## One-paragraph verdict

The documented baseline is accurate (HEAD `45ab762…` matches design §1; all 14 referenced paths exist; the 1850-test suite is green). The audit confirms four load-bearing realities the NerveShield design anticipated: (1) the mail transports keep only **decoded body text + a header allowlist** — no RFC822/DKIM/auth evidence is preserved, so every authentication axis must be `UNKNOWN`; (2) **both ingest paths already mint attention/Today items directly from untrusted body text** with no security evaluation (threat T-26 is live) — so the security gate must sit *before* that promotion and belongs to Phase 7 behind an OFF flag, never Phase 1; (3) **object-store encryption is not implemented in code** — Zone-P0 raw-content encryption is unverifiable today; (4) **`hybrid/reader`/`hybrid/code`, Granite, and Qwen do not exist in the code** — they are spec-only, so model identity and local-vs-cloud routing are `UNKNOWN` and the `hybrid/*` alias cannot be assumed local. The existing PDP (`INFER_ROUTE` + namespace isolation + approval binding) and the sensitivity classifier (`PERSONAL→CONFIDENTIAL→LOCAL_ONLY`) are strong, reusable enforcement seams.

## Evidence index

| Task | Deliverable | Key finding |
| --- | --- | --- |
| 0.1 | `phase0-baseline.md` | HEAD matches; **pytest 1850 passed / 0 failed**; ruff 416 pre-existing style errors (not a CI gate); pyright/web build NOT RUN |
| 0.2 | `phase0-source-fidelity.md` | Gmail = decoded text parts (no `format=raw`); IMAP = full RFC822 fetched but discarded; **no DKIM/auth headers captured** → fidelity `BODY_ONLY` |
| 0.3 | `phase0-ingest-attention.md` | **T-26 bypass CONFIRMED on worker + API paths**; cursor advances only post-durable-commit (P0-D); replay idempotent |
| 0.4 | `phase0-storage-privacy.md` | **No object-store encryption in code**; `communications` has **no account_id** (R-023/T-15 gap); transport redaction + boolean flags good |
| 0.5 | `phase0-omniroute-pdp.md` | **Granite/Qwen/hybrid aliases absent from code (UNKNOWN)**; transport has no LOCAL_ONLY enforcement (T-13); PDP/sensitivity reusable |
| 0.6 | `phase0-integration-plan.md` | Integration map; min-schema strategy; **smallest-safe slice = Task 1.1 (pure Pydantic models)**; unknowns + rollback |

## Gate G0 checklist (requirements §"Gate G0")

- [x] Current source + durable data flow mapped (0.3 sequencing, both paths).
- [x] Existing tests run (1850 passed, 0 failed — 0.1).
- [x] Evidence limitations documented (0.2 fidelity matrix; UNKNOWN auth).
- [x] Action/attention bypass identified (0.3 — T-26 live on both paths).
- [x] Storage/privacy guarantees audited; unverifiable ones listed (0.4).
- [x] Inference route/model identity audited; UNKNOWNs recorded (0.5).
- [x] Threat model updated; ADR recommendations recorded (`decisions.md`).
- [x] Smallest safe Phase 1 slice identified, **not implemented** (0.6).

## Recommended smallest safe first step (on approval)

**Task 1.1 — versioned strict Pydantic models for evidence manifest / finding / assessment, tests-first**, in a new isolated `backend/mail_security/` package with `NERVE_MAIL_SECURITY_ENABLED=0` default. Zero wiring, zero migration, zero network; cannot affect ingestion/attention/cursor behavior. Do **not** proceed to durable tables (1.7) until the 0.4 encryption gap and account-scope strategy are accepted.

## Explicit request for approval

**Requesting owner sign-off to begin Phase 1 at Task 1.1 only, under the stated constraints (shadow, flag-off, no migration, additive-only).** Phase 1 will not start, and no further tasks will run, until this approval is recorded. Per steering, phases are not auto-advanced.

## Owner decision (to be completed by owner)

```
G0 decision:  APPROVE Phase 1 (Task 1.1 only)
Reviewer / date: Owner (via orchestrator) / 2026-11-04
Notes / conditions:
  - Scope strictly Task 1.1: strict versioned Pydantic models (evidence
    manifest / finding / assessment) in a new isolated backend/mail_security/
    package, tests-first, NERVE_MAIL_SECURITY_ENABLED=0 default.
  - Additive-only: no runtime wiring, no migration, no network, no change to
    ingestion/attention/cursor behavior. Must keep the 1850-test suite green.
  - Models mirror schemas/security-assessment.schema.json and
    schemas/semantic-observation.schema.json (extra="forbid", capped string
    lengths, SHADOW => empty capabilities, INCOMPLETE/ERROR/DEAD_LETTER =>
    restricted disposition set).
  - Optional raw-RFC822 capture seam (NS-ADR-002) NOT in scope for this slice.
  - STOP after Task 1.1 for a phase checkpoint; do not auto-advance to 1.2+.
```
