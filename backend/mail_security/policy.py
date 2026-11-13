"""Conservative, versioned security decision policy v1 for NerveShield (Task 1.6).

This module derives a **disposition** from the *separate* evidence produced by
earlier Phase 1 tasks — it never reaches out, never persists, and never mutates
provider mail. It is a pure, deterministic, versioned function:

    input  = :class:`PolicyEvidence` (the independent analytical results:
             authentication provenance/axes from Task 1.4, the deterministic
             :class:`~backend.mail_security.models.SecurityFinding` list from
             Task 1.5, the parser completeness / ``INSPECTION_INCOMPLETE`` state
             from Task 1.3, and optional model-run metadata placeholders)
    output = :class:`PolicyResult` carrying a
             :class:`~backend.mail_security.models.PolicyDisposition` (the
             decision block of ``security-assessment.schema.json``), the six
             DISTINCT :class:`~backend.mail_security.models.TrustAxes`, and the
             separate :class:`~backend.mail_security.models.Authentication`
             block (technical authenticity), stamped with ``created_at`` from an
             **injected clock** (never wall-clock inside the pure function).

Design / requirement anchors (`.kiro/specs/nerve-shield/`):

- **R-007 / design §3 / §8.1 — distinct axes, no single score.** Technical
  authenticity (the ``authentication`` block), threat likelihood, threat
  severity (``impact_severity``), personal relevance, urgency, actionability
  and uncertainty are represented as DISTINCT fields. There is **no** single
  collapsed "legitimacy" scalar and no threshold on one. Fusion is explicit,
  versioned rule logic; axes stay separate in the output. Uncertainty is
  carried by ``NOT_EVALUATED`` / ``UNKNOWN`` semantics plus the completeness
  state, never manufactured into a certainty.

- **R-007.2 — uncalibrated numbers are not probabilities.** No calibration
  mapping exists in Phase 1, so any model-derived number is labeled
  ``RAW_SCORE`` (uncalibrated) and can never drive ``SAFE_TO_PROCESS`` on its
  own. A ``CALIBRATED_PROBABILITY`` is never emitted here.

- **R-011 / design §8.1 / §8.4 — risk/decision separation, no privilege
  propagation.** The disposition is derived separately from the evidence. A
  message suspected malicious or insufficiently assessed prevents any
  autonomous unsafe action (SHADOW grants none at all) but may still be a safe
  review item. A claim of emergency / prior approval / internal privilege
  SHALL NOT expand the disposition — such claims are recorded as a reason code
  and otherwise ignored by the decision.

- **R-012 / design §8.2 — shadow-first.** v1 is ``SHADOW``: it records what
  *would* have happened and carries empty ``action_capabilities`` with no
  approvals. Moving to NOTIFY/REVIEW_GATED/ENFORCE is a future, explicit config
  change outside this module.

- **I-02 / T-02 / T-04 / T-18 / T-26 — fail closed.** Absent/clean findings
  with incomplete evidence or UNKNOWN auth never become ``SAFE_TO_PROCESS``. A
  high-severity credential/payment ask forces REVIEW even when the sender is
  provider-authenticated (a compromised-but-signed sender). Timeouts / caps /
  malformed input yield INCOMPLETE, never clean.

The policy consumes only metadata and opaque codes/refs (safe
``explanation_code`` / reason codes); it never reads raw body text (I-05).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from backend.mail_security.models import (
    AssessmentMode,
    Authentication,
    AuthProvenance,
    AuthResult,
    AxisScore,
    Code,
    Disposition,
    FindingCategory,
    FindingSeverity,
    PolicyDisposition,
    ScoreSemantics,
    SecurityFinding,
    TrustAxes,
)

# -- version -----------------------------------------------------------------

#: Stable version of this decision policy. Any change to the fusion rules or
#: their thresholds MUST bump this constant so a persisted verdict records which
#: rule set produced it (R-007.4, R-011.1/.5).
POLICY_VERSION = "mail-security-policy@4"

# -- deployment mode ---------------------------------------------------------

#: v1 is SHADOW-only (R-012.1). NOTIFY/REVIEW_GATED/ENFORCE require an explicit
#: future configuration change and are intentionally not reachable here.
_MODE: AssessmentMode = AssessmentMode.SHADOW

# -- reason codes (safe upper-snake; never raw text) -------------------------

_R_INSPECTION_INCOMPLETE: Code = "INSPECTION_INCOMPLETE"
_R_AUTH_UNKNOWN: Code = "AUTH_PROVENANCE_UNKNOWN"
_R_NO_INDEPENDENT_AUTH: Code = "NO_INDEPENDENT_AUTH_EVIDENCE"
_R_HIGH_SEVERITY_FINDING: Code = "HIGH_SEVERITY_FINDING"
_R_CREDENTIAL_OR_PAYMENT_ASK: Code = "CREDENTIAL_OR_PAYMENT_ASK"
_R_DECEPTIVE_URL: Code = "DECEPTIVE_URL_FINDING"
_R_SENDER_IMPERSONATION: Code = "SENDER_IMPERSONATION_FINDING"
_R_PROMPT_INJECTION: Code = "PROMPT_INJECTION_FINDING"
_R_THREAT_FINDINGS_PRESENT: Code = "THREAT_FINDINGS_PRESENT"
_R_CLEAN_BUT_UNASSESSED: Code = "CLEAN_BUT_UNASSESSED"
_R_BULK_MAIL: Code = "BULK_MAIL"
_R_UNCALIBRATED_MODEL_NUMBER: Code = "UNCALIBRATED_MODEL_ESTIMATE"
_R_CLAIM_IGNORED: Code = "UNTRUSTED_AUTHORITY_CLAIM_IGNORED"
_R_THREAT_INTEL_MATCH: Code = "THREAT_INTEL_MATCH"
_R_MALWARE_FINDING: Code = "MALWARE_FINDING"
_R_ATTACHMENT_FINDING: Code = "ATTACHMENT_FINDING"

#: Finding categories that, at HIGH/CRITICAL severity, represent a high-impact
#: credential/payment/phishing instruction that mandates review even when the
#: sender is authenticated (design §8.1, T-02/T-04).
_HIGH_IMPACT_ASK_CATEGORIES: frozenset[FindingCategory] = frozenset(
    {
        FindingCategory.PAYMENT_FRAUD,
        FindingCategory.PHISHING,
        FindingCategory.SOCIAL_ENGINEERING,
    }
)

#: Finding categories that signal a deception/impersonation threat.
_DECEPTION_CATEGORIES: frozenset[FindingCategory] = frozenset(
    {
        FindingCategory.URL_DECEPTION,
        FindingCategory.SENDER_IMPERSONATION,
        FindingCategory.MALWARE,
        FindingCategory.ATTACHMENT,
        FindingCategory.INTEL_MATCH,
    }
)

#: Severities that count as a positive threat signal (not merely informational
#: tracking noise).
_THREAT_SEVERITIES: frozenset[FindingSeverity] = frozenset(
    {FindingSeverity.MEDIUM, FindingSeverity.HIGH, FindingSeverity.CRITICAL}
)

_HIGH_SEVERITIES: frozenset[FindingSeverity] = frozenset(
    {FindingSeverity.HIGH, FindingSeverity.CRITICAL}
)


# =============================================================================
# inputs
# =============================================================================


@dataclass(frozen=True)
class PolicyEvidence:
    """The separate evidence the policy fuses — never a decision itself.

    Each field is an *independent* analytical result (design §8.1). The policy
    does not recompute or second-guess any of them; it applies versioned rules.

    - ``authentication`` — the Task 1.4 provenance-aware auth block (technical
      authenticity). ``PROVIDER_REPORTED`` is not independent verification.
    - ``findings`` — the Task 1.5 deterministic finding list (may be empty).
    - ``inspection_complete`` — ``False`` when the parser hit a cap or failed
      (``INSPECTION_INCOMPLETE``); such a message is never clean (I-02, T-18).
    - ``incomplete_reason_codes`` — optional safe codes describing why.
    - ``bulk_mail`` — a non-authoritative hint that the message is bulk /
      no-reply (used only to prefer LOW_PRIORITY over REVIEW for *clean,
      complete* mail; never to upgrade to SAFE_TO_PROCESS).
    - ``model_benign_estimate`` — an OPTIONAL uncalibrated model number
      placeholder. With no calibration mapping in Phase 1 it is recorded as an
      uncalibrated ``RAW_SCORE`` and can never drive SAFE_TO_PROCESS (R-007.2).
    - ``claims_*`` — observations that the message *claims* emergency / prior
      approval / internal privilege. These NEVER expand the disposition
      (R-011.4); they are recorded as a reason code and otherwise ignored.
    """

    authentication: Authentication
    findings: list[SecurityFinding] = field(default_factory=list)
    inspection_complete: bool = True
    incomplete_reason_codes: list[Code] = field(default_factory=list)
    bulk_mail: bool = False
    #: NS-ADR-072: the Reply-To is on a free-mail provider (gmail.com, ...).
    #: Computed by the worker from the parsed headers; never persisted.
    reply_to_freemail: bool = False
    model_benign_estimate: float | None = None
    claims_emergency: bool = False
    claims_prior_approval: bool = False
    claims_internal_privilege: bool = False


# =============================================================================
# output
# =============================================================================


@dataclass(frozen=True)
class PolicyResult:
    """The policy output: a disposition plus the DISTINCT evidence axes.

    Deliberately exposes **no** single collapsed legitimacy/trust score. The
    six trust axes live on ``axes`` (each independently set or
    ``NOT_EVALUATED``), technical authenticity stays on the separate
    ``technical_authentication`` block, and ``created_at`` is stamped from the
    injected clock.
    """

    policy: PolicyDisposition
    axes: TrustAxes
    technical_authentication: Authentication
    created_at: datetime


# =============================================================================
# public entry point
# =============================================================================


def decide_policy(
    evidence: PolicyEvidence,
    *,
    clock: Callable[[], datetime],
) -> PolicyResult:
    """Derive the conservative SHADOW disposition from separate evidence.

    Pure and deterministic: identical ``evidence`` yields an identical
    disposition and axes, and ``created_at`` comes only from the injected
    ``clock`` (no wall-clock read inside this function). Never raises on
    well-typed input.
    """
    if not isinstance(evidence, PolicyEvidence):
        raise TypeError("evidence must be a PolicyEvidence")

    created_at = clock()

    axes = _derive_axes(evidence)
    disposition, reason_codes = _derive_disposition(evidence)

    policy = PolicyDisposition(
        mode=_MODE,
        disposition=disposition,
        policy_version=POLICY_VERSION,
        reason_codes=_dedupe(reason_codes),
        # Any non-clean disposition requires a human to look; even a
        # LOW_PRIORITY clean-but-unassessed message is never auto-actioned in
        # SHADOW. We only clear the flag for the narrow SAFE_TO_PROCESS path.
        human_approval_required=disposition is not Disposition.SAFE_TO_PROCESS,
        # SHADOW grants NO capabilities (R-012.1; schema SHADOW => []).
        action_capabilities=[],
    )

    return PolicyResult(
        policy=policy,
        axes=axes,
        technical_authentication=evidence.authentication,
        created_at=created_at,
    )


# =============================================================================
# axis derivation (each axis independent; uncertainty stays explicit)
# =============================================================================


def _not_evaluated() -> AxisScore:
    """An axis with no signal: NOT_EVALUATED, null value + null calibration."""
    return AxisScore(
        value=None,
        semantics=ScoreSemantics.NOT_EVALUATED,
        calibration_id=None,
        evidence_refs=[],
    )


def _raw_score(value: float) -> AxisScore:
    """An uncalibrated deterministic score (RAW_SCORE; never a probability).

    Policy-normalized deterministic signals are honest *uncalibrated* values:
    there is no validated calibration mapping in Phase 1, so ``semantics`` is
    ``RAW_SCORE`` and ``calibration_id`` is ``None`` (R-007.2).
    """
    return AxisScore(
        value=max(0.0, min(1.0, value)),
        semantics=ScoreSemantics.RAW_SCORE,
        calibration_id=None,
        evidence_refs=[],
    )


def _derive_axes(evidence: PolicyEvidence) -> TrustAxes:
    """Set each of the six schema axes independently from the evidence.

    No axis is a function of another, and none is collapsed into a single
    summary. An axis with no supporting signal stays ``NOT_EVALUATED`` so the
    uncertainty is visible rather than defaulted to a benign number (R-007.1/.3).
    """
    findings = evidence.findings

    threat_findings = [
        f
        for f in findings
        if f.category in _DECEPTION_CATEGORIES | _HIGH_IMPACT_ASK_CATEGORIES
        and f.severity in _THREAT_SEVERITIES
    ]

    # threat_likelihood: only evaluated when a deterministic threat signal
    # exists. We never manufacture a "0.0 / safe" from the *absence* of a
    # finding (I-02), and we never read a cap/failed inspection as low threat.
    if threat_findings:
        threat_likelihood = _raw_score(0.75)
    else:
        threat_likelihood = _not_evaluated()

    # impact_severity (threat severity): driven by the max finding severity.
    severity_value = _max_severity_value(findings)
    impact_severity = (
        _raw_score(severity_value) if severity_value is not None else _not_evaluated()
    )

    # personal_relevance: Phase 1 has no personal-trust graph; always
    # NOT_EVALUATED here (that evidence arrives in a later phase).
    personal_relevance = _not_evaluated()

    # urgency: an emergency *claim* is an observation, not a verified urgency;
    # we record the signal but it never relaxes the decision (R-011.4). It is
    # kept distinct from threat.
    urgency = _raw_score(0.5) if evidence.claims_emergency else _not_evaluated()

    # expectedness: no relationship/thread history in Phase 1 => NOT_EVALUATED.
    expectedness = _not_evaluated()

    # actionability: a credential/payment/phishing ask is a high-impact
    # requested action; otherwise not evaluated here.
    actionability = (
        _raw_score(0.75)
        if any(
            f.category in _HIGH_IMPACT_ASK_CATEGORIES
            and f.severity in _HIGH_SEVERITIES
            for f in findings
        )
        else _not_evaluated()
    )

    return TrustAxes(
        threat_likelihood=threat_likelihood,
        impact_severity=impact_severity,
        personal_relevance=personal_relevance,
        urgency=urgency,
        expectedness=expectedness,
        actionability=actionability,
    )


def _max_severity_value(findings: list[SecurityFinding]) -> float | None:
    """Map the maximum finding severity to an uncalibrated [0,1] value.

    Returns ``None`` when there are no severity-bearing findings, so the axis
    stays NOT_EVALUATED rather than defaulting to 0.0 (I-02).
    """
    ladder: dict[FindingSeverity, float] = {
        FindingSeverity.INFO: 0.1,
        FindingSeverity.LOW: 0.3,
        FindingSeverity.MEDIUM: 0.6,
        FindingSeverity.HIGH: 0.85,
        FindingSeverity.CRITICAL: 1.0,
    }
    values = [ladder[f.severity] for f in findings if f.severity in ladder]
    return max(values) if values else None


# =============================================================================
# disposition derivation (explicit, versioned fusion rules — no single score)
# =============================================================================


def _derive_disposition(
    evidence: PolicyEvidence,
) -> tuple[Disposition, list[Code]]:
    """Apply the conservative fusion rules and return disposition + reasons.

    The rules are ordered most-restrictive-first and each is independent and
    testable. No rule thresholds a single fused legitimacy number; they key off
    the *separate* evidence fields.
    """
    reasons: list[Code] = []
    findings = evidence.findings

    # Record (but never act on) an untrusted authority claim (R-011.4).
    if (
        evidence.claims_emergency
        or evidence.claims_prior_approval
        or evidence.claims_internal_privilege
    ):
        reasons.append(_R_CLAIM_IGNORED)

    # Record an uncalibrated model number if one was supplied — it informs
    # nothing on its own (R-007.2).
    if evidence.model_benign_estimate is not None:
        reasons.append(_R_UNCALIBRATED_MODEL_NUMBER)

    # policy@4 (NS-ADR-072): a Reply-To on another organization's domain is
    # routine (ticketing, ESPs) once the receiving provider itself reports
    # DMARC=pass for the From domain. Such a mismatch is still recorded as a
    # finding but no longer counts as impersonation for the disposition. A
    # free-mail Reply-To (the business-email-compromise shape) and every
    # unauthenticated / untrusted-header case keep counting. Lookalike From
    # domains are caught by their own (unaffected) rules.
    if _dmarc_provider_pass(evidence.authentication) and not evidence.reply_to_freemail:
        calibrated = [f for f in findings if f.explanation_code in _REPLY_TO_MISMATCH_CODES]
        if calibrated:
            reasons.append(_R_REPLY_TO_MISMATCH_DMARC_ALIGNED)
            findings = [f for f in findings if f.explanation_code not in _REPLY_TO_MISMATCH_CODES]

    # Classify the findings into the signals the rules need.
    high_impact_ask = any(
        f.category in _HIGH_IMPACT_ASK_CATEGORIES and f.severity in _HIGH_SEVERITIES
        for f in findings
    )
    deceptive_or_impersonation = any(
        f.category in _DECEPTION_CATEGORIES and f.severity in _THREAT_SEVERITIES
        for f in findings
    )
    prompt_injection = any(
        f.category is FindingCategory.PROMPT_INJECTION for f in findings
    )
    # policy@2: ANY live threat-intel match (even a low-trust UNVERIFIED one,
    # whose severity is UNKNOWN) is a floor of REVIEW. A match can only RAISE;
    # its absence never lowers or clears anything (R-004.5, R-006.2).
    intel_match = any(f.category is FindingCategory.INTEL_MATCH for f in findings)
    # policy@3: an executable-disguised / scanner-detected MALWARE finding at
    # HIGH+ (or any CRITICAL attachment/malware finding) is RESTRICTED; lesser
    # attachment findings (MEDIUM/HIGH ATTACHMENT) stay REVIEW via rule 2.
    malware_restricted = any(
        (f.category is FindingCategory.MALWARE and f.severity in _HIGH_SEVERITIES)
        or (
            f.category in (FindingCategory.MALWARE, FindingCategory.ATTACHMENT)
            and f.severity is FindingSeverity.CRITICAL
        )
        for f in findings
    )
    high_severity_finding = any(f.severity in _HIGH_SEVERITIES for f in findings)
    threat_findings_present = any(
        f.category in _DECEPTION_CATEGORIES | _HIGH_IMPACT_ASK_CATEGORIES
        and f.severity in _THREAT_SEVERITIES
        for f in findings
    )

    auth_unknown = _auth_is_unknown(evidence.authentication)
    has_independent_auth = _has_independent_pass(evidence.authentication)

    # ---- Rule 1: a credential/payment/phishing high-impact ask mandates
    # review even when the sender is authenticated (design §8.1, T-02/T-04).
    if high_impact_ask:
        reasons.append(_R_CREDENTIAL_OR_PAYMENT_ASK)
        if high_severity_finding:
            reasons.append(_R_HIGH_SEVERITY_FINDING)
        # A deceptive link / impersonation alongside the ask pushes to
        # RESTRICTED; otherwise REVIEW. Incompleteness cannot downgrade this.
        if deceptive_or_impersonation or prompt_injection:
            reasons.extend(_deception_reasons(findings))
            return Disposition.RESTRICTED, reasons
        if intel_match:
            reasons.append(_R_THREAT_INTEL_MATCH)
        return Disposition.REVIEW, reasons

    # ---- Rule 2: a deception/impersonation/malware threat finding => REVIEW,
    # or RESTRICTED when combined with a prompt-injection attempt.
    if deceptive_or_impersonation or prompt_injection or intel_match:
        reasons.extend(_deception_reasons(findings))
        if (prompt_injection and deceptive_or_impersonation) or malware_restricted:
            return Disposition.RESTRICTED, reasons
        return Disposition.REVIEW, reasons

    # ---- Rule 3: incomplete inspection is never clean (I-02, T-18).
    if not evidence.inspection_complete:
        reasons.append(_R_INSPECTION_INCOMPLETE)
        reasons.extend(evidence.incomplete_reason_codes)
        if threat_findings_present:
            reasons.append(_R_THREAT_FINDINGS_PRESENT)
            return Disposition.REVIEW, reasons
        return Disposition.INCOMPLETE, reasons

    # ---- Rule 4 (inspection complete from here on): any remaining
    # threat-severity finding => REVIEW.
    if threat_findings_present:
        reasons.append(_R_THREAT_FINDINGS_PRESENT)
        return Disposition.REVIEW, reasons

    # ---- Rule 5: no threat findings, inspection complete. "Clean is not
    # safe": absence of a narrow-recall finding is not proof of legitimacy
    # (I-02 carried-forward from Task 1.5). Without INDEPENDENT auth evidence a
    # clean message can at most be LOW_PRIORITY, never SAFE_TO_PROCESS.
    if auth_unknown:
        reasons.append(_R_AUTH_UNKNOWN)
    if not has_independent_auth:
        reasons.append(_R_NO_INDEPENDENT_AUTH)

    reasons.append(_R_CLEAN_BUT_UNASSESSED)
    if evidence.bulk_mail:
        reasons.append(_R_BULK_MAIL)

    # SAFE_TO_PROCESS is intentionally unreachable in Phase 1: it requires
    # INDEPENDENT (cryptographically verified) auth evidence, which the Task 1.4
    # header classifier can never produce (only PROVIDER_REPORTED). Everything
    # clean-but-not-independently-verified is LOW_PRIORITY at best — a bulk
    # newsletter — and still carries human_approval_required in SHADOW.
    return Disposition.LOW_PRIORITY, reasons


# =============================================================================
# small pure helpers
# =============================================================================


def _deception_reasons(findings: list[SecurityFinding]) -> list[Code]:
    """Collect the specific deception/impersonation/injection reason codes."""
    reasons: list[Code] = []
    for finding in findings:
        if (
            finding.category is FindingCategory.URL_DECEPTION
            and finding.severity in _THREAT_SEVERITIES
        ):
            reasons.append(_R_DECEPTIVE_URL)
        elif (
            finding.category is FindingCategory.SENDER_IMPERSONATION
            and finding.severity in _THREAT_SEVERITIES
        ):
            reasons.append(_R_SENDER_IMPERSONATION)
        elif finding.category is FindingCategory.PROMPT_INJECTION:
            reasons.append(_R_PROMPT_INJECTION)
        elif finding.category is FindingCategory.INTEL_MATCH:
            reasons.append(_R_THREAT_INTEL_MATCH)
        elif (
            finding.category is FindingCategory.MALWARE
            and finding.severity in _THREAT_SEVERITIES
        ):
            reasons.append(_R_MALWARE_FINDING)
        elif (
            finding.category is FindingCategory.ATTACHMENT
            and finding.severity in _THREAT_SEVERITIES
        ):
            reasons.append(_R_ATTACHMENT_FINDING)
    return reasons


_REPLY_TO_MISMATCH_CODES = frozenset({"REPLY_TO_MISMATCH", "REPLY_TO_DOMAIN_MISMATCH"})
_R_REPLY_TO_MISMATCH_DMARC_ALIGNED = "REPLY_TO_MISMATCH_DMARC_ALIGNED"


def _dmarc_provider_pass(authentication: Authentication) -> bool:
    """DMARC=pass reported by an attested receiver (or independently verified)."""
    d = authentication.dmarc
    return d.result is AuthResult.PASS and d.provenance in (
        AuthProvenance.PROVIDER_REPORTED,
        AuthProvenance.INDEPENDENT,
    )


def _auth_is_unknown(authentication: Authentication) -> bool:
    """``True`` iff every authentication axis is UNKNOWN provenance+result."""
    for obs in (
        authentication.spf,
        authentication.dkim,
        authentication.dmarc,
        authentication.arc,
    ):
        if obs.provenance is not AuthProvenance.UNKNOWN:
            return False
        if obs.result is not AuthResult.UNKNOWN:
            return False
    return True


def _has_independent_pass(authentication: Authentication) -> bool:
    """``True`` iff any axis is an INDEPENDENT (cryptographically verified) PASS.

    Phase 1's header classifier never produces INDEPENDENT provenance, so this
    is always ``False`` today; the check is written explicitly so the
    SAFE_TO_PROCESS gate is honest and self-documenting (design §5.1, T-01).
    """
    for obs in (
        authentication.spf,
        authentication.dkim,
        authentication.dmarc,
        authentication.arc,
    ):
        if (
            obs.provenance is AuthProvenance.INDEPENDENT
            and obs.result is AuthResult.PASS
        ):
            return True
    return False


def _dedupe(codes: list[Code]) -> list[Code]:
    """Order-preserving de-duplication (the model forbids duplicate codes)."""
    seen: set[str] = set()
    unique: list[Code] = []
    for code in codes:
        if code not in seen:
            seen.add(code)
            unique.append(code)
    return unique


__all__ = [
    "POLICY_VERSION",
    "PolicyEvidence",
    "PolicyResult",
    "decide_policy",
]
