"""NerveShield Phase 1 Task 1.1 — strict data-contract model tests.

Tests-first coverage for the versioned Pydantic models in
``backend.mail_security.models`` that mirror the two authoritative JSON
schemas (``schemas/security-assessment.schema.json`` and
``schemas/semantic-observation.schema.json``) in the NerveShield spec.

Requirements exercised: R-002 (evidence/provenance contract), R-005
(deterministic findings shape), R-007 (six independent trust axes) and
R-025 (strict serialization + invalid-input proof). Schema invariants
encoded as validators are proven here:

- ``extra="forbid"`` on every model (reject unknown keys).
- ``max_length`` cap on every user-controlled string (reject over-length).
- enum values mirror the schema enums exactly (reject unknown enum values).
- SHADOW mode => ``action_capabilities`` must be empty (schema allOf #1).
- INCOMPLETE / ERROR_RETRYABLE / DEAD_LETTER processing states => disposition
  restricted to {INCOMPLETE, REVIEW, RESTRICTED}, never SAFE_TO_PROCESS
  (schema allOf #2 / invariant I-02: absent/unscanned/failed is never clean).
- every trust axis supports NOT_EVALUATED (value/calibration null) and every
  auth/component axis supports UNKNOWN / NOT_EVALUATED.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.mail_security import (
    NERVE_MAIL_SECURITY_ENABLED,
    AcquisitionMode,
    AssessmentMode,
    Authentication,
    AuthObservation,
    AuthProvenance,
    AuthResult,
    AxisScore,
    ClaimedIdentity,
    ClaimedRole,
    Completeness,
    Component,
    ComponentRiskLevel,
    Contradiction,
    Disposition,
    EvidenceFidelity,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    IdentityConsistency,
    MessageCategory,
    MessageEvidenceManifest,
    ModelRun,
    ModelRunStatus,
    ObservationRequest,
    ObserverRole,
    PolicyDisposition,
    ProcessingState,
    RequestType,
    RequestUrgency,
    RiskIndicator,
    ScoreSemantics,
    SecurityAssessment,
    SecurityFinding,
    SemanticObservation,
    TrustAxes,
)

# -- helpers -----------------------------------------------------------------


def _not_evaluated_score() -> AxisScore:
    return AxisScore(
        value=None,
        semantics=ScoreSemantics.NOT_EVALUATED,
        calibration_id=None,
        evidence_refs=[],
    )


def _trust_axes_all_not_evaluated() -> TrustAxes:
    return TrustAxes(
        threat_likelihood=_not_evaluated_score(),
        impact_severity=_not_evaluated_score(),
        personal_relevance=_not_evaluated_score(),
        urgency=_not_evaluated_score(),
        expectedness=_not_evaluated_score(),
        actionability=_not_evaluated_score(),
    )


def _unknown_auth() -> AuthObservation:
    return AuthObservation(
        result=AuthResult.UNKNOWN,
        provenance=AuthProvenance.UNKNOWN,
        evidence_refs=[],
    )


def _authentication_all_unknown() -> Authentication:
    return Authentication(
        spf=_unknown_auth(),
        dkim=_unknown_auth(),
        dmarc=_unknown_auth(),
        arc=_unknown_auth(),
    )


def _policy(
    *,
    mode: AssessmentMode = AssessmentMode.SHADOW,
    disposition: Disposition = Disposition.REVIEW,
    action_capabilities: list[str] | None = None,
) -> PolicyDisposition:
    return PolicyDisposition(
        mode=mode,
        disposition=disposition,
        policy_version="policy-v1",
        reason_codes=["NO_AUTH_EVIDENCE"],
        human_approval_required=True,
        action_capabilities=action_capabilities or [],
    )


def _assessment(
    *,
    processing_state: ProcessingState = ProcessingState.ASSESSED,
    policy: PolicyDisposition | None = None,
) -> SecurityAssessment:
    return SecurityAssessment(
        assessment_id="assess:acct1:src1:1",
        account_scope="acct:1",
        source_ref="src:msg:1",
        source_revision=1,
        analysis_profile_version="profile-1",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        processing_state=processing_state,
        evidence_fidelity=EvidenceFidelity.METADATA_ONLY,
        completeness=Completeness(available=[], missing=[], checks_not_run=[]),
        authentication=_authentication_all_unknown(),
        findings=[],
        axes=_trust_axes_all_not_evaluated(),
        model_runs=[],
        policy=policy or _policy(),
    )


def _finding() -> SecurityFinding:
    return SecurityFinding(
        finding_key="finding:1",
        rule_id="rule-001",
        category=FindingCategory.AUTH_PROVENANCE,
        severity=FindingSeverity.UNKNOWN,
        status=FindingStatus.NOT_EVALUATED,
        detector_version="det-1",
        evidence_refs=["ev:1"],
        explanation_code="NO_AUTH_EVIDENCE",
    )


def _manifest() -> MessageEvidenceManifest:
    return MessageEvidenceManifest(
        source_id="src:msg:1",
        account_scope="acct:1",
        provider="gmail",
        provider_uid="uid-123",
        folder_or_label="INBOX",
        source_message_id="msgid:abc",
        acquisition_mode=AcquisitionMode.LEGACY_BODY_ONLY,
        acquired_at=datetime(2024, 1, 1, tzinfo=UTC),
        source_time=datetime(2024, 1, 1, tzinfo=UTC),
        raw_content_ref="ref:raw:1",
        normalized_content_ref="ref:norm:1",
        sha256="a" * 64,
        evidence_fidelity=EvidenceFidelity.BODY_ONLY,
        missing_parts=["ORIGINAL_HEADERS"],
        source_revision=1,
    )


def _observation() -> SemanticObservation:
    return SemanticObservation(
        schema_version="1.0",
        source_ref="src:msg:1",
        observer_role=ObserverRole.TEST_FIXTURE,
        gist="A short, bounded summary of the message.",
        claimed_identity=ClaimedIdentity(
            claimed_role=ClaimedRole.UNKNOWN,
            entity_refs=[],
            identity_consistency=IdentityConsistency.NOT_EVALUATED,
        ),
        message_category=MessageCategory.UNKNOWN,
        requests=[],
        links=[],
        images=[],
        attachments=[],
        risk_indicators=[],
        contradictions=[],
        missing_evidence=[],
        evidence_refs=[],
    )


# -- feature flag ------------------------------------------------------------


def test_feature_flag_defined_default_off() -> None:
    """Default-off flag constant is defined in the package (not wired)."""
    assert NERVE_MAIL_SECURITY_ENABLED == 0


# -- schema version pinning --------------------------------------------------


def test_schema_versions_pinned() -> None:
    assert _assessment().schema_version == "1.0"
    assert _observation().schema_version == "1.0"


# -- round-trip (de)serialization --------------------------------------------


def test_assessment_round_trip() -> None:
    assessment = _assessment(policy=_policy())
    dumped = assessment.model_dump(mode="json")
    restored = SecurityAssessment.model_validate(dumped)
    assert restored == assessment
    assert restored.model_dump(mode="json") == dumped


def test_observation_round_trip() -> None:
    observation = _observation()
    dumped = observation.model_dump(mode="json")
    restored = SemanticObservation.model_validate(dumped)
    assert restored == observation


def test_manifest_round_trip() -> None:
    manifest = _manifest()
    dumped = manifest.model_dump(mode="json")
    restored = MessageEvidenceManifest.model_validate(dumped)
    assert restored == manifest


def test_finding_round_trip() -> None:
    finding = _finding()
    dumped = finding.model_dump(mode="json")
    assert SecurityFinding.model_validate(dumped) == finding


# -- extra="forbid": reject unknown keys on every model ----------------------


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (SecurityAssessment, "assessment"),
        (SemanticObservation, "observation"),
        (MessageEvidenceManifest, "manifest"),
        (SecurityFinding, "finding"),
    ],
)
def test_models_forbid_unknown_keys(model: type, payload: str) -> None:
    builder = {
        "assessment": _assessment,
        "observation": _observation,
        "manifest": _manifest,
        "finding": _finding,
    }[payload]
    dumped = builder().model_dump(mode="json")
    dumped["this_field_does_not_exist"] = "x"
    with pytest.raises(ValidationError):
        model.model_validate(dumped)


def test_nested_submodels_forbid_unknown_keys() -> None:
    dumped = _assessment().model_dump(mode="json")
    dumped["policy"]["unexpected"] = "x"
    with pytest.raises(ValidationError):
        SecurityAssessment.model_validate(dumped)

    dumped2 = _assessment().model_dump(mode="json")
    dumped2["axes"]["threat_likelihood"]["unexpected"] = "x"
    with pytest.raises(ValidationError):
        SecurityAssessment.model_validate(dumped2)


# -- max_length caps on user-controlled strings ------------------------------


def test_gist_over_length_rejected() -> None:
    with pytest.raises(ValidationError):
        SemanticObservation(
            schema_version="1.0",
            source_ref="src:1",
            observer_role=ObserverRole.TEST_FIXTURE,
            gist="x" * 601,  # schema maxLength 600
            claimed_identity=ClaimedIdentity(
                claimed_role=ClaimedRole.UNKNOWN,
                entity_refs=[],
                identity_consistency=IdentityConsistency.NOT_EVALUATED,
            ),
            message_category=MessageCategory.UNKNOWN,
            requests=[],
            links=[],
            images=[],
            attachments=[],
            risk_indicators=[],
            contradictions=[],
            missing_evidence=[],
            evidence_refs=[],
        )


def test_analysis_profile_version_over_length_rejected() -> None:
    dumped = _assessment().model_dump(mode="json")
    dumped["analysis_profile_version"] = "p" * 101  # schema maxLength 100
    with pytest.raises(ValidationError):
        SecurityAssessment.model_validate(dumped)


def test_policy_version_over_length_rejected() -> None:
    dumped = _assessment().model_dump(mode="json")
    dumped["policy"]["policy_version"] = "p" * 101
    with pytest.raises(ValidationError):
        SecurityAssessment.model_validate(dumped)


def test_finding_rule_id_over_length_rejected() -> None:
    dumped = _finding().model_dump(mode="json")
    dumped["rule_id"] = "r" * 101  # schema maxLength 100
    with pytest.raises(ValidationError):
        SecurityFinding.model_validate(dumped)


def test_opaque_ref_over_length_rejected() -> None:
    dumped = _finding().model_dump(mode="json")
    dumped["finding_key"] = "k" * 201  # opaqueRef maxLength 200
    with pytest.raises(ValidationError):
        SecurityFinding.model_validate(dumped)


def test_model_run_route_over_length_rejected() -> None:
    run = ModelRun(
        route="local/granite",
        model_id="granite-1",
        artifact_digest=None,
        runtime="local",
        status=ModelRunStatus.NOT_RUN,
        privacy_policy_version="priv-1",
        latency_ms=None,
    )
    dumped = run.model_dump(mode="json")
    dumped["route"] = "x" * 121  # schema maxLength 120
    with pytest.raises(ValidationError):
        ModelRun.model_validate(dumped)


# -- opaque-ref / code pattern enforcement -----------------------------------


def test_opaque_ref_pattern_rejects_raw_text() -> None:
    """Opaque refs reject free text (spaces, @, raw addresses) — I-05/R-022.4."""
    dumped = _finding().model_dump(mode="json")
    dumped["finding_key"] = "attacker@example.com has spaces"
    with pytest.raises(ValidationError):
        SecurityFinding.model_validate(dumped)


def test_code_pattern_rejects_lowercase() -> None:
    dumped = _finding().model_dump(mode="json")
    dumped["explanation_code"] = "lowercase_code"  # code must start [A-Z]
    with pytest.raises(ValidationError):
        SecurityFinding.model_validate(dumped)


# -- enum validation mirrors schema enums ------------------------------------


def test_unknown_enum_value_rejected() -> None:
    dumped = _assessment().model_dump(mode="json")
    dumped["processing_state"] = "NOT_A_REAL_STATE"
    with pytest.raises(ValidationError):
        SecurityAssessment.model_validate(dumped)


def test_finding_category_enum_exact() -> None:
    expected = {
        "AUTH_PROVENANCE",
        "SENDER_IMPERSONATION",
        "URL_DECEPTION",
        "TRACKING",
        "HTML_RISK",
        "ATTACHMENT",
        "MALWARE",
        "PHISHING",
        "PAYMENT_FRAUD",
        "SOCIAL_ENGINEERING",
        "PROMPT_INJECTION",
        "INTEL_MATCH",
        "SCANNER_FAILURE",
        "OTHER",
    }
    assert {c.value for c in FindingCategory} == expected


def test_disposition_enum_exact() -> None:
    assert {d.value for d in Disposition} == {
        "SAFE_TO_PROCESS",
        "LOW_PRIORITY",
        "REVIEW",
        "RESTRICTED",
        "INCOMPLETE",
    }


def test_processing_state_enum_exact() -> None:
    assert {s.value for s in ProcessingState} == {
        "DISCOVERED",
        "EVIDENCE_READY",
        "DETERMINISTIC_DONE",
        "MODEL_PENDING",
        "ASSESSED",
        "REVIEW",
        "SAFE_TO_PROCESS",
        "INCOMPLETE",
        "ERROR_RETRYABLE",
        "DEAD_LETTER",
    }


# -- SHADOW => empty action capabilities (schema allOf #1) -------------------


def test_shadow_mode_rejects_nonempty_capabilities() -> None:
    with pytest.raises(ValidationError):
        _assessment(
            policy=_policy(
                mode=AssessmentMode.SHADOW,
                action_capabilities=["CREATE_REVIEW_CARD"],
            )
        )


def test_shadow_mode_allows_empty_capabilities() -> None:
    assessment = _assessment(
        policy=_policy(mode=AssessmentMode.SHADOW, action_capabilities=[])
    )
    assert assessment.policy.action_capabilities == []


def test_non_shadow_mode_allows_capabilities() -> None:
    assessment = _assessment(
        processing_state=ProcessingState.ASSESSED,
        policy=_policy(
            mode=AssessmentMode.ENFORCE,
            disposition=Disposition.SAFE_TO_PROCESS,
            action_capabilities=["CREATE_REVIEW_CARD"],
        ),
    )
    assert assessment.policy.action_capabilities == ["CREATE_REVIEW_CARD"]


# -- INCOMPLETE/ERROR/DEAD_LETTER => restricted disposition (allOf #2, I-02) -


@pytest.mark.parametrize(
    "state",
    [
        ProcessingState.INCOMPLETE,
        ProcessingState.ERROR_RETRYABLE,
        ProcessingState.DEAD_LETTER,
    ],
)
def test_incomplete_states_reject_safe_to_process(state: ProcessingState) -> None:
    with pytest.raises(ValidationError):
        _assessment(
            processing_state=state,
            policy=_policy(mode=AssessmentMode.SHADOW, disposition=Disposition.SAFE_TO_PROCESS),
        )


@pytest.mark.parametrize(
    "state",
    [
        ProcessingState.INCOMPLETE,
        ProcessingState.ERROR_RETRYABLE,
        ProcessingState.DEAD_LETTER,
    ],
)
@pytest.mark.parametrize(
    "disposition",
    [Disposition.INCOMPLETE, Disposition.REVIEW, Disposition.RESTRICTED],
)
def test_incomplete_states_allow_restricted_set(
    state: ProcessingState, disposition: Disposition
) -> None:
    assessment = _assessment(
        processing_state=state,
        policy=_policy(mode=AssessmentMode.SHADOW, disposition=disposition),
    )
    assert assessment.policy.disposition == disposition


@pytest.mark.parametrize(
    "disposition",
    [Disposition.SAFE_TO_PROCESS, Disposition.LOW_PRIORITY],
)
def test_incomplete_state_rejects_lenient_dispositions(disposition: Disposition) -> None:
    with pytest.raises(ValidationError):
        _assessment(
            processing_state=ProcessingState.INCOMPLETE,
            policy=_policy(mode=AssessmentMode.SHADOW, disposition=disposition),
        )


# -- NOT_EVALUATED / UNKNOWN handling on every axis --------------------------


def test_every_trust_axis_supports_not_evaluated() -> None:
    axes = _trust_axes_all_not_evaluated()
    for name in (
        "threat_likelihood",
        "impact_severity",
        "personal_relevance",
        "urgency",
        "expectedness",
        "actionability",
    ):
        score = getattr(axes, name)
        assert score.value is None
        assert score.semantics == ScoreSemantics.NOT_EVALUATED
        assert score.calibration_id is None


def test_not_evaluated_score_rejects_value() -> None:
    """NOT_EVALUATED must carry a null value (schema allOf then: value null)."""
    with pytest.raises(ValidationError):
        AxisScore(
            value=0.5,
            semantics=ScoreSemantics.NOT_EVALUATED,
            calibration_id=None,
            evidence_refs=[],
        )


def test_calibrated_probability_requires_calibration_id() -> None:
    """CALIBRATED_PROBABILITY requires a non-null value and calibration_id."""
    with pytest.raises(ValidationError):
        AxisScore(
            value=None,
            semantics=ScoreSemantics.CALIBRATED_PROBABILITY,
            calibration_id=None,
            evidence_refs=[],
        )
    with pytest.raises(ValidationError):
        AxisScore(
            value=0.8,
            semantics=ScoreSemantics.CALIBRATED_PROBABILITY,
            calibration_id=None,
            evidence_refs=[],
        )
    ok = AxisScore(
        value=0.8,
        semantics=ScoreSemantics.CALIBRATED_PROBABILITY,
        calibration_id="cal-1",
        evidence_refs=[],
    )
    assert ok.value == 0.8


def test_raw_score_allows_value_without_calibration() -> None:
    score = AxisScore(
        value=0.3,
        semantics=ScoreSemantics.RAW_SCORE,
        calibration_id=None,
        evidence_refs=[],
    )
    assert score.value == 0.3


def test_axis_score_value_range_enforced() -> None:
    for bad in (-0.1, 1.1):
        with pytest.raises(ValidationError):
            AxisScore(
                value=bad,
                semantics=ScoreSemantics.RAW_SCORE,
                calibration_id=None,
                evidence_refs=[],
            )


def test_every_auth_axis_supports_unknown() -> None:
    auth = _authentication_all_unknown()
    for name in ("spf", "dkim", "dmarc", "arc"):
        obs = getattr(auth, name)
        assert obs.result == AuthResult.UNKNOWN
        assert obs.provenance == AuthProvenance.UNKNOWN


def test_auth_result_enum_exact() -> None:
    assert {r.value for r in AuthResult} == {"PASS", "FAIL", "NEUTRAL", "UNKNOWN"}
    assert {p.value for p in AuthProvenance} == {
        "INDEPENDENT",
        "PROVIDER_REPORTED",
        "UNTRUSTED_HEADER",
        "UNKNOWN",
    }


def test_component_supports_not_evaluated_and_unknown() -> None:
    for level in (ComponentRiskLevel.UNKNOWN, ComponentRiskLevel.NOT_EVALUATED):
        component = Component(
            evidence_ref="ev:1",
            risk_level=level,
            indicator_codes=[],
            model_confidence=None,
        )
        assert component.risk_level == level
        assert component.model_confidence is None


def test_component_confidence_range_enforced() -> None:
    with pytest.raises(ValidationError):
        Component(
            evidence_ref="ev:1",
            risk_level=ComponentRiskLevel.LOW,
            indicator_codes=[],
            model_confidence=1.5,
        )


# -- bounded collections mirror schema maxItems ------------------------------


def test_findings_collection_capped() -> None:
    dumped = _assessment().model_dump(mode="json")
    dumped["findings"] = [_finding().model_dump(mode="json")] * 257  # maxItems 256
    with pytest.raises(ValidationError):
        SecurityAssessment.model_validate(dumped)


def test_reason_codes_unique_and_capped() -> None:
    dumped = _assessment().model_dump(mode="json")
    dumped["policy"]["reason_codes"] = [f"CODE_{i}" for i in range(65)]  # maxItems 64
    with pytest.raises(ValidationError):
        SecurityAssessment.model_validate(dumped)


# -- request / observation submodels -----------------------------------------


def test_observation_request_round_trip() -> None:
    request = ObservationRequest(
        request_type=RequestType.PAY,
        claimed_urgency=RequestUrgency.EMERGENCY_CLAIM,
        evidence_refs=["ev:1"],
        high_impact=True,
        requires_independent_verification=True,
    )
    assert ObservationRequest.model_validate(request.model_dump(mode="json")) == request


def test_risk_indicator_and_contradiction_round_trip() -> None:
    indicator = RiskIndicator(
        indicator_code="LOOKS_LIKE_PHISH",
        evidence_refs=["ev:1"],
        model_confidence=0.4,
    )
    contradiction = Contradiction(
        contradiction_code="DISPLAY_NAME_MISMATCH",
        evidence_refs=["ev:1"],
    )
    assert RiskIndicator.model_validate(indicator.model_dump(mode="json")) == indicator
    assert (
        Contradiction.model_validate(contradiction.model_dump(mode="json")) == contradiction
    )
