# Versioned strict data-contract models for NerveShield mail security (Task 1.1)

Task 1.1 adds an isolated `backend/mail_security` package with versioned Pydantic models that mirror the two authoritative JSON schemas — `security-assessment.schema.json` and `semantic-observation.schema.json` — plus a `MessageEvidenceManifest` for the design §4.1 ingest envelope, and a tests-first suite. The change is additive and self-contained: three new files, no existing production file edited, no import wiring, routes, migrations, event subjects, network, or I/O, and a default-off `NERVE_MAIL_SECURITY_ENABLED = 0` flag that no runtime path reads. Every model forbids unexpected fields, caps every user-controlled string, mirrors the schema enums exactly, and encodes the two schema invariants (SHADOW ⇒ empty capabilities; non-clean processing state ⇒ restricted disposition) as validators. There are no raw body/subject/address free-text fields and no executable/instruction/tool fields anywhere.

Watch for: a strictness gap on `AxisScore` where the schema marks `value`/`calibration_id`/`evidence_refs` as *required* keys but the model supplies defaults, so a payload omitting them is accepted (confirmed, permissive-direction only). No security-direction divergences found.

**Verdict**: APPROVED

## High-level view

The scope contract was respected exactly. The commit (`7a0347e`) adds only `backend/mail_security/__init__.py`, `backend/mail_security/models.py`, and `tests/backend/test_mail_security_models.py`. A repo-wide grep finds no consumer of the feature flag and no import of the package outside itself and its test, so nothing is wired into the running app. This satisfies the "models only, no activation" phase gate (R-025.3/.5) and the default-SHADOW steering contract.

The data-contract strictness is sound. A private `_StrictModel` base sets `extra="forbid"` and `str_strip_whitespace=True`, and every model derives from it, so unknown keys are rejected top-level and in nested submodels. User-controlled strings are either bounded `StringConstraints` or the pattern-locked `OpaqueRef`/`Code` types that structurally cannot carry raw email text, addresses, or whitespace (I-05/R-022.4). No field accepts raw subject/body/addresses, and there is no executable, instruction, or tool field in any contract (I-01).

The encoded invariants match the schema `allOf` blocks and the stricter steering intent. `PolicyDisposition` rejects any `action_capabilities` in SHADOW mode (I-03/schema allOf #1). `SecurityAssessment` rejects a `SAFE_TO_PROCESS`/`LOW_PRIORITY` disposition whenever the processing state is INCOMPLETE/ERROR_RETRYABLE/DEAD_LETTER, restricting it to {INCOMPLETE, REVIEW, RESTRICTED} (I-02/schema allOf #2). `AxisScore` enforces the NOT_EVALUATED ⇒ null-value/null-calibration and CALIBRATED_PROBABILITY ⇒ value+calibration_id consistency rules.

The abstention posture (R-007) is faithful: all six trust axes are independent `AxisScore` fields with no summarizing composite, every axis supports `NOT_EVALUATED`, and each authentication axis plus component risk level supports `UNKNOWN`. Model output is representable only as `SemanticObservation`/`ModelRun` metadata (I-08) with a bounded 600-char `gist` and opaque refs, never a decision or capability grant.

The one divergence is a looseness, not a risk: the schema `score` object lists `value`, `calibration_id`, and `evidence_refs` as required keys, but the model defaults them, so a JSON payload omitting those keys validates against the model though it would fail the JSON schema. This weakens exact schema round-trip fidelity in the permissive direction; it does not let any unsafe value through.

Test coverage hits every behavior the task enumerates: round-trip serialization for all top-level models, `extra=forbid` rejection (top-level and nested), over-length rejection across several string fields, opaque-ref/code pattern rejection, enum exactness, SHADOW ⇒ empty-capabilities (both directions), non-clean-state ⇒ restricted-disposition (parametrized across all three states and both lenient dispositions), and NOT_EVALUATED/UNKNOWN per axis. The recorded verification in the commit message shows 53 new tests passing, the full non-integration suite at 1903 passed / 0 failed, and clean ruff + pyright.

<details>
<summary>Issues (1)</summary>

1. **AxisScore required-key looseness** — the schema `score` marks `value`/`calibration_id`/`evidence_refs` as required, but the model defaults them, so a payload omitting those keys is accepted by the model yet would be rejected by the JSON schema. Permissive-direction only (no unsafe value admitted); consider adding `Field(...)` or a presence check if exact schema round-trip fidelity is desired in a later task. Non-blocking.

</details>

<details>
<summary>Details</summary>

### Scope was respected: three new files, nothing wired

`git show --name-status HEAD` lists exactly `A backend/mail_security/__init__.py`, `A backend/mail_security/models.py`, `A tests/backend/test_mail_security_models.py`. Grepping the whole tree for `NERVE_MAIL_SECURITY_ENABLED` and for `mail_security` outside the package and its test returns nothing, confirming the flag is defined but unread and the package is imported nowhere else (confirmed). There are no migrations, route registrations, event subjects, or network/I/O calls in either source file — the models are pure Pydantic declarations plus validators. This is the correct "define contracts, do not activate" posture for the phase gate (R-025.3/.5).

### Strictness is structural, not incidental

Every model inherits `_StrictModel`, whose `model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)` rejects unknown keys and trims incidental whitespace on bounded strings (R-025). The two reusable string types do the heavy lifting for I-05/R-022.4: `OpaqueRef` is `min_length=1, max_length=200, pattern=^[A-Za-z0-9:_-]+$` and `Code` is `min_length=1, max_length=80, pattern=^[A-Z][A-Z0-9_.-]*$`. Both patterns exclude spaces, `@`, and arbitrary Unicode, so by construction an opaque ref or a reason/indicator code cannot smuggle a raw address or body fragment. Free-text-ish fields are explicitly bounded instead: `gist` is capped at 600, `rule_id`/`detector_version` at 100, `route`/`model_id`/`runtime`/`privacy_policy_version` at their schema maxima. The `sha256` field is additionally shape-checked to 64 hex characters. Collection fields carry `max_length` matching each schema `maxItems`, and a shared `_require_unique` helper enforces `uniqueItems: true`.

There is no field anywhere that holds a raw subject, body, email address, header value as free text, or any executable/instruction/tool payload — the manifest and assessment carry metadata and opaque content refs only (I-01, I-05, design §9).

### Enums mirror the schemas exactly

Each `StrEnum` reproduces the schema enum value set verbatim. The test suite pins three of the larger sets (`FindingCategory`, `Disposition`, `ProcessingState`, plus `AuthResult`/`AuthProvenance`) with exact set-equality assertions, which will fail loudly if a value drifts from the schema. Cross-reading the enums against both `.schema.json` files shows no missing or extra members on any of the shared enums.

### Encoded invariants match the schema allOf blocks

Two schema `allOf` conditionals and the stricter steering intent are enforced as validators rather than left implicit:

```
SHADOW mode            -> action_capabilities must be empty   (PolicyDisposition._shadow_has_no_capabilities)
INCOMPLETE/ERROR/DEAD  -> disposition in {INCOMPLETE, REVIEW, RESTRICTED}
                          never SAFE_TO_PROCESS/LOW_PRIORITY  (SecurityAssessment._non_clean_state_restricts_disposition)
```

The I-02 validator checks membership in a `_RESTRICTED_DISPOSITIONS` frozenset, so both lenient dispositions (`SAFE_TO_PROCESS` and `LOW_PRIORITY`) are blocked for non-clean states — slightly stricter than the schema allOf, which only enumerates the allowed set but reaches the same outcome. `AxisScore._semantics_consistency` enforces the score allOf: NOT_EVALUATED forbids a non-null value or calibration id, and CALIBRATED_PROBABILITY requires both a numeric value and a non-empty calibration id. These are the right places to encode I-02 and the calibration contract, since they are the model-level invariants a caller cannot bypass.

### Abstention and the six independent axes (R-007)

`TrustAxes` declares the six axes as separate `AxisScore` fields with no composite/summary score, matching design §3's "a single score never summarizes trust." Every axis supports `NOT_EVALUATED` (verified by `test_every_trust_axis_supports_not_evaluated`), each authentication axis supports `UNKNOWN` result and `UNKNOWN` provenance (R-002.4/.5, R-004.5), and `ComponentRiskLevel` carries both `UNKNOWN` and `NOT_EVALUATED`. The `AuthProvenance` enum keeps `INDEPENDENT`/`PROVIDER_REPORTED`/`UNTRUSTED_HEADER`/`UNKNOWN` distinct, so a forged `Authentication-Results` header is representable as `UNTRUSTED_HEADER` rather than a verified pass (R-002.4, threat-model forged-header case).

### Model output is observation-only (I-08)

`SemanticObservation` and `ModelRun` are the only places model activity surfaces. `ModelRun` records resolved route/model/runtime/status/privacy-policy-version and latency as metadata — no output text, no capability. `SemanticObservation` carries a bounded gist and opaque refs, and its request/indicator/contradiction submodels are typed observations with `requires_independent_verification`/`high_impact` booleans, never a grant. `model_confidence` is explicitly documented and typed as an uncalibrated 0–1 estimate, consistent with R-007.2 and the steering note that LLM probabilities are unverified until calibrated.

### AxisScore required-key looseness (the one divergence)

The schema `score` object marks `value`, `semantics`, `calibration_id`, and `evidence_refs` as `required` (present, though `value` and `calibration_id` are nullable). The model declares `value: ... | None = None`, `calibration_id: ... | None = None`, and `evidence_refs` with a default factory, so a payload that omits those keys entirely validates against the model even though the JSON schema would reject it for missing required keys (confirmed by reading the field declarations against the schema `required` arrays). This only loosens input acceptance — the semantics/value consistency validator still runs, and no unsafe value is admitted — so it is not a security-direction problem, but it does mean the model is not a strict superset-reject of the JSON schema for the score object. If exact schema round-trip parity is a later requirement, switch those to `Field(...)` or add a presence check. Non-blocking for Task 1.1.

### Test coverage

The suite covers each behavior the task calls for and a few beyond it: round-trip for `SecurityAssessment`, `SemanticObservation`, `MessageEvidenceManifest`, `SecurityFinding`, `ObservationRequest`, `RiskIndicator`, `Contradiction`; `extra=forbid` rejection both at top level (parametrized over four models) and on nested submodels (`policy`, `axes.threat_likelihood`); over-length rejection on `gist`, `analysis_profile_version`, `policy_version`, `rule_id`, `OpaqueRef`, and `ModelRun.route`; pattern rejection proving an opaque ref refuses `attacker@example.com has spaces` and a code refuses lowercase; enum exactness; SHADOW ⇒ empty capabilities in both directions; non-clean-state ⇒ restricted disposition parametrized across all three states against both the restricted set (accept) and both lenient dispositions (reject); and NOT_EVALUATED/UNKNOWN across every trust, auth, and component axis plus value-range enforcement.

Not tested: the schema-`required`-key looseness above is not exercised (no test asserts that omitting `value`/`calibration_id` is or isn't accepted), which is why the divergence is easy to miss. Also untested — and reasonably out of scope for a models-only task — is JSON-schema-level conformance (validating a dumped model against the actual `.schema.json` with a JSON Schema validator), which would be the surest guard against future model/schema drift. Worth a follow-up task.

### Recorded verification (not re-run)

The commit message records four commands and their observed output: `pytest tests/backend/test_mail_security_models.py -q` ⇒ 53 passed; `pytest tests/ -m "not integration" -q` ⇒ 1903 passed, 3 warnings (1850 baseline + 53 new, 0 failed); `ruff check backend/mail_security` ⇒ all checks passed; `pyright backend/mail_security` ⇒ 0 errors. Per the review instructions these were read, not re-executed.

</details>

<details>
<summary>File map</summary>

- `backend/mail_security/__init__.py` — package surface: re-exports all models/enums, defines the unread default-off `NERVE_MAIL_SECURITY_ENABLED = 0`.
- `backend/mail_security/models.py` — the versioned strict models, enums, bounded string types, and invariant validators mirroring both schemas.
- `tests/backend/test_mail_security_models.py` — tests-first suite (53 tests) covering round-trip, strictness, enums, and the encoded invariants.

Full diff: `git show 7a0347e`.
</details>
