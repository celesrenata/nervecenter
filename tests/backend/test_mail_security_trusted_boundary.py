"""Topmost-only trusted receiver boundary (NS-ADR-070).

Gmail prepends its own ``Authentication-Results: mx.google.com`` above every
header the sender wrote. Anything further down is attacker-writable, so even
an exact ``mx.google.com`` authserv-id there must stay UNTRUSTED_HEADER.
"""
from __future__ import annotations

import pytest

from backend.mail_security.auth_evidence import (
    ENV_TRUSTED_AUTHSERV_IDS,
    TrustedReceiverBoundary,
    boundary_from_env,
    classify_authentication,
)
from backend.mail_security.models import AuthProvenance, AuthResult

GMAIL = TrustedReceiverBoundary(["mx.google.com"], topmost_only=True)
AR = "Authentication-Results"


def _axes(headers):
    res = classify_authentication(headers, boundary=GMAIL)
    a = res.authentication
    axes = {k: (getattr(a, k).result, getattr(a, k).provenance) for k in ("spf", "dkim", "dmarc")}
    return axes, {f.explanation_code for f in res.findings}


def test_topmost_provider_result_is_provider_reported() -> None:
    axes, codes = _axes([
        ("Delivered-To", "me@example.com"),
        (AR, "mx.google.com; dkim=pass header.i=@bank.example; spf=pass; dmarc=pass"),
        ("From", "Bank <no-reply@bank.example>"),
    ])
    assert axes["dkim"] == (AuthResult.PASS, AuthProvenance.PROVIDER_REPORTED)
    assert axes["dmarc"] == (AuthResult.PASS, AuthProvenance.PROVIDER_REPORTED)
    assert "AUTH_RESULTS_UNTRUSTED_ORIGIN" not in codes


def test_forged_lower_header_with_same_authserv_id_stays_untrusted() -> None:
    """The real (topmost) result says fail; a sender-written mx.google.com
    'pass' further down neither upgrades it nor counts as provider-reported."""
    axes, codes = _axes([
        (AR, "mx.google.com; dkim=fail; spf=softfail; dmarc=fail"),
        ("From", "Bank <no-reply@bank.example>"),
        (AR, "mx.google.com; dkim=pass; spf=pass; dmarc=pass"),
    ])
    assert axes["dmarc"] == (AuthResult.FAIL, AuthProvenance.PROVIDER_REPORTED)
    assert axes["dkim"] == (AuthResult.FAIL, AuthProvenance.PROVIDER_REPORTED)
    assert "AUTH_RESULTS_UNTRUSTED_ORIGIN" in codes
    assert "AUTH_PROVIDER_REPORTED_DMARC_FAIL" in codes


def test_forged_header_alone_below_another_receiver_is_untrusted() -> None:
    axes, _ = _axes([
        (AR, "relay.other.example; spf=pass"),
        (AR, "mx.google.com; dkim=pass; dmarc=pass"),
    ])
    assert axes["dkim"] == (AuthResult.PASS, AuthProvenance.UNTRUSTED_HEADER)
    assert axes["dmarc"][1] is AuthProvenance.UNTRUSTED_HEADER


def test_never_independent_and_legacy_boundary_unchanged() -> None:
    axes, _ = _axes([(AR, "mx.google.com; dkim=pass; spf=pass; dmarc=pass")])
    assert all(p is not AuthProvenance.INDEPENDENT for _r, p in axes.values())
    # Without topmost_only (the historical behavior) any position is attested.
    legacy = TrustedReceiverBoundary(["mx.google.com"])
    res = classify_authentication(
        [(AR, "other; spf=pass"), (AR, "mx.google.com; dkim=pass")], boundary=legacy
    )
    assert res.authentication.dkim.provenance is AuthProvenance.PROVIDER_REPORTED


@pytest.mark.parametrize(
    ("raw", "ids"),
    [
        ("", frozenset()),
        ("mx.google.com", frozenset({"mx.google.com"})),
        (" MX.Google.com , mx2.example ", frozenset({"mx.google.com", "mx2.example"})),
        ("mx.google.com; dkim=pass", frozenset()),  # malformed -> empty, safe
        ("bad id", frozenset()),
    ],
)
def test_boundary_from_env(raw: str, ids: frozenset[str]) -> None:
    b = boundary_from_env({ENV_TRUSTED_AUTHSERV_IDS: raw})
    assert b.authserv_ids == ids
    assert b.topmost_only is bool(ids)
