"""NerveShield Phase 1 Task 1.4 — authentication-evidence representation tests.

Tests-first coverage for ``backend.mail_security.auth_evidence``: honest
classification of SPF/DKIM/DMARC/ARC claims and offline sender-provenance
observations. These are the ``forged and provider-reported fixture tests``
named as the Task 1.4 Done criterion (tasks.md).

Requirements / invariants / threats exercised:

- **R-002.4/.5** — distinguish trusted-provider assertions, independently
  verified results and unverified claims; report ``UNKNOWN`` /
  ``PROVIDER_REPORTED`` rather than fabricated verified states.
- **R-004.1** — represent SPF/DKIM/DMARC/ARC and trust-source confidence
  independently.
- **R-004.2** — an ``Authentication-Results`` field from outside the configured
  trusted boundary SHALL NOT be treated as verified.
- **R-004.3** — inspect From/Reply-To/Return-Path mismatch, display-name fraud,
  Unicode/confusable domains.
- **R-004.4 / I-06** — a compromised authenticated sender must not bypass
  content/intent: an auth observation is never an allow/trust flag.
- **R-004.5** — reputation unavailable => ``UNKNOWN``.
- **T-01** — forged ``Authentication-Results`` cannot yield independent
  verification (red-team "Forged trusted relay results" template).
- **T-02** — SPF/DKIM pass via compromised sender does not bypass content.
- **T-03** — homoglyph/IDNA lookalike yields a confusable observation offline.
- **T-28** — legacy body-only mail stays ``UNKNOWN`` / not-evaluated.

No network, no DNS, no I/O: every fixture is a synthetic in-memory header set.
"""

from __future__ import annotations

import pytest

from backend.mail_security import (
    AuthProvenance,
    AuthResult,
    FindingCategory,
    FindingStatus,
)
from backend.mail_security.auth_evidence import (
    AUTH_EVIDENCE_VERSION,
    NO_AUTH_EVIDENCE_CAPTURED,
    AuthEvidenceResult,
    SenderAddresses,
    TrustedReceiverBoundary,
    build_auth_evidence,
    classify_authentication,
    inspect_sender_provenance,
)

# -- helpers -----------------------------------------------------------------


def _trusted_boundary() -> TrustedReceiverBoundary:
    """A boundary that trusts a single provider authserv-id."""
    return TrustedReceiverBoundary(authserv_ids=["mx.google.com"])


def _ar_header(value: str) -> list[tuple[str, str]]:
    return [("Authentication-Results", value)]


# =============================================================================
# (1) FORGED — attacker-authored Authentication-Results (T-01 red-team)
# =============================================================================


def test_forged_authentication_results_is_untrusted_header_never_verified() -> None:
    """An attacker-written AR whose authserv-id is NOT inside the configured
    trusted boundary classifies as UNTRUSTED_HEADER, never INDEPENDENT (T-01).
    """
    boundary = _trusted_boundary()
    headers = _ar_header("mx.trusted.example; dkim=pass header.d=evil.example")

    result = classify_authentication(headers, boundary=boundary)

    assert isinstance(result, AuthEvidenceResult)
    dkim = result.authentication.dkim
    # The parsed state is preserved (pass) but provenance must be untrusted.
    assert dkim.result is AuthResult.PASS
    assert dkim.provenance is AuthProvenance.UNTRUSTED_HEADER
    # Never minted as independently verified.
    assert dkim.provenance is not AuthProvenance.INDEPENDENT
    assert dkim.provenance is not AuthProvenance.PROVIDER_REPORTED
    # A typed AUTH_PROVENANCE finding records the untrusted origin.
    assert any(
        f.category is FindingCategory.AUTH_PROVENANCE
        and f.explanation_code == "AUTH_RESULTS_UNTRUSTED_ORIGIN"
        for f in result.findings
    )


def test_no_axis_is_ever_independent_from_headers_alone() -> None:
    """INDEPENDENT is unreachable from header classification: we lack SMTP
    client IP/envelope and historical DNS keys (design §5.1, tasks.md must-not).
    """
    boundary = _trusted_boundary()
    headers = _ar_header("mx.google.com; spf=pass dkim=pass dmarc=pass")

    result = classify_authentication(headers, boundary=boundary)

    for obs in (
        result.authentication.spf,
        result.authentication.dkim,
        result.authentication.dmarc,
        result.authentication.arc,
    ):
        assert obs.provenance is not AuthProvenance.INDEPENDENT


# =============================================================================
# (2) PROVIDER_REPORTED — AR from an authserv-id inside the trusted boundary
# =============================================================================


def test_provider_reported_inside_trusted_boundary() -> None:
    """An AR header whose authserv-id IS inside the trusted boundary is
    PROVIDER_REPORTED with the parsed state — explicitly NOT independent
    (R-002.4, R-004.2).
    """
    boundary = _trusted_boundary()
    headers = _ar_header(
        "mx.google.com; spf=pass smtp.mailfrom=alice@example.com; "
        "dkim=pass header.d=example.com; dmarc=pass"
    )

    result = classify_authentication(headers, boundary=boundary)

    for axis in (
        result.authentication.spf,
        result.authentication.dkim,
        result.authentication.dmarc,
    ):
        assert axis.result is AuthResult.PASS
        assert axis.provenance is AuthProvenance.PROVIDER_REPORTED
        assert axis.provenance is not AuthProvenance.INDEPENDENT
    # ARC not asserted => stays UNKNOWN / not evaluated.
    assert result.authentication.arc.result is AuthResult.UNKNOWN


def test_provider_reported_preserves_fail_and_neutral_states() -> None:
    """Provenance is independent of result: a provider-reported FAIL stays FAIL,
    and unknown/odd tokens (softfail/temperror) degrade to UNKNOWN, never PASS.
    """
    boundary = _trusted_boundary()
    headers = _ar_header(
        "mx.google.com; spf=softfail; dkim=fail; dmarc=none; arc=temperror"
    )

    result = classify_authentication(headers, boundary=boundary)

    # dkim=fail preserved as FAIL (provider reported).
    assert result.authentication.dkim.result is AuthResult.FAIL
    assert result.authentication.dkim.provenance is AuthProvenance.PROVIDER_REPORTED
    # dmarc=none -> NEUTRAL.
    assert result.authentication.dmarc.result is AuthResult.NEUTRAL
    # spf=softfail and arc=temperror are not a clean pass/fail -> UNKNOWN.
    assert result.authentication.spf.result is AuthResult.UNKNOWN
    assert result.authentication.arc.result is AuthResult.UNKNOWN


# =============================================================================
# (3) LEGACY BODY-ONLY — no auth headers supplied (the current real case)
# =============================================================================


def test_legacy_body_only_all_axes_unknown() -> None:
    """With no auth headers supplied, every axis is UNKNOWN with provenance
    UNKNOWN and a NO_AUTH_EVIDENCE_CAPTURED completeness code (T-28, I-02).
    Never default to PASS or a verified state.
    """
    boundary = _trusted_boundary()

    result = classify_authentication([], boundary=boundary)

    for obs in (
        result.authentication.spf,
        result.authentication.dkim,
        result.authentication.dmarc,
        result.authentication.arc,
    ):
        assert obs.result is AuthResult.UNKNOWN
        assert obs.provenance is AuthProvenance.UNKNOWN
    assert NO_AUTH_EVIDENCE_CAPTURED in result.completeness.checks_not_run
    # No fabricated verified state anywhere.
    assert all(
        obs.provenance is not AuthProvenance.INDEPENDENT
        for obs in (
            result.authentication.spf,
            result.authentication.dkim,
            result.authentication.dmarc,
            result.authentication.arc,
        )
    )


def test_legacy_body_only_without_boundary_still_unknown() -> None:
    """No boundary configured + no headers still yields UNKNOWN (never clean)."""
    result = classify_authentication([], boundary=TrustedReceiverBoundary())
    assert result.authentication.dkim.result is AuthResult.UNKNOWN
    assert result.authentication.dkim.provenance is AuthProvenance.UNKNOWN


# =============================================================================
# (4) COMPROMISED SENDER — DKIM=pass but mismatched Reply-To (I-06, T-02)
# =============================================================================


def test_compromised_sender_dkim_pass_is_not_a_trust_flag() -> None:
    """A provider-reported DKIM=pass plus a mismatched Reply-To does not raise
    any allow/trust state. The auth observation is emitted; the mismatch is a
    separate finding. Content/intent can still override (R-004.4, I-06, T-02).
    """
    boundary = _trusted_boundary()
    headers = _ar_header("mx.google.com; dkim=pass header.d=bank.example")
    addresses = SenderAddresses(
        from_address="ceo@bank.example",
        reply_to="attacker@evil.example",
        return_path="bounce@evil.example",
    )

    result = build_auth_evidence(headers, addresses=addresses, boundary=boundary)

    # Auth observation present and provider-reported (not independent).
    assert result.authentication.dkim.result is AuthResult.PASS
    assert result.authentication.dkim.provenance is AuthProvenance.PROVIDER_REPORTED
    # The result exposes NO allow/trust/pass-through flag at all.
    assert not hasattr(result, "allow")
    assert not hasattr(result, "trusted")
    assert not hasattr(result, "safe")
    # A reply-to mismatch finding is present and independent of the auth pass.
    assert any(
        f.category is FindingCategory.SENDER_IMPERSONATION
        and f.explanation_code == "REPLY_TO_MISMATCH"
        for f in result.findings
    )
    # Return-Path mismatch also recorded.
    assert any(
        f.explanation_code == "RETURN_PATH_MISMATCH" for f in result.findings
    )


# =============================================================================
# (5) HOMOGLYPH / IDNA — confusable From domain (T-03, offline)
# =============================================================================


def test_homoglyph_confusable_from_domain_is_flagged_offline() -> None:
    """A From domain using a Cyrillic look-alike character yields a typed
    confusable observation, computed offline with no network (T-03, R-004.3).
    """
    # "pаypal.com" — the second 'а' is Cyrillic U+0430, not Latin 'a'.
    confusable_from = "security@p\u0430ypal.com"
    addresses = SenderAddresses(from_address=confusable_from)

    findings = inspect_sender_provenance(addresses)

    assert any(
        f.category is FindingCategory.SENDER_IMPERSONATION
        and f.explanation_code == "CONFUSABLE_DOMAIN"
        for f in findings
    )


def test_plain_ascii_domain_is_not_flagged_confusable() -> None:
    """A normal all-ASCII domain raises no confusable finding (no false alarm)."""
    addresses = SenderAddresses(from_address="alice@example.com")
    findings = inspect_sender_provenance(addresses)
    assert not any(
        f.explanation_code == "CONFUSABLE_DOMAIN" for f in findings
    )


def test_punycode_idna_domain_is_flagged() -> None:
    """An xn-- (punycode/IDNA) From domain is surfaced as a confusable
    observation so a non-ASCII registrable domain is never silently trusted.
    """
    addresses = SenderAddresses(from_address="info@xn--pypal-4ve.com")
    findings = inspect_sender_provenance(addresses)
    assert any(
        f.explanation_code == "CONFUSABLE_DOMAIN" for f in findings
    )


# =============================================================================
# (6) MULTIPLE / MALFORMED Authentication-Results — bounded, no crash
# =============================================================================


def test_multiple_authentication_results_headers_bounded() -> None:
    """Multiple AR headers (trusted + forged) are each classified by their own
    authserv-id; the trusted one is provider-reported, the forged one untrusted.
    No crash, bounded typed handling.
    """
    boundary = _trusted_boundary()
    headers = [
        ("Authentication-Results", "mx.google.com; spf=pass"),
        ("Authentication-Results", "mx.trusted.example; dkim=pass"),
    ]

    result = classify_authentication(headers, boundary=boundary)

    # spf from the trusted authserv-id is provider-reported.
    assert result.authentication.spf.result is AuthResult.PASS
    assert result.authentication.spf.provenance is AuthProvenance.PROVIDER_REPORTED
    # dkim came only from the forged authserv-id -> untrusted header.
    assert result.authentication.dkim.result is AuthResult.PASS
    assert result.authentication.dkim.provenance is AuthProvenance.UNTRUSTED_HEADER


def test_malformed_authentication_results_does_not_crash() -> None:
    """A garbage/malformed AR value yields bounded UNKNOWN axes and a typed
    note instead of raising (R-003.2 style robustness carried into 1.4).
    """
    boundary = _trusted_boundary()
    headers = _ar_header(";;;===garbage===;;; no authserv id here ::::")

    result = classify_authentication(headers, boundary=boundary)

    # Nothing parsed into a clean pass; axes stay UNKNOWN, no exception.
    assert result.authentication.dkim.result is AuthResult.UNKNOWN
    assert result.authentication.spf.result is AuthResult.UNKNOWN
    assert isinstance(result.findings, list)


def test_empty_authserv_id_is_untrusted_not_provider_reported() -> None:
    """An AR header with a result but no resolvable authserv-id can never be
    provider-reported (we cannot attest the origin) -> UNTRUSTED_HEADER."""
    boundary = _trusted_boundary()
    headers = _ar_header("; dkim=pass")

    result = classify_authentication(headers, boundary=boundary)
    assert result.authentication.dkim.provenance in (
        AuthProvenance.UNTRUSTED_HEADER,
        AuthProvenance.UNKNOWN,
    )
    assert result.authentication.dkim.provenance is not AuthProvenance.PROVIDER_REPORTED


# =============================================================================
# structural / metadata guarantees
# =============================================================================


def test_version_and_result_shape() -> None:
    """The module exposes a stable version and a well-formed typed result."""
    assert isinstance(AUTH_EVIDENCE_VERSION, str) and AUTH_EVIDENCE_VERSION
    result = classify_authentication([], boundary=TrustedReceiverBoundary())
    # authentication serializes through the strict pydantic model.
    dumped = result.authentication.model_dump()
    assert set(dumped) == {"spf", "dkim", "dmarc", "arc"}


def test_trusted_boundary_is_case_insensitive_and_frozen() -> None:
    """authserv-id matching is case-insensitive; the boundary is immutable."""
    boundary = TrustedReceiverBoundary(authserv_ids=["MX.Google.COM"])
    headers = _ar_header("mx.google.com; dkim=pass")
    result = classify_authentication(headers, boundary=boundary)
    assert result.authentication.dkim.provenance is AuthProvenance.PROVIDER_REPORTED
    with pytest.raises((AttributeError, TypeError)):
        boundary.authserv_ids.add("other")  # type: ignore[attr-defined]


def test_findings_carry_detector_version_and_no_raw_text() -> None:
    """Every emitted finding carries the detector version and only safe codes
    (I-05: no raw addresses in explanation codes)."""
    boundary = _trusted_boundary()
    headers = _ar_header("mx.trusted.example; dkim=pass")
    result = classify_authentication(headers, boundary=boundary)
    for finding in result.findings:
        assert finding.detector_version == AUTH_EVIDENCE_VERSION
        assert finding.status in set(FindingStatus)
        # explanation_code matches the safe Code pattern (upper snake).
        assert finding.explanation_code.isupper() or "_" in finding.explanation_code
