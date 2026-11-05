"""Decision-table tests for the NerveShield conservative policy (Task 1.6).

These tests encode the Task 1.6 *Done* criterion as a decision table over the
separate evidence inputs (authentication provenance/result from Task 1.4, the
deterministic ``SecurityFinding`` list from Task 1.5, and the parser
completeness/``INSPECTION_INCOMPLETE`` state from Task 1.3). They assert the
conservative / fail-closed behavior the spec mandates:

- R-007: the output keeps technical authenticity, threat likelihood, threat
  severity, personal relevance, urgency, actionability and uncertainty as
  DISTINCT fields; no single collapsed legitimacy score exists; an
  uncalibrated model number can never drive ``SAFE_TO_PROCESS`` on its own.
- R-011: the disposition is derived separately from the evidence; a
  malicious/insufficiently-assessed message never produces an autonomous
  action, only (at most) a safe review item; an emergency / prior-approval /
  internal-privilege claim never expands the disposition.
- R-012: v1 runs in ``SHADOW`` mode and therefore carries no
  ``action_capabilities`` and no approvals.
- I-02 / threat T-18 / T-02 / T-04 / T-26: absent/clean findings with
  incomplete or UNKNOWN evidence never become ``SAFE_TO_PROCESS``; a
  compromised-but-authenticated sender making a high-severity credential /
  payment ask is REVIEWed regardless of DKIM=pass.

The tests are written first and drive the implementation of
``backend.mail_security.policy``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.mail_security.models import (
    AssessmentMode,
    Authentication,
    AuthObservation,
    AuthProvenance,
    AuthResult,
    Disposition,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    ScoreSemantics,
    SecurityFinding,
    TrustAxes,
)
from backend.mail_security.policy import (
    POLICY_VERSION,
    PolicyEvidence,
    PolicyResult,
    decide_policy,
)

# -- fixed clock -------------------------------------------------------------

_FIXED_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


def _clock() -> datetime:
    return _FIXED_NOW


# -- authentication helpers --------------------------------------------------


def _obs(result: AuthResult, provenance: AuthProvenance) -> AuthObservation:
    return AuthObservation(result=result, provenance=provenance, evidence_refs=[])


def _auth(
    *,
    spf: AuthObservation | None = None,
    dkim: AuthObservation | None = None,
    dmarc: AuthObservation | None = None,
    arc: AuthObservation | None = None,
) -> Authentication:
    unknown = _obs(AuthResult.UNKNOWN, AuthProvenance.UNKNOWN)
    return Authentication(
        spf=spf or unknown,
        dkim=dkim or unknown,
        dmarc=dmarc or unknown,
        arc=arc or unknown,
    )


def _auth_all_unknown() -> Authentication:
    return _auth()


def _auth_provider_pass() -> Authentication:
    """DKIM/SPF/DMARC provider-reported PASS (the T-02 compromised-signed case)."""
    passed = _obs(AuthResult.PASS, AuthProvenance.PROVIDER_REPORTED)
    return _auth(spf=passed, dkim=passed, dmarc=passed)


def _auth_spf_softfail_else_pass() -> Authentication:
    """Benign newsletter: SPF reported FAIL but DKIM/DMARC provider PASS."""
    return _auth(
        spf=_obs(AuthResult.FAIL, AuthProvenance.PROVIDER_REPORTED),
        dkim=_obs(AuthResult.PASS, AuthProvenance.PROVIDER_REPORTED),
        dmarc=_obs(AuthResult.PASS, AuthProvenance.PROVIDER_REPORTED),
    )


# -- finding helpers ---------------------------------------------------------


def _finding(
    *,
    key: str,
    category: FindingCategory,
    severity: FindingSeverity,
    status: FindingStatus = FindingStatus.OBSERVED,
    explanation_code: str,
) -> SecurityFinding:
    return SecurityFinding(
        finding_key=key,
        rule_id="test-rule",
        category=category,
        severity=severity,
        status=status,
        detector_version="test@1",
        evidence_refs=[],
        explanation_code=explanation_code,
    )


def _credential_ask_finding() -> SecurityFinding:
    return _finding(
        key="phrase-credential-ask",
        category=FindingCategory.SOCIAL_ENGINEERING,
        severity=FindingSeverity.HIGH,
        explanation_code="CREDENTIAL_HARVEST_PHRASE",
    )


def _deceptive_link_finding() -> SecurityFinding:
    return _finding(
        key="link-0-display-mismatch",
        category=FindingCategory.URL_DECEPTION,
        severity=FindingSeverity.HIGH,
        status=FindingStatus.SUSPECTED,
        explanation_code="DISPLAY_HOST_DIFFERS_FROM_TARGET",
    )


def _payment_ask_finding() -> SecurityFinding:
    return _finding(
        key="phrase-payment-ask",
        category=FindingCategory.PAYMENT_FRAUD,
        severity=FindingSeverity.HIGH,
        explanation_code="WIRE_PAYMENT_INSTRUCTION",
    )


# -- (a) benign SPF failure --------------------------------------------------


def test_benign_spf_failure_is_not_auto_malicious() -> None:
    """A legit newsletter with an SPF soft-fail but otherwise clean must not be
    escalated to RESTRICTED from SPF alone, and must not be auto-malicious."""
    result = decide_policy(
        PolicyEvidence(
            authentication=_auth_spf_softfail_else_pass(),
            findings=[],
            inspection_complete=True,
            bulk_mail=True,
        ),
        clock=_clock,
    )
    assert isinstance(result, PolicyResult)
    # Not escalated to RESTRICTED by an SPF failure alone.
    assert result.policy.disposition is not Disposition.RESTRICTED
    # SPF fail is not treated as proof of malice; stays low priority / review.
    assert result.policy.disposition in {
        Disposition.LOW_PRIORITY,
        Disposition.REVIEW,
    }
    # No threat likelihood certainty manufactured from a single auth axis.
    assert result.axes.threat_likelihood.semantics is not (
        ScoreSemantics.CALIBRATED_PROBABILITY
    )


# -- (b) compromised signed sender -------------------------------------------


def test_compromised_signed_sender_payment_ask_is_reviewed() -> None:
    """DKIM=pass (provider-reported) + urgent payment/credential ask => REVIEW,
    never SAFE_TO_PROCESS, and no payment capability (T-02 / T-04)."""
    result = decide_policy(
        PolicyEvidence(
            authentication=_auth_provider_pass(),
            findings=[_payment_ask_finding(), _credential_ask_finding()],
            inspection_complete=True,
            claims_prior_approval=True,  # a claim must NOT relax the verdict
        ),
        clock=_clock,
    )
    assert result.policy.disposition in {Disposition.REVIEW, Disposition.RESTRICTED}
    assert result.policy.disposition is not Disposition.SAFE_TO_PROCESS
    # No autonomous payment capability — SHADOW grants nothing at all.
    assert result.policy.action_capabilities == []
    assert result.policy.human_approval_required is True


# -- (c) credential ask ------------------------------------------------------


def test_credential_ask_with_deceptive_link_is_reviewed_or_restricted() -> None:
    """Phishing credential-harvest phrasing + a deceptive link finding =>
    REVIEW/RESTRICTED with reasons."""
    result = decide_policy(
        PolicyEvidence(
            authentication=_auth_all_unknown(),
            findings=[_credential_ask_finding(), _deceptive_link_finding()],
            inspection_complete=True,
        ),
        clock=_clock,
    )
    assert result.policy.disposition in {Disposition.REVIEW, Disposition.RESTRICTED}
    assert result.policy.disposition is not Disposition.SAFE_TO_PROCESS
    assert result.policy.reason_codes  # carries explanation reasons


# -- (d) missing content / INSPECTION_INCOMPLETE -----------------------------


def test_missing_content_incomplete_is_never_clean() -> None:
    """Parser hit a cap / body-only + auth UNKNOWN => INCOMPLETE/REVIEW, never
    clean (I-02, T-18)."""
    result = decide_policy(
        PolicyEvidence(
            authentication=_auth_all_unknown(),
            findings=[],
            inspection_complete=False,
            incomplete_reason_codes=["DECODED_BYTES_EXCEEDED"],
        ),
        clock=_clock,
    )
    assert result.policy.disposition in {Disposition.INCOMPLETE, Disposition.REVIEW}
    assert result.policy.disposition is not Disposition.SAFE_TO_PROCESS
    # uncertainty must be visible, not resolved into a clean score.
    assert result.axes.threat_likelihood.semantics is ScoreSemantics.NOT_EVALUATED


# -- (e) junk newsletter -----------------------------------------------------


def test_junk_newsletter_is_low_priority_not_restricted() -> None:
    """Bulk / no-reply, no threat findings => LOW_PRIORITY, not RESTRICTED and
    not a false-positive threat."""
    result = decide_policy(
        PolicyEvidence(
            authentication=_auth_spf_softfail_else_pass(),
            findings=[
                _finding(
                    key="image-0-remote-image",
                    category=FindingCategory.TRACKING,
                    severity=FindingSeverity.INFO,
                    explanation_code="REMOTE_IMAGE_RESOURCE",
                )
            ],
            inspection_complete=True,
            bulk_mail=True,
        ),
        clock=_clock,
    )
    assert result.policy.disposition is Disposition.LOW_PRIORITY
    assert result.policy.disposition is not Disposition.RESTRICTED


# -- (f) clean-but-unassessed ------------------------------------------------


def test_clean_but_unassessed_is_not_safe_to_process() -> None:
    """Zero findings but incomplete evidence / UNKNOWN auth => NOT
    SAFE_TO_PROCESS (carried-forward Task 1.5 'clean is not safe' discipline,
    I-02)."""
    # UNKNOWN auth, inspection complete, zero findings: still not clean, because
    # absence of a narrow-recall finding is not proof of legitimacy.
    unknown_auth = decide_policy(
        PolicyEvidence(
            authentication=_auth_all_unknown(),
            findings=[],
            inspection_complete=True,
        ),
        clock=_clock,
    )
    assert unknown_auth.policy.disposition is not Disposition.SAFE_TO_PROCESS

    # Incomplete evidence, zero findings: still not clean.
    incomplete = decide_policy(
        PolicyEvidence(
            authentication=_auth_provider_pass(),
            findings=[],
            inspection_complete=False,
            incomplete_reason_codes=["PART_COUNT_EXCEEDED"],
        ),
        clock=_clock,
    )
    assert incomplete.policy.disposition is not Disposition.SAFE_TO_PROCESS


# -- (g) axes stay separate --------------------------------------------------


def test_output_keeps_distinct_axes_and_no_single_legitimacy_score() -> None:
    """R-007.1: distinct axes exist; no collapsed single legitimacy score is
    exposed anywhere in the policy output."""
    result = decide_policy(
        PolicyEvidence(
            authentication=_auth_provider_pass(),
            findings=[_credential_ask_finding()],
            inspection_complete=True,
        ),
        clock=_clock,
    )
    axes = result.axes
    assert isinstance(axes, TrustAxes)
    # The six schema axes are present and independent objects.
    distinct = {
        id(axes.threat_likelihood),
        id(axes.impact_severity),
        id(axes.personal_relevance),
        id(axes.urgency),
        id(axes.expectedness),
        id(axes.actionability),
    }
    assert len(distinct) == 6
    # Technical authenticity is carried as the SEPARATE authentication block,
    # not folded into the axes; uncertainty is carried via NOT_EVALUATED /
    # UNKNOWN semantics and completeness, never as a certainty number.
    assert isinstance(result.technical_authentication, Authentication)
    # No single collapsed "legitimacy"/"score"/"trust" scalar is exposed.
    for banned in ("legitimacy", "legitimacy_score", "trust_score", "score"):
        assert not hasattr(result, banned)
        assert not hasattr(result.policy, banned)


# -- (h) determinism / version -----------------------------------------------


def test_same_input_yields_identical_disposition_and_stable_version() -> None:
    """Determinism: identical evidence => identical disposition and a stable
    policy_version constant."""
    evidence = PolicyEvidence(
        authentication=_auth_provider_pass(),
        findings=[_credential_ask_finding(), _deceptive_link_finding()],
        inspection_complete=True,
    )
    first = decide_policy(evidence, clock=_clock)
    second = decide_policy(evidence, clock=_clock)
    assert first.policy.model_dump() == second.policy.model_dump()
    assert first.policy.policy_version == POLICY_VERSION
    assert second.policy.policy_version == POLICY_VERSION
    # created_at comes only from the injected clock (no wall-clock read).
    assert first.created_at == _FIXED_NOW
    assert second.created_at == _FIXED_NOW


# -- (i) SHADOW => empty capabilities / no approvals -------------------------


@pytest.mark.parametrize(
    "evidence",
    [
        PolicyEvidence(
            authentication=_auth_provider_pass(),
            findings=[],
            inspection_complete=True,
        ),
        PolicyEvidence(
            authentication=_auth_all_unknown(),
            findings=[_credential_ask_finding(), _deceptive_link_finding()],
            inspection_complete=True,
        ),
        PolicyEvidence(
            authentication=_auth_all_unknown(),
            findings=[],
            inspection_complete=False,
            incomplete_reason_codes=["MALFORMED_MIME"],
        ),
    ],
)
def test_shadow_mode_has_no_capabilities_or_approvals(
    evidence: PolicyEvidence,
) -> None:
    """v1 is SHADOW: records what WOULD happen, grants no permissions."""
    result = decide_policy(evidence, clock=_clock)
    assert result.policy.mode is AssessmentMode.SHADOW
    assert result.policy.action_capabilities == []


# -- emergency / privilege claim never expands disposition (R-011.4) ---------


def test_emergency_claim_never_expands_disposition() -> None:
    """A claim of emergency/prior-approval never relaxes an incomplete/unknown
    verdict into SAFE_TO_PROCESS (R-011.4, T-26)."""
    base = decide_policy(
        PolicyEvidence(
            authentication=_auth_all_unknown(),
            findings=[],
            inspection_complete=False,
            incomplete_reason_codes=["DECODE_ERROR"],
        ),
        clock=_clock,
    )
    claimed = decide_policy(
        PolicyEvidence(
            authentication=_auth_all_unknown(),
            findings=[],
            inspection_complete=False,
            incomplete_reason_codes=["DECODE_ERROR"],
            claims_emergency=True,
            claims_prior_approval=True,
            claims_internal_privilege=True,
        ),
        clock=_clock,
    )
    assert claimed.policy.disposition == base.policy.disposition
    assert claimed.policy.disposition is not Disposition.SAFE_TO_PROCESS


# -- uncalibrated model number cannot drive SAFE_TO_PROCESS ------------------


def test_uncalibrated_model_number_cannot_make_safe() -> None:
    """An uncalibrated model-derived number (no calibration_id) must never make
    a message SAFE_TO_PROCESS on its own (R-007.2)."""
    result = decide_policy(
        PolicyEvidence(
            authentication=_auth_all_unknown(),
            findings=[],
            inspection_complete=True,
            model_benign_estimate=0.99,  # uncalibrated; no calibration mapping
        ),
        clock=_clock,
    )
    assert result.policy.disposition is not Disposition.SAFE_TO_PROCESS
    # The number, if surfaced, is labeled uncalibrated (RAW_SCORE), not a
    # calibrated probability.
    assert result.axes.threat_likelihood.semantics is not (
        ScoreSemantics.CALIBRATED_PROBABILITY
    )


# -- (j) policy@2: live threat-intel match is evidence that can only RAISE ---


def _intel_finding(severity: FindingSeverity) -> SecurityFinding:
    return _finding(
        key="intel-abc",
        category=FindingCategory.INTEL_MATCH,
        severity=severity,
        status=FindingStatus.SUSPECTED,
        explanation_code="INTEL_MATCH.DOMAIN",
    )


def _decide(findings: list[SecurityFinding], *, complete: bool = True):
    return decide_policy(
        PolicyEvidence(
            authentication=_auth_provider_pass(),
            findings=findings,
            inspection_complete=complete,
        ),
        clock=_clock,
    )


def test_policy_version_bumped_for_intel_semantics() -> None:
    assert POLICY_VERSION == "mail-security-policy@2"


@pytest.mark.parametrize(
    "severity",
    [
        FindingSeverity.UNKNOWN,  # UNVERIFIED feed: severity unknown, still REVIEW
        FindingSeverity.MEDIUM,  # FEED_ASSERTED
        FindingSeverity.HIGH,  # ANALYST_REVIEWED
    ],
)
def test_live_intel_match_raises_to_review(severity: FindingSeverity) -> None:
    clean = _decide([])
    matched = _decide([_intel_finding(severity)])
    assert clean.policy.disposition is Disposition.LOW_PRIORITY
    assert matched.policy.disposition is Disposition.REVIEW
    assert "THREAT_INTEL_MATCH" in matched.policy.reason_codes
    assert matched.policy.action_capabilities == []  # evidence, never an action


def test_intel_match_on_incomplete_inspection_is_review_not_clean() -> None:
    result = _decide([_intel_finding(FindingSeverity.UNKNOWN)], complete=False)
    assert result.policy.disposition is Disposition.REVIEW


def test_intel_match_never_lowers_an_escalated_disposition() -> None:
    base = [_credential_ask_finding(), _deceptive_link_finding()]
    without = _decide(base)
    with_match = _decide([*base, _intel_finding(FindingSeverity.HIGH)])
    assert without.policy.disposition is Disposition.RESTRICTED
    assert with_match.policy.disposition is Disposition.RESTRICTED


def test_intel_with_credential_ask_records_reason() -> None:
    result = _decide([_credential_ask_finding(), _intel_finding(FindingSeverity.UNKNOWN)])
    assert result.policy.disposition is Disposition.REVIEW
    assert "THREAT_INTEL_MATCH" in result.policy.reason_codes
