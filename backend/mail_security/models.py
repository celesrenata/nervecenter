"""Strict data-contract models for NerveShield (Phase 1, Task 1.1).

These are the versioned Pydantic models that mirror the two authoritative
JSON schemas in the NerveShield spec:

- ``schemas/security-assessment.schema.json`` → :class:`SecurityAssessment`
  (plus :class:`Completeness`, :class:`Authentication`,
  :class:`AuthObservation`, :class:`SecurityFinding`, :class:`AxisScore`,
  :class:`TrustAxes`, :class:`ModelRun`, :class:`PolicyDisposition`).
- ``schemas/semantic-observation.schema.json`` → :class:`SemanticObservation`
  (plus :class:`ClaimedIdentity`, :class:`Component`,
  :class:`ObservationRequest`, :class:`RiskIndicator`, :class:`Contradiction`).

A :class:`MessageEvidenceManifest` captures the design §4.1 immutable ingest
envelope (metadata + opaque content refs only).

Design/requirement anchors:

- **R-002/R-005/R-007/R-025** — evidence/provenance, deterministic findings,
  the six independent trust axes, and strict serialization + invalid-input
  proof.
- **Design §3** — the six trust axes are *independent*; every axis supports
  ``NOT_EVALUATED``/``UNKNOWN``. A single score never summarizes trust.
- **Design §9** — persisted records are metadata + opaque evidence refs only.
- **I-01** — no executable/instruction/tool fields exist in any accepted
  contract; email content is data, never authority.
- **I-02** — absent/unscanned/failed is never clean: INCOMPLETE/ERROR/
  DEAD_LETTER processing states can never carry a ``SAFE_TO_PROCESS`` (or
  other lenient) disposition.
- **I-05/R-022.4** — no field carries raw body/subject/addresses as free
  unbounded text; opaque refs and codes are pattern-bounded, every
  user-controlled string is length-capped, and ``extra="forbid"`` rejects
  unexpected fields everywhere.
- **I-08** — model outputs are observations only (:class:`SemanticObservation`
  and :class:`ModelRun` metadata), never a decision or capability grant.

Follows existing repo model conventions (``StrEnum`` enums, ``BaseModel``,
``Field`` + ``field_validator``/``model_validator``; see
``backend/communications/models.py``). The one deliberate divergence required
by R-025/the schemas is ``model_config = ConfigDict(extra="forbid")`` on every
model so unexpected fields are rejected.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# -- schema constant -----------------------------------------------------------

#: Version both schemas pin (``"schema_version": {"const": "1.0"}``).
SCHEMA_VERSION = "1.0"


# -- bounded primitive string types (mirror the JSON-schema $defs) -------------

#: ``opaqueRef`` — minLength 1, maxLength 200, pattern ``^[A-Za-z0-9:_-]+$``.
#: Opaque evidence/source reference. By construction it cannot carry raw email
#: text, addresses or whitespace (I-05/R-022.4).
OpaqueRef = Annotated[
    str,
    StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9:_-]+$"),
]

#: ``code`` — minLength 1, maxLength 80, pattern ``^[A-Z][A-Z0-9_.-]*$``.
#: A safe, upper-snake reason/indicator/explanation code — never free text.
Code = Annotated[
    str,
    StringConstraints(min_length=1, max_length=80, pattern=r"^[A-Z][A-Z0-9_.-]*$"),
]

_SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")


class _StrictModel(BaseModel):
    """Base for every NerveShield contract model.

    ``extra="forbid"`` rejects unexpected fields (R-025); ``str_strip_whitespace``
    normalizes incidental surrounding whitespace on bounded strings.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# =============================================================================
# Shared enums (mirror schema enum values exactly)
# =============================================================================


class ScoreSemantics(StrEnum):
    """What a trust-axis ``value`` means (security-assessment ``score``)."""

    NOT_EVALUATED = "NOT_EVALUATED"
    RAW_SCORE = "RAW_SCORE"
    CALIBRATED_PROBABILITY = "CALIBRATED_PROBABILITY"
    POLICY_NORMALIZED = "POLICY_NORMALIZED"


class AuthResult(StrEnum):
    """SPF/DKIM/DMARC/ARC observation result."""

    PASS = "PASS"
    FAIL = "FAIL"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class AuthProvenance(StrEnum):
    """Where an authentication claim actually came from (design §3/§5.1)."""

    INDEPENDENT = "INDEPENDENT"
    PROVIDER_REPORTED = "PROVIDER_REPORTED"
    UNTRUSTED_HEADER = "UNTRUSTED_HEADER"
    UNKNOWN = "UNKNOWN"


class FindingCategory(StrEnum):
    """Typed deterministic finding category (R-005)."""

    AUTH_PROVENANCE = "AUTH_PROVENANCE"
    SENDER_IMPERSONATION = "SENDER_IMPERSONATION"
    URL_DECEPTION = "URL_DECEPTION"
    TRACKING = "TRACKING"
    HTML_RISK = "HTML_RISK"
    ATTACHMENT = "ATTACHMENT"
    MALWARE = "MALWARE"
    PHISHING = "PHISHING"
    PAYMENT_FRAUD = "PAYMENT_FRAUD"
    SOCIAL_ENGINEERING = "SOCIAL_ENGINEERING"
    PROMPT_INJECTION = "PROMPT_INJECTION"
    INTEL_MATCH = "INTEL_MATCH"
    SCANNER_FAILURE = "SCANNER_FAILURE"
    OTHER = "OTHER"


class FindingSeverity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class FindingStatus(StrEnum):
    OBSERVED = "OBSERVED"
    SUSPECTED = "SUSPECTED"
    NOT_EVALUATED = "NOT_EVALUATED"
    INCOMPLETE = "INCOMPLETE"


class ProcessingState(StrEnum):
    """Assessment state machine (design §3 state machine v1).

    Distinct from :class:`Disposition`: a partial assessment can still yield a
    restrictive disposition, but INCOMPLETE/ERROR/DEAD_LETTER states can never
    be clean (I-02).
    """

    DISCOVERED = "DISCOVERED"
    EVIDENCE_READY = "EVIDENCE_READY"
    DETERMINISTIC_DONE = "DETERMINISTIC_DONE"
    MODEL_PENDING = "MODEL_PENDING"
    ASSESSED = "ASSESSED"
    REVIEW = "REVIEW"
    SAFE_TO_PROCESS = "SAFE_TO_PROCESS"
    INCOMPLETE = "INCOMPLETE"
    ERROR_RETRYABLE = "ERROR_RETRYABLE"
    DEAD_LETTER = "DEAD_LETTER"


class EvidenceFidelity(StrEnum):
    """How faithful the captured evidence is to the original (design §4)."""

    ORIGINAL_RFC822 = "ORIGINAL_RFC822"
    PROVIDER_FULL = "PROVIDER_FULL"
    SELECTED_HEADERS_AND_BODY = "SELECTED_HEADERS_AND_BODY"
    BODY_ONLY = "BODY_ONLY"
    METADATA_ONLY = "METADATA_ONLY"
    UNKNOWN = "UNKNOWN"


class AssessmentMode(StrEnum):
    """Deployment mode (not a state). Default is SHADOW (design §3/§14)."""

    SHADOW = "SHADOW"
    NOTIFY = "NOTIFY"
    REVIEW_GATED = "REVIEW_GATED"
    ENFORCE = "ENFORCE"


class Disposition(StrEnum):
    """Policy disposition — the restrictive verdict, never a capability."""

    SAFE_TO_PROCESS = "SAFE_TO_PROCESS"
    LOW_PRIORITY = "LOW_PRIORITY"
    REVIEW = "REVIEW"
    RESTRICTED = "RESTRICTED"
    INCOMPLETE = "INCOMPLETE"


class ModelRunStatus(StrEnum):
    COMPLETED = "COMPLETED"
    DENIED_BY_POLICY = "DENIED_BY_POLICY"
    TIMEOUT = "TIMEOUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_RUN = "NOT_RUN"


class AcquisitionMode(StrEnum):
    """How message evidence was acquired (design §4.1)."""

    GMAIL_FULL = "GMAIL_FULL"
    GMAIL_RAW = "GMAIL_RAW"
    IMAP_RFC822 = "IMAP_RFC822"
    TAKEOUT_MBOX = "TAKEOUT_MBOX"
    EML = "EML"
    LEGACY_BODY_ONLY = "LEGACY_BODY_ONLY"


# -- semantic-observation enums ------------------------------------------------


class ObserverRole(StrEnum):
    GRANITE_EXTRACTOR = "GRANITE_EXTRACTOR"
    QWEN_CHALLENGER = "QWEN_CHALLENGER"
    TEST_FIXTURE = "TEST_FIXTURE"


class ClaimedRole(StrEnum):
    PERSON = "PERSON"
    BUSINESS = "BUSINESS"
    SERVICE = "SERVICE"
    AUTOMATION = "AUTOMATION"
    UNKNOWN = "UNKNOWN"


class IdentityConsistency(StrEnum):
    CONSISTENT = "CONSISTENT"
    INCONSISTENT = "INCONSISTENT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NOT_EVALUATED = "NOT_EVALUATED"


class MessageCategory(StrEnum):
    PERSONAL = "PERSONAL"
    HUMAN_REQUEST = "HUMAN_REQUEST"
    APPOINTMENT = "APPOINTMENT"
    BILL_OR_INVOICE = "BILL_OR_INVOICE"
    RECEIPT = "RECEIPT"
    ACCOUNT_SECURITY = "ACCOUNT_SECURITY"
    NEWSLETTER = "NEWSLETTER"
    MARKETING = "MARKETING"
    AUTOMATED = "AUTOMATED"
    PHISHING_SUSPECTED = "PHISHING_SUSPECTED"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class RequestType(StrEnum):
    READ = "READ"
    REPLY = "REPLY"
    SCHEDULE = "SCHEDULE"
    PAY = "PAY"
    CHANGE_PAYMENT_DETAILS = "CHANGE_PAYMENT_DETAILS"
    SUBMIT_CREDENTIALS = "SUBMIT_CREDENTIALS"
    DOWNLOAD = "DOWNLOAD"
    OPEN_LINK = "OPEN_LINK"
    SHARE_PERSONAL_DATA = "SHARE_PERSONAL_DATA"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class RequestUrgency(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    EMERGENCY_CLAIM = "EMERGENCY_CLAIM"
    UNKNOWN = "UNKNOWN"


class ComponentRiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"
    NOT_EVALUATED = "NOT_EVALUATED"


# =============================================================================
# security-assessment submodels
# =============================================================================


class Completeness(_StrictModel):
    """Which checks ran, were missing, or were skipped (I-02 evidence)."""

    available: list[Code] = Field(default_factory=list, max_length=64)
    missing: list[Code] = Field(default_factory=list, max_length=64)
    checks_not_run: list[Code] = Field(default_factory=list, max_length=64)

    @field_validator("available", "missing", "checks_not_run")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value)


class AuthObservation(_StrictModel):
    """One authentication axis (SPF/DKIM/DMARC/ARC).

    ``result`` records the observed outcome and ``provenance`` records whether
    it was independently verified or merely provider/header reported — a
    forged ``Authentication-Results`` header is ``UNTRUSTED_HEADER``, never
    treated as verified truth (design §3, threat T-28).
    """

    result: AuthResult
    provenance: AuthProvenance
    evidence_refs: list[OpaqueRef] = Field(default_factory=list, max_length=16)

    @field_validator("evidence_refs")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value)


class Authentication(_StrictModel):
    """The four authentication axes; each independently ``UNKNOWN``-capable."""

    spf: AuthObservation
    dkim: AuthObservation
    dmarc: AuthObservation
    arc: AuthObservation


class SecurityFinding(_StrictModel):
    """A single typed deterministic security finding (design §5, R-005).

    Carries a safe ``explanation_code`` and opaque ``evidence_refs`` only — no
    raw body fragments (design §9, I-05).
    """

    finding_key: OpaqueRef
    rule_id: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    category: FindingCategory
    severity: FindingSeverity
    status: FindingStatus
    detector_version: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    evidence_refs: list[OpaqueRef] = Field(default_factory=list, max_length=24)
    explanation_code: Code

    @field_validator("evidence_refs")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value)


class AxisScore(_StrictModel):
    """One of the six independent trust axes (design §3).

    ``value`` is an *uncalibrated* estimate unless ``semantics`` is
    ``CALIBRATED_PROBABILITY``. ``NOT_EVALUATED`` must carry a null value and
    null calibration id (schema ``score`` allOf); ``CALIBRATED_PROBABILITY``
    requires both a numeric value and a calibration id.

    Schema-parity note (Task 1.6, carried-forward from Task 1.1): the JSON
    ``score`` object lists ``value``, ``semantics``, ``calibration_id`` and
    ``evidence_refs`` as *required keys*. ``value`` and ``calibration_id`` may
    be ``null`` where the schema allows null, but the keys must be *present* —
    a payload that omits them is rejected by the JSON schema. To keep
    model-validation aligned with that required-key contract these three fields
    carry **no default** and must be supplied explicitly (``evidence_refs`` may
    be an empty list, ``value``/``calibration_id`` may be ``None``). This is an
    additive tightening: it only forbids *omitting* a key the schema already
    required, so every assessment that conformed to the schema still validates.
    """

    value: Annotated[float, Field(ge=0.0, le=1.0)] | None
    semantics: ScoreSemantics
    calibration_id: Annotated[str, StringConstraints(max_length=100)] | None
    evidence_refs: list[OpaqueRef] = Field(max_length=24)

    @field_validator("evidence_refs")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value)

    @model_validator(mode="after")
    def _semantics_consistency(self) -> AxisScore:
        if self.semantics is ScoreSemantics.NOT_EVALUATED:
            if self.value is not None:
                raise ValueError("NOT_EVALUATED axis must have a null value")
            if self.calibration_id is not None:
                raise ValueError("NOT_EVALUATED axis must have a null calibration_id")
        if self.semantics is ScoreSemantics.CALIBRATED_PROBABILITY:
            if self.value is None:
                raise ValueError("CALIBRATED_PROBABILITY requires a numeric value")
            if not self.calibration_id:
                raise ValueError("CALIBRATED_PROBABILITY requires a calibration_id")
        return self


class TrustAxes(_StrictModel):
    """The six independent axes (design §3). No single summarizing score."""

    threat_likelihood: AxisScore
    impact_severity: AxisScore
    personal_relevance: AxisScore
    urgency: AxisScore
    expectedness: AxisScore
    actionability: AxisScore


class ModelRun(_StrictModel):
    """Metadata about a model invocation — an observation, not authority (I-08).

    Records the resolved route/model/runtime and status so a local-only policy
    can be audited; it never carries model output text or a capability grant.
    """

    route: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    model_id: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    artifact_digest: Annotated[str, StringConstraints(max_length=160)] | None = None
    runtime: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    status: ModelRunStatus
    privacy_policy_version: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    latency_ms: Annotated[float, Field(ge=0.0)] | None = None


class PolicyDisposition(_StrictModel):
    """Deterministic policy verdict (design §8).

    ``action_capabilities`` is a list of safe capability *codes*; in SHADOW
    mode it must be empty (schema allOf #1 / steering default-SHADOW). This
    model never carries free command text (I-01).
    """

    mode: AssessmentMode
    disposition: Disposition
    policy_version: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    reason_codes: list[Code] = Field(default_factory=list, max_length=64)
    human_approval_required: bool
    action_capabilities: list[Code] = Field(default_factory=list, max_length=32)

    @field_validator("reason_codes", "action_capabilities")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value)

    @model_validator(mode="after")
    def _shadow_has_no_capabilities(self) -> PolicyDisposition:
        if self.mode is AssessmentMode.SHADOW and self.action_capabilities:
            raise ValueError("SHADOW mode must not grant any action_capabilities")
        return self


# -- restrictive disposition set for non-clean states (I-02) -------------------

_RESTRICTED_DISPOSITIONS: frozenset[Disposition] = frozenset(
    {Disposition.INCOMPLETE, Disposition.REVIEW, Disposition.RESTRICTED}
)
_NON_CLEAN_STATES: frozenset[ProcessingState] = frozenset(
    {
        ProcessingState.INCOMPLETE,
        ProcessingState.ERROR_RETRYABLE,
        ProcessingState.DEAD_LETTER,
    }
)


class SecurityAssessment(_StrictModel):
    """Persisted metadata-only mail security assessment (design §9).

    Mirrors ``security-assessment.schema.json``. Carries only metadata and
    opaque evidence refs — never raw body, subject, addresses or secrets
    (I-05/R-022.4). Encodes the two schema invariants as validators:

    - SHADOW mode => empty ``action_capabilities`` (on :class:`PolicyDisposition`).
    - INCOMPLETE/ERROR_RETRYABLE/DEAD_LETTER => disposition restricted to
      {INCOMPLETE, REVIEW, RESTRICTED}, never SAFE_TO_PROCESS (I-02).
    """

    schema_version: str = SCHEMA_VERSION
    assessment_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9:_-]{1,160}$")]
    account_scope: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9:_-]{1,128}$")]
    source_ref: OpaqueRef
    source_revision: Annotated[int, Field(ge=1)]
    analysis_profile_version: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    created_at: datetime
    processing_state: ProcessingState
    evidence_fidelity: EvidenceFidelity
    completeness: Completeness
    authentication: Authentication
    findings: list[SecurityFinding] = Field(default_factory=list, max_length=256)
    axes: TrustAxes
    model_runs: list[ModelRun] = Field(default_factory=list, max_length=16)
    policy: PolicyDisposition

    @field_validator("schema_version")
    @classmethod
    def _schema_version_pinned(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION!r}")
        return value

    @model_validator(mode="after")
    def _non_clean_state_restricts_disposition(self) -> SecurityAssessment:
        if (
            self.processing_state in _NON_CLEAN_STATES
            and self.policy.disposition not in _RESTRICTED_DISPOSITIONS
        ):
            raise ValueError(
                "INCOMPLETE/ERROR_RETRYABLE/DEAD_LETTER states require a restricted "
                "disposition (INCOMPLETE/REVIEW/RESTRICTED); absent/unscanned/failed "
                "is never SAFE_TO_PROCESS (I-02)"
            )
        return self


# =============================================================================
# message-evidence manifest (design §4.1 ingest envelope)
# =============================================================================


class MessageEvidenceManifest(_StrictModel):
    """Immutable evidence manifest for one account-scoped source observation.

    Metadata + opaque content refs and a content hash only — no raw bytes
    (design §4.1/§9, I-05). ``evidence_fidelity`` records how lossy the capture
    was so a lossy summary is never mistaken for original signed MIME (I-05).
    """

    schema_version: str = SCHEMA_VERSION
    source_id: OpaqueRef
    account_scope: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9:_-]{1,128}$")]
    source_revision: Annotated[int, Field(ge=1)] = 1
    provider: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    provider_uid: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    folder_or_label: Annotated[str, StringConstraints(max_length=200)] | None = None
    source_message_id: OpaqueRef | None = None
    acquisition_mode: AcquisitionMode
    acquired_at: datetime
    source_time: datetime | None = None
    raw_content_ref: OpaqueRef | None = None
    normalized_content_ref: OpaqueRef | None = None
    sha256: Annotated[str, StringConstraints(max_length=64)] | None = None
    evidence_fidelity: EvidenceFidelity
    missing_parts: list[Code] = Field(default_factory=list, max_length=64)

    @field_validator("schema_version")
    @classmethod
    def _schema_version_pinned(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION!r}")
        return value

    @field_validator("missing_parts")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value)

    @field_validator("sha256")
    @classmethod
    def _sha256_shape(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_RE.match(value):
            raise ValueError("sha256 must be 64 hex characters")
        return value


# =============================================================================
# semantic-observation submodels (untrusted model output; observations only)
# =============================================================================


#: Uncalibrated model estimate in [0, 1]. Strict: numeric strings, bools, NaN and
#: infinities are rejected (schema ``type: number``, min 0, max 1).
Confidence = Annotated[float, Field(ge=0.0, le=1.0, strict=True, allow_inf_nan=False)]


class Component(_StrictModel):
    """An observed link/image/attachment component.

    ``model_confidence`` is an uncalibrated model estimate, not a verified
    probability (design §3/§7). ``risk_level`` supports UNKNOWN/NOT_EVALUATED.
    """

    evidence_ref: OpaqueRef
    risk_level: ComponentRiskLevel
    indicator_codes: list[Code] = Field(max_length=32)
    model_confidence: Confidence | None

    @field_validator("indicator_codes")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value)


class ObservationRequest(_StrictModel):
    """A request the message appears to make (observation only, I-01/I-08)."""

    request_type: RequestType
    claimed_urgency: RequestUrgency
    evidence_refs: list[OpaqueRef] = Field(max_length=24)
    high_impact: Annotated[bool, Field(strict=True)]
    requires_independent_verification: Annotated[bool, Field(strict=True)]

    @field_validator("evidence_refs")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value)


class RiskIndicator(_StrictModel):
    """An observed risk indicator with an uncalibrated confidence estimate."""

    indicator_code: Code
    evidence_refs: list[OpaqueRef] = Field(max_length=24)
    model_confidence: Confidence | None

    @field_validator("evidence_refs")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value)


class Contradiction(_StrictModel):
    """An observed internal contradiction (e.g. display-name vs. address)."""

    contradiction_code: Code
    evidence_refs: list[OpaqueRef] = Field(max_length=24)

    @field_validator("evidence_refs")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value)


class ClaimedIdentity(_StrictModel):
    """Who the message *claims* to be from (observation, never verified)."""

    claimed_role: ClaimedRole
    entity_refs: list[OpaqueRef] = Field(max_length=24)
    identity_consistency: IdentityConsistency

    @field_validator("entity_refs")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value)


class SemanticObservation(_StrictModel):
    """Strict schema for an untrusted LLM observation (design §7, I-01/I-08).

    This is *not* a policy decision, permission grant, or agent instruction.
    ``gist`` is a short bounded summary, not raw body text; all references are
    opaque. Mirrors ``semantic-observation.schema.json``.
    """

    schema_version: str
    source_ref: OpaqueRef
    observer_role: ObserverRole
    gist: Annotated[str, StringConstraints(max_length=600)]
    claimed_identity: ClaimedIdentity
    message_category: MessageCategory
    requests: list[ObservationRequest] = Field(max_length=32)
    links: list[Component] = Field(max_length=128)
    images: list[Component] = Field(max_length=128)
    attachments: list[Component] = Field(max_length=128)
    risk_indicators: list[RiskIndicator] = Field(max_length=64)
    contradictions: list[Contradiction] = Field(max_length=24)
    missing_evidence: list[Code] = Field(max_length=64)
    evidence_refs: list[OpaqueRef] = Field(max_length=256)

    @field_validator("schema_version")
    @classmethod
    def _schema_version_pinned(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION!r}")
        return value

    @field_validator("missing_evidence", "evidence_refs")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value)


# -- shared helper -------------------------------------------------------------


def _require_unique(value: list[str]) -> list[str]:
    """Enforce the schema ``uniqueItems: true`` constraint on code/ref lists."""
    if len(set(value)) != len(value):
        raise ValueError("list items must be unique")
    return value
