# Phase 0.6 — Integration map, schema/migration strategy, smallest-safe Phase 1 slice

**Task:** 0.6 Integration map, schema/migration strategy, threat-model update, ADR recommendations; smallest safe Phase 1 patchset + ownership. **Req:** R-001, R-025. **Depends:** 0.2–0.5.
**Legend:** `[VERIFIED]`; `[INFERRED]`. Plan items implement NOTHING here (audit only).

## Integration map (reuse existing, do not reinvent)

| NerveShield need | Existing asset to reuse `[VERIFIED]` | Integration risk |
| --- | --- | --- |
| Durable job/outbox | `backend/persistence/repositories/import_repo.py` (cursors, import_jobs), event bus `backend/events/*` (NATS/JetStream, DLQ) | add table/subjects; don't touch cursor commit-gate |
| Object store for raw evidence | `backend/storage/{contracts,s3,memory}.py` | no encryption today (0.4) — gate P0 claims |
| Content resolution | `DetectionService.register_content` / `_InMemoryResolver` | keep transient; add a **separate** typed evidence resolver (design §1) — do NOT widen generic content access |
| Sensitivity→routing | `backend/policy/sensitivity.py` (PERSONAL→confidential→LOCAL_ONLY) | reuse as the local-only signal |
| Policy authority | `backend/policy/pdp.py` (fail-closed, namespace isolation, `INFER_ROUTE`, `verify_approval_binding`) | reuse; add fail-closed route resolver (4.2) |
| Inference transport | `backend/inference/omniroute.py` | no local enforcement — add resolver before use |
| Attention promotion seam | `CommunicationPipeline.ingest` (`wiring.py`) | **the bypass (0.3)** — gate must precede, Phase 7, OFF flag |
| Account scoping precedent | `calendar_events.account_id` + index (`personal.py`) | model new tables on this |
| Migrations | Alembic `001..015`, forward-numbered | add `016_*` forward-only |
| UI seam | `web/src/views/EmailView.tsx` (307 LOC, `emailApi`), `CommunicationsView.tsx` | add read-only safety card later (1.11), no remote fetch |
| Feature flag | `NERVE_INGEST_ENABLED` convention (`ingest_enabled`) | add `NERVE_MAIL_SECURITY_ENABLED=0` default-off, SHADOW |

## Schema / migration strategy

- **Phase 1 minimum tables only** (design §9): `mail_evidence` (manifest + fidelity + hashes; raw stays in object store), `mail_security_jobs` (account-scoped, state/retry/lease/outbox-key unique on `(account_scope, source_ref, analysis_profile_version)`), `mail_security_assessments` + `mail_security_findings` (metadata/opaque-ref only). Defer intel/datasets/model/actions tables to later phases.
- **Account scoping:** add explicit `account_scope` columns (model on `calendar_events.account_id` precedent) — closes the R-023/T-15 gap that `communications` lacks. `[VERIFIED gap from 0.4]`
- **Forward-only** alembic `016_*` with documented downgrade/read-only compatibility (design §14).
- **Schemas authoritative:** `schemas/security-assessment.schema.json` (SHADOW ⇒ `action_capabilities:[]`; INCOMPLETE/ERROR/DEAD_LETTER ⇒ disposition∈{INCOMPLETE,REVIEW,RESTRICTED}) and `schemas/semantic-observation.schema.json` (`additionalProperties:false`, bounded lengths). Phase 1.1 mirrors these as strict Pydantic (`extra="forbid"`, capped strings).

## Threat-model update (deltas proven at HEAD)

- **T-26 (untrusted mail → unsafe workflow):** CONFIRMED live on both ingest paths via `CommunicationPipeline` attention mint (0.3). Mitigation is the Phase 7 pre-promotion gate behind an OFF flag; Phase 1 stays shadow and must not alter the path.
- **T-28 / T-01 (historic auth):** CONFIRMED — no auth evidence captured at all (0.2); any auth axis must be `UNKNOWN`.
- **T-13 (cloud fallback):** CONFIRMED exposure — transport/adapter have no local-only enforcement (0.5).
- **T-14/R-022.4 (body text in surfaces):** PARTIAL — durable rows are templated-safe, but in-memory `AttentionItem.reason` and §8 event `matched` carry quoted body fragments (0.3/0.4). Watch when the security card/events land.
- **T-15/R-023 (cross-account):** CONFIRMED gap — `communications` has no `account_id`; person id is shared per handle across accounts (0.4).
- **Object-store encryption (R-017.4):** CONFIRMED unverifiable — no SSE/crypto in code (0.4).

## Smallest safe Phase 1 vertical slice (tasks 1.1–1.3 territory) — RECOMMENDATION, NOT IMPLEMENTED

**Recommended first slice: Task 1.1 — versioned Pydantic evidence/finding/assessment models + strict (de)serialization tests.** Reasoning:
- **Zero runtime wiring, zero migration, zero network, pure in-process** → cannot touch cursor/attention/ingest behavior (preserves I-07, I-11, R-001). Lowest blast radius.
- It is the dependency root: 1.2 (identifiers) and 1.3 (MIME parser) both depend on 1.1; starting elsewhere forces rework.
- Directly testable tests-first against the two JSON schemas already in the spec (invalid-input rejection, `extra=forbid`, length caps, SHADOW ⇒ empty capabilities, INCOMPLETE-disposition constraint).
- Files (new, isolated): e.g. `backend/mail_security/models.py` + `tests/backend/test_mail_security_models.py`. No existing file edited.

1.2 (deterministic ids/revision, cross-account isolation) is the natural second step; 1.3 (bounded offline MIME parser) third. 1.7+ (durable tables) must wait until the schema is locked and the 0.4 encryption gap is addressed.

## File-level Phase 1.1 plan (for the owner — DO NOT IMPLEMENT until G0 approved)

1. `backend/mail_security/__init__.py`, `backend/mail_security/models.py` — `MessageEvidenceManifest`, `SecurityFinding`, `SecurityAssessment`, auth/axis/score/model-run submodels; `model_config = {"extra":"forbid"}`; capped `max_length` on every user-controlled string; enums mirroring the JSON schemas.
2. `tests/backend/test_mail_security_models.py` — round-trip + invalid-input + schema-constraint tests (SHADOW empty-capabilities, INCOMPLETE disposition set, forbid unknown keys, reject over-length).
3. Verify: `uv run pytest tests/backend/test_mail_security_models.py -q` green; `uv run ruff check backend/mail_security` clean; `uv run pyright backend/mail_security`.

## Explicit unknowns carried into Phase 1

- Object-store SSE / DB / backup encryption (0.4) — `UNKNOWN` until `deployments/` reviewed.
- Granite/Qwen deployment + `hybrid/*` resolution (0.5) — `UNKNOWN`; blocks Phases 3–4, not Phase 1.
- Whether an optional raw-RFC822 capture seam is wanted now (0.2) — owner decision for NS-ADR-002.

## Rollback & gate risks

- **Rollback:** Phase 1.1 is pure additive code — rollback = delete the new module/tests. No data/migration to reverse.
- **Gate risk:** keep `NERVE_MAIL_SECURITY_ENABLED=0` default; never wire the pipeline gate in Phase 1; keep the 1850-green regression suite green (run it after any change). Must-fail guards (tasks.md "Never mark complete when…") apply.

## Ownership boundaries

- Evidence/analysis plane (parser, findings, assessment models): new `backend/mail_security/*` — owns reading content under policy, emits observations only.
- Decision/action plane: existing `backend/policy/pdp.py` remains sole authority; NerveShield never mints capabilities.
- UI: `web/src/views/*` read-only safety card later; no remote resource fetch.

## Done (0.6): integration map, schema/migration strategy, threat-model update, smallest-safe slice with file-level plan, unknowns, rollback & gate risks. ✅ ADR recommendations appended in `decisions.md`.
