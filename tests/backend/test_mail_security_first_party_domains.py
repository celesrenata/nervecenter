"""First-party domain precision for the ASCII typosquat rule (NS-ADR-026).

Both directions: legitimate first-party hosts must NOT be flagged, and
lookalikes must still be flagged. Offline, pure, no I/O.
"""

from __future__ import annotations

import pytest

from backend.mail_security import inspections
from backend.mail_security.inspections import (
    _BRAND_OWNED_TLDS,
    _FIRST_PARTY_REGISTRABLE_DOMAINS,
    _KNOWN_BRAND_DOMAINS,
    INSPECTIONS_VERSION,
    _is_ascii_typosquat,
    _is_first_party_host,
    _is_subdomain_brand_deception,
)

FIRST_PARTY = [
    "applecard.cdn-apple.com",  # observed on real mail
    "email.post.applecard.apple",  # observed on real mail
    "cdn-apple.com",
    "apple.com",
    "www.apple.com",
    "icloud.com",
    "p01-mail.icloud.com",
    "is1-ssl.mzstatic.com",
    "applecard.apple",
    "apple.cdn-apple.com",
    "paypal.com",
    "www.google.com",
    "login.microsoft.com",
    "something.google",
    "shop.amazon",
]

LOOKALIKES = [
    "apple-support-login.com",
    "paypa1.com",
    "cdn-apple.com.evil.test",
    "secure-cdn-apple.com",
    "evilcdn-apple.com",
    "applecard.cdn-apple.com.attacker.net",
    "cdn-apple.com-login.net",
    "icloud.com.evil.test",
    "apple.co",
    "paypal.co",
    "g00gle.com",
    "applecard.apple.evil.test",
]


@pytest.mark.parametrize("host", FIRST_PARTY)
def test_first_party_hosts_not_flagged(host: str) -> None:
    assert _is_first_party_host(host)
    assert not _is_ascii_typosquat(host)
    assert not _is_subdomain_brand_deception(host)


@pytest.mark.parametrize("host", LOOKALIKES)
def test_lookalikes_still_flagged(host: str) -> None:
    assert not _is_first_party_host(host)
    assert _is_ascii_typosquat(host) or _is_subdomain_brand_deception(host)


@pytest.mark.parametrize("host", ["xn--cdn-apple-9ib.com", "xn--pple-43d.apple", "аpple.apple"])
def test_punycode_and_unicode_never_first_party(host: str) -> None:
    assert not _is_first_party_host(host)


def test_unicode_lookalike_still_gets_confusable_finding() -> None:
    findings = inspections._domain_deception_findings(  # noqa: SLF001
        "аpple.com", finding_key_prefix="t", evidence_ref="ref"
    )
    assert [f.rule_id for f in findings] == ["DOMAIN_CONFUSABLE_IDNA"]


@pytest.mark.parametrize("brand", sorted(_KNOWN_BRAND_DOMAINS))
def test_hyphen_prefix_and_suffix_forms_flagged_for_every_brand(brand: str) -> None:
    for host in (f"{brand}-support.com", f"secure-{brand}.com", f"{brand}-login.net"):
        assert _is_ascii_typosquat(host), host
    assert not _is_ascii_typosquat(_KNOWN_BRAND_DOMAINS[brand])
    assert _is_ascii_typosquat(f"{brand}.co")


def test_observed_host_yields_no_finding_end_to_end() -> None:
    findings = inspections._domain_deception_findings(  # noqa: SLF001
        "applecard.cdn-apple.com", finding_key_prefix="t", evidence_ref="ref"
    )
    assert findings == []


def test_allowlist_data_is_well_formed() -> None:
    assert _FIRST_PARTY_REGISTRABLE_DOMAINS["apple"] >= {
        "icloud.com",
        "cdn-apple.com",
        "mzstatic.com",
    }
    assert set(_FIRST_PARTY_REGISTRABLE_DOMAINS) <= set(_KNOWN_BRAND_DOMAINS)
    assert set(_BRAND_OWNED_TLDS) <= set(_KNOWN_BRAND_DOMAINS)
    for domains in _FIRST_PARTY_REGISTRABLE_DOMAINS.values():
        assert all(d == d.lower() and d.count(".") == 1 for d in domains)


def test_detector_version_bumped() -> None:
    assert INSPECTIONS_VERSION == "offline-inspections@2"
