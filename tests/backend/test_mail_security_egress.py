"""Tests for the NerveShield egress policy engine/client (Phase 2, Task 2.5).

Deterministic: fake resolver, fake transport, fake clock. No real DNS, sockets
or network. Invariants: R-005.2/.3, R-017.3, I-02, I-04, T-05, T-09 (SSRF /
DNS rebinding -> network off by default, private/loopback/DNS rotation denied).
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from structlog.testing import capture_logs

from backend.mail_security import egress
from backend.mail_security.egress import (
    ConsentGrant,
    EgressClient,
    EgressPolicy,
    EgressPolicyError,
    EgressPurpose,
    EgressReason,
    EgressStatus,
    InMemoryAuditSink,
    TransportRequest,
    TransportResponse,
    is_address_blocked,
    parse_ip,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
PUBLIC = "93.184.216.34"
PUBLIC2 = "93.184.216.35"
MAIL = EgressPurpose.MAIL_URL_REPUTATION
FEED = EgressPurpose.PUBLIC_FEED
ACCT = "acct-1"
CANARY = "CANARY9f3a"


class FakeClock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class FakeResolver:
    """Maps host -> list of answers; a list of lists yields per call."""

    def __init__(self, mapping: dict[str, object] | None = None, default: object = None) -> None:
        self.mapping = mapping or {}
        self.default = default
        self.calls: list[str] = []

    async def resolve(self, host: str):
        self.calls.append(host)
        value = self.mapping.get(host, self.default)
        if value is None:
            raise OSError(f"nxdomain {CANARY}")
        if value and isinstance(value[0], list):  # type: ignore[index]
            n = sum(1 for h in self.calls if h == host) - 1
            return value[min(n, len(value) - 1)]  # type: ignore[index]
        return value


class FakeTransport:
    def __init__(self, responses: list[TransportResponse] | None = None) -> None:
        self.responses = responses or [TransportResponse(200, b"ok")]
        self.requests: list[TransportRequest] = []

    async def fetch(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        i = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[i]


def grant(account: str = ACCT, expires: datetime | None = None, purpose=MAIL) -> ConsentGrant:
    return ConsentGrant(
        approval_ref="appr-1",
        account_id=account,
        purpose=purpose,
        expires_at=expires or T0 + timedelta(days=1),
    )


def policy(**kw) -> EgressPolicy:
    base: dict = {
        "enabled": True,
        "consent_grants": (grant(),),
        "host_allow": ("api.example.com", ".example.org"),
    }
    base.update(kw)
    return EgressPolicy(**base)


def make(
    pol: EgressPolicy | None = None,
    resolver: FakeResolver | None = None,
    transport: FakeTransport | None = None,
    clock: FakeClock | None = None,
    master: bool = True,
    audit: InMemoryAuditSink | None = None,
):
    resolver = resolver or FakeResolver(default=[PUBLIC])
    transport = transport or FakeTransport()
    client = EgressClient(
        pol or policy(),
        transport=transport,
        resolver=resolver,
        audit=audit,
        now=clock or FakeClock(),
        master_enabled=lambda: master,
    )
    return client, resolver, transport


async def mail(client: EgressClient, url: str, account: str | None = ACCT):
    return await client.fetch(url, purpose=MAIL, account_id=account)


# --------------------------------------------------------------------------
# Defaults: deny everything, transport never called
# --------------------------------------------------------------------------


def test_default_policy_is_deny_all() -> None:
    p = EgressPolicy()
    assert p.enabled is False
    assert p.consent_grants == ()
    assert p.allowed_schemes == ("https",)
    assert p.allowed_ports == (443,)
    assert p.host_allow == ()
    assert p.max_redirects == 3


async def test_default_config_refuses_and_never_calls_transport(monkeypatch) -> None:
    monkeypatch.delenv("NERVE_MAIL_SECURITY_ENABLED", raising=False)
    resolver, transport = FakeResolver(default=[PUBLIC]), FakeTransport()
    client = EgressClient(EgressPolicy(), transport=transport, resolver=resolver)
    for purpose in (MAIL, FEED):
        r = await client.fetch("https://api.example.com/", purpose=purpose, account_id=ACCT)
        assert r.status is EgressStatus.EGRESS_DENIED
        assert r.evaluated is False
    assert transport.requests == [] and resolver.calls == []


async def test_master_flag_off_denies_even_with_enabled_policy(monkeypatch) -> None:
    monkeypatch.delenv("NERVE_MAIL_SECURITY_ENABLED", raising=False)
    resolver, transport = FakeResolver(default=[PUBLIC]), FakeTransport()
    client = EgressClient(policy(), transport=transport, resolver=resolver)
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.MASTER_DISABLED
    assert transport.requests == [] and resolver.calls == []


async def test_master_flag_off_explicit() -> None:
    client, resolver, transport = make(master=False)
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.MASTER_DISABLED
    assert transport.requests == [] and resolver.calls == []


async def test_policy_disabled_denies() -> None:
    client, resolver, transport = make(policy(enabled=False))
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.EGRESS_DISABLED
    assert transport.requests == [] and resolver.calls == []


async def test_enabled_with_empty_allowlist_denies_everything() -> None:
    client, resolver, transport = make(policy(host_allow=()))
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.HOST_NOT_ALLOWED
    assert transport.requests == [] and resolver.calls == []


async def test_invalid_purpose_denied() -> None:
    client, _, transport = make()
    r = await client.fetch("https://api.example.com/", purpose="mail", account_id=ACCT)  # type: ignore[arg-type]
    assert r.reason is EgressReason.INVALID_PURPOSE
    assert transport.requests == []


# --------------------------------------------------------------------------
# Consent
# --------------------------------------------------------------------------


async def test_consent_missing_denied() -> None:
    client, resolver, transport = make(policy(consent_grants=()))
    r = await mail(client, "https://api.example.com/")
    assert (r.status, r.reason) == (EgressStatus.EGRESS_DENIED, EgressReason.CONSENT_MISSING)
    assert transport.requests == [] and resolver.calls == []


async def test_consent_expired_denied() -> None:
    clock = FakeClock()
    client, _, transport = make(
        policy(consent_grants=(grant(expires=T0 + timedelta(hours=1)),)), clock=clock
    )
    clock.advance(3601)
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.CONSENT_EXPIRED
    assert transport.requests == []


async def test_consent_other_account_denied() -> None:
    client, _, transport = make(policy(consent_grants=(grant(account="other"),)))
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.CONSENT_MISSING
    assert transport.requests == []


async def test_mail_requires_account() -> None:
    client, _, transport = make()
    r = await mail(client, "https://api.example.com/", account=None)
    assert r.reason is EgressReason.ACCOUNT_REQUIRED
    assert transport.requests == []


def test_consent_is_purpose_bound() -> None:
    # A grant can only be for the mail purpose; a feed grant is not constructible,
    # so consent for one purpose can never be reused for another.
    with pytest.raises(EgressPolicyError):
        grant(purpose=FEED)


def test_consent_validation() -> None:
    with pytest.raises(EgressPolicyError):
        ConsentGrant("bad ref!", ACCT, MAIL, T0 + timedelta(days=1))
    with pytest.raises(EgressPolicyError):
        ConsentGrant("ok", "", MAIL, T0 + timedelta(days=1))
    with pytest.raises(EgressPolicyError):
        ConsentGrant("ok", ACCT, MAIL, datetime(2030, 1, 1))  # naive


async def test_consent_valid_but_host_not_allowlisted_denied() -> None:
    client, resolver, transport = make()
    r = await mail(client, "https://other.example.net/")
    assert r.reason is EgressReason.HOST_NOT_ALLOWED
    assert transport.requests == [] and resolver.calls == []


async def test_feed_purpose_needs_no_consent_but_needs_allowlist() -> None:
    client, _, transport = make(policy(consent_grants=()))
    ok = await client.fetch("https://api.example.com/feed.json?v=1", purpose=FEED)
    assert ok.status is EgressStatus.OK
    assert transport.requests[0].path == "/feed.json" and transport.requests[0].query == "v=1"
    bad = await client.fetch("https://nope.example.net/feed.json", purpose=FEED)
    assert bad.reason is EgressReason.HOST_NOT_ALLOWED


# --------------------------------------------------------------------------
# Allowed path: pinned IP, limits passed through, minimization
# --------------------------------------------------------------------------


async def test_allowed_path_pins_ip_and_passes_limits() -> None:
    pol = policy(connect_timeout=2.0, read_timeout=4.0, total_timeout=9.0, max_response_bytes=1234)
    client, resolver, transport = make(pol)
    r = await mail(client, "https://API.Example.COM./")
    assert r.status is EgressStatus.OK and r.evaluated and r.body == b"ok"
    (req,) = transport.requests
    assert req.pinned_ip == PUBLIC
    assert (req.scheme, req.host, req.port) == ("https", "api.example.com", 443)
    assert (req.connect_timeout, req.read_timeout, req.max_response_bytes) == (2.0, 4.0, 1234)
    assert resolver.calls == ["api.example.com", "api.example.com"]


async def test_mail_url_minimized_query_fragment_path_never_reach_transport() -> None:
    client, _, transport = make()
    r = await mail(client, f"https://api.example.com/p/{CANARY}/x?token={CANARY}&u=1#{CANARY}")
    assert r.status is EgressStatus.OK
    (req,) = transport.requests
    assert (req.path, req.query) == ("/", "")
    assert CANARY not in repr(req)


@pytest.mark.parametrize(
    "url",
    [
        f"https://{CANARY}:pw@api.example.com/",
        f"https://{CANARY}@api.example.com/",
        f"https://api.example.com@evil.example.org/{CANARY}",
        f"https://evil.example.org\\@api.example.com/{CANARY}",
        f"https://api.example.com:443@evil.example.org/{CANARY}",
    ],
)
async def test_userinfo_denied_and_never_reaches_transport(url: str) -> None:
    sink = InMemoryAuditSink()
    client, resolver, transport = make(audit=sink)
    with capture_logs() as logs:
        r = await mail(client, url)
    assert r.status is EgressStatus.EGRESS_DENIED
    assert transport.requests == [] and resolver.calls == []
    assert CANARY not in repr(sink.records) and CANARY not in repr(r) and CANARY not in repr(logs)


# --------------------------------------------------------------------------
# Lexical URL / host tricks (denied before any resolution)
# --------------------------------------------------------------------------

LEXICAL_CASES = [
    # numeric host tricks
    ("https://2130706433/", EgressReason.NUMERIC_HOST),  # decimal
    ("https://0177.0.0.1/", EgressReason.NUMERIC_HOST),  # octal
    ("https://0x7f.0.0.1/", EgressReason.NUMERIC_HOST),  # hex
    ("https://0x7f000001/", EgressReason.NUMERIC_HOST),  # hex single
    ("https://0x7f.1/", EgressReason.NUMERIC_HOST),  # mixed + short
    ("https://127.1/", EgressReason.NUMERIC_HOST),  # shortened
    ("https://127.0.1/", EgressReason.NUMERIC_HOST),
    ("https://017700000001/", EgressReason.NUMERIC_HOST),
    ("https://127.0.0.1/", EgressReason.IP_LITERAL),
    ("https://93.184.216.34/", EgressReason.IP_LITERAL),
    ("https://169.254.169.254/latest/meta-data/", EgressReason.IP_LITERAL),
    ("https://[::1]/", EgressReason.IP_LITERAL),
    ("https://[::ffff:127.0.0.1]/", EgressReason.IP_LITERAL),
    ("https://[fe80::1%25eth0]/", EgressReason.IP_LITERAL),
    # non-ASCII digits / IDN confusion
    ("https://\uff11\uff12\uff17.0.0.1/", EgressReason.URL_NON_ASCII),
    ("https://api.exampl\u0435.com/", EgressReason.URL_NON_ASCII),
    ("https://xn--pple-43d.example.org/", EgressReason.HOST_CONFUSABLE),
    # whitespace / backslash / control tricks
    ("https://api.example.com\\@evil.example.org/", EgressReason.URL_BACKSLASH_OR_WHITESPACE),
    ("https://api.example.com\\/x", EgressReason.URL_BACKSLASH_OR_WHITESPACE),
    (" https://api.example.com/", EgressReason.URL_BACKSLASH_OR_WHITESPACE),
    ("https://api.example.com/ x", EgressReason.URL_BACKSLASH_OR_WHITESPACE),
    ("https://api.example.com/\tx", EgressReason.URL_BACKSLASH_OR_WHITESPACE),
    ("https://api.example.com/\r\nHost: x", EgressReason.URL_BACKSLASH_OR_WHITESPACE),
    ("https://api.exa\x00mple.com/", EgressReason.URL_BACKSLASH_OR_WHITESPACE),
    # host shape
    ("https://api..example.com/", EgressReason.HOST_INVALID),
    ("https://-api.example.com/", EgressReason.HOST_INVALID),
    ("https://api_x.example.com/", EgressReason.HOST_INVALID),
    ("https://" + "a" * 64 + ".example.org/", EgressReason.HOST_INVALID),
    ("https://" + ".".join(["a" * 60] * 5) + ".org/", EgressReason.HOST_INVALID),
    ("https://api%2eexample.com/", EgressReason.HOST_INVALID),
    ("https:///", EgressReason.HOST_INVALID),
    ("https://:443/", EgressReason.HOST_INVALID),
    ("https://api.example.com:443:80/", EgressReason.HOST_INVALID),
    ("https://localhost/", EgressReason.HOST_BLOCKED),
    ("https://app.localhost/", EgressReason.HOST_BLOCKED),
    ("https://printer.local/", EgressReason.HOST_BLOCKED),
    ("https://db.internal/", EgressReason.HOST_BLOCKED),
    ("https://intranet/", EgressReason.HOST_BLOCKED),
    # scheme / port
    ("http://api.example.com/", EgressReason.SCHEME_NOT_ALLOWED),
    ("ftp://api.example.com/", EgressReason.SCHEME_NOT_ALLOWED),
    ("file:///etc/passwd", EgressReason.SCHEME_NOT_ALLOWED),
    ("gopher://api.example.com/", EgressReason.SCHEME_NOT_ALLOWED),
    ("javascript:alert(1)", EgressReason.URL_INVALID),
    ("//api.example.com/", EgressReason.URL_INVALID),
    ("api.example.com", EgressReason.URL_INVALID),
    ("", EgressReason.URL_INVALID),
    ("https://api.example.com:8443/", EgressReason.PORT_NOT_ALLOWED),
    ("https://api.example.com:22/", EgressReason.PORT_NOT_ALLOWED),
    ("https://api.example.com:/", EgressReason.PORT_NOT_ALLOWED),
    ("https://api.example.com:0443/x", EgressReason.PORT_NOT_ALLOWED),
    ("https://api.example.com:\u0664\u0664\u0663/", EgressReason.URL_NON_ASCII),
    ("https://api.example.com:99999999/", EgressReason.PORT_NOT_ALLOWED),
    # allow/deny
    ("https://evil.example.net/", EgressReason.HOST_NOT_ALLOWED),
    ("https://example.com/", EgressReason.HOST_NOT_ALLOWED),
    ("https://api.example.com.evil.net/", EgressReason.HOST_NOT_ALLOWED),
    ("https://xapi.example.com/", EgressReason.HOST_NOT_ALLOWED),
    ("https://evilexample.org/", EgressReason.HOST_NOT_ALLOWED),
    ("https://example.org/", EgressReason.HOST_NOT_ALLOWED),  # suffix rule is subdomains only
    # fragment / query tricks keep the real authority
    ("https://evil.example.net#@api.example.com/", EgressReason.HOST_NOT_ALLOWED),
    ("https://evil.example.net?@api.example.com/", EgressReason.HOST_NOT_ALLOWED),
    ("https://evil.example.net/#.api.example.com", EgressReason.HOST_NOT_ALLOWED),
    ("https://evil.example.net/?x=api.example.com", EgressReason.HOST_NOT_ALLOWED),
]


@pytest.mark.parametrize(("url", "reason"), LEXICAL_CASES)
async def test_lexical_matrix_denied_before_resolution(url: str, reason: EgressReason) -> None:
    client, resolver, transport = make()
    r = await mail(client, url)
    assert r.status is EgressStatus.EGRESS_DENIED
    assert r.reason is reason
    assert r.evaluated is False
    assert resolver.calls == [] and transport.requests == []  # R-005.2: offline first


async def test_url_too_long_denied() -> None:
    client, resolver, transport = make(policy(max_url_length=64))
    r = await mail(client, "https://api.example.com/" + "a" * 100)
    assert r.reason is EgressReason.URL_TOO_LONG
    assert resolver.calls == [] and transport.requests == []


async def test_non_string_url_denied() -> None:
    client, _, transport = make()
    r = await mail(client, None)  # type: ignore[arg-type]
    assert r.reason is EgressReason.URL_INVALID
    assert transport.requests == []


async def test_mixed_case_and_trailing_dot_canonicalized_not_bypassing_deny() -> None:
    client, _, transport = make(policy(host_deny=("api.example.com",)))
    r = await mail(client, "https://API.EXAMPLE.COM./")
    assert r.reason is EgressReason.HOST_DENIED
    assert transport.requests == []


async def test_deny_list_overrides_allow_suffix() -> None:
    client, _, transport = make(policy(host_deny=(".bad.example.org",)))
    r = await mail(client, "https://x.bad.example.org/")
    assert r.reason is EgressReason.HOST_DENIED
    assert transport.requests == []


async def test_http_allowed_only_when_explicitly_configured() -> None:
    client, _, transport = make(policy(allowed_schemes=("https", "http"), allowed_ports=(443, 80)))
    r = await mail(client, "http://api.example.com/")
    assert r.status is EgressStatus.OK
    assert transport.requests[0].port == 80 and transport.requests[0].scheme == "http"


# --------------------------------------------------------------------------
# Address matrix (resolved addresses)
# --------------------------------------------------------------------------

BLOCKED_ADDRESSES = [
    "127.0.0.1", "127.255.255.254", "10.0.0.1", "10.255.255.255", "172.16.0.1",
    "172.31.255.255", "192.168.1.1", "169.254.169.254", "169.254.0.1", "100.64.0.1",
    "100.127.255.254", "224.0.0.1", "239.255.255.255", "240.0.0.1", "255.255.255.255",
    "0.0.0.0", "0.1.2.3", "192.0.0.1", "192.0.2.1", "198.18.0.1", "198.51.100.1",
    "203.0.113.1",
    "::1", "::", "fc00::1", "fd12:3456::1", "fe80::1", "fec0::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "::ffff:93.184.216.34",
    "::127.0.0.1", "::10.0.0.1", "::7f00:1",
    "64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b::5db8:d822",
    "2002:7f00:1::", "2002:0a00:0001::1", "2002:a9fe:a9fe::1",
    "2001::1", "2001:db8::1", "100::1", "3fff::1", "4000::1",
]  # fmt: skip


@pytest.mark.parametrize("addr", BLOCKED_ADDRESSES)
async def test_blocked_resolved_addresses_denied(addr: str) -> None:
    client, _, transport = make(resolver=FakeResolver(default=[addr]))
    r = await mail(client, "https://api.example.com/")
    assert r.status is EgressStatus.EGRESS_DENIED
    assert r.reason is EgressReason.ADDRESS_BLOCKED
    assert transport.requests == []
    parsed = parse_ip(addr)
    assert parsed is not None and is_address_blocked(parsed)


@pytest.mark.parametrize(
    "addr", [PUBLIC, "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "2a00:1450:4001::1"]
)
def test_public_addresses_not_blocked(addr: str) -> None:
    parsed = parse_ip(addr)
    assert parsed is not None and not is_address_blocked(parsed)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " 93.184.216.34",
        "93.184.216.34 ",
        "fe80::1%eth0",
        "093.184.216.34",
        "0x5d.0.0.1",
        "abc",
        "1.2.3",
        "\uff11.1.1.1",
        None,
        7,
    ],
)
async def test_malformed_resolver_answers_denied(bad) -> None:
    client, _, transport = make(resolver=FakeResolver(default=[bad]))
    r = await mail(client, "https://api.example.com/")
    assert r.status is EgressStatus.EGRESS_DENIED and r.reason is EgressReason.ADDRESS_INVALID
    assert transport.requests == []


async def test_mixed_public_and_private_answer_denied() -> None:
    for answer in (
        [PUBLIC, "10.0.0.5"],
        ["10.0.0.5", PUBLIC],
        [PUBLIC, PUBLIC2, "::1"],
        [PUBLIC, "169.254.169.254"],
    ):
        client, _, transport = make(resolver=FakeResolver(default=answer))
        r = await mail(client, "https://api.example.com/")
        assert r.reason is EgressReason.ADDRESS_BLOCKED
        assert transport.requests == []


async def test_resolver_error_and_empty_are_failed_not_clean() -> None:
    client, _, transport = make(resolver=FakeResolver(default=None))
    r = await mail(client, "https://api.example.com/")
    assert (r.status, r.reason) == (EgressStatus.EGRESS_FAILED, EgressReason.DNS_FAILED)
    assert r.evaluated is False
    client, _, transport = make(resolver=FakeResolver(default=[]))
    r = await mail(client, "https://api.example.com/")
    assert (r.status, r.reason) == (EgressStatus.EGRESS_FAILED, EgressReason.DNS_NO_ADDRESS)
    assert transport.requests == []


async def test_too_many_addresses_denied() -> None:
    client, _, transport = make(resolver=FakeResolver(default=[f"93.184.{i}.1" for i in range(40)]))
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.ADDRESS_INVALID
    assert transport.requests == []


async def test_ip_allow_further_restricts_and_never_overrides_builtin_blocks() -> None:
    client, _, transport = make(policy(ip_allow=("93.184.216.0/24",)))
    assert (await mail(client, "https://api.example.com/")).status is EgressStatus.OK
    client, _, transport = make(
        policy(ip_allow=("93.184.216.0/24",)), resolver=FakeResolver(default=["8.8.8.8"])
    )
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.ADDRESS_NOT_ALLOWED and transport.requests == []
    client, _, transport = make(
        policy(ip_allow=("10.0.0.0/8",)), resolver=FakeResolver(default=["10.0.0.5"])
    )
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.ADDRESS_BLOCKED and transport.requests == []


async def test_ip_deny_list() -> None:
    client, _, transport = make(policy(ip_deny=("93.184.216.0/24",)))
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.ADDRESS_BLOCKED and transport.requests == []


# --------------------------------------------------------------------------
# Rebinding / TOCTOU
# --------------------------------------------------------------------------


async def test_rebinding_public_then_private_denied() -> None:
    resolver = FakeResolver({"api.example.com": [[PUBLIC], ["10.0.0.5"]]})
    client, _, transport = make(resolver=resolver)
    r = await mail(client, "https://api.example.com/")
    assert r.status is EgressStatus.EGRESS_DENIED
    assert r.reason is EgressReason.ADDRESS_BLOCKED
    assert transport.requests == []


async def test_dns_rotation_between_public_addresses_denied() -> None:
    resolver = FakeResolver({"api.example.com": [[PUBLIC], [PUBLIC2]]})
    client, _, transport = make(resolver=resolver)
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.DNS_ROTATION
    assert transport.requests == []


async def test_pinned_ip_is_the_validated_ip_passed_to_transport() -> None:
    resolver = FakeResolver({"api.example.com": [PUBLIC, PUBLIC2]})
    client, _, transport = make(resolver=resolver)
    await mail(client, "https://api.example.com/")
    assert transport.requests[0].pinned_ip in (PUBLIC, PUBLIC2)
    assert transport.requests[0].host == "api.example.com"  # SNI/Host, not the IP


async def test_redirect_revalidates_and_rebinding_on_second_hop_denied() -> None:
    resolver = FakeResolver(
        {"api.example.com": [PUBLIC], "b.example.org": [[PUBLIC], ["127.0.0.1"]]}
    )
    transport = FakeTransport([TransportResponse(302, location="https://b.example.org/")])
    client, _, _ = make(resolver=resolver, transport=transport)
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.ADDRESS_BLOCKED
    assert r.status is EgressStatus.EGRESS_DENIED
    assert len(transport.requests) == 1  # never connected to the second hop


# --------------------------------------------------------------------------
# Redirects
# --------------------------------------------------------------------------


async def test_redirect_followed_with_revalidation_and_pin() -> None:
    resolver = FakeResolver({"api.example.com": [PUBLIC], "b.example.org": [PUBLIC2]})
    transport = FakeTransport(
        [TransportResponse(301, location="https://b.example.org/"), TransportResponse(200, b"done")]
    )
    client, _, _ = make(resolver=resolver, transport=transport)
    r = await mail(client, "https://api.example.com/")
    assert r.status is EgressStatus.OK and r.hops == 1 and r.body == b"done"
    assert [q.pinned_ip for q in transport.requests] == [PUBLIC, PUBLIC2]
    assert [q.host for q in transport.requests] == ["api.example.com", "b.example.org"]


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("https://10.0.0.5/", EgressReason.REDIRECT_INVALID),  # IP literal
        ("https://127.0.0.1:443/", EgressReason.REDIRECT_INVALID),
        ("https://2130706433/", EgressReason.REDIRECT_INVALID),
        ("http://b.example.org/", EgressReason.REDIRECT_INVALID),  # http not in scheme allowlist
        ("https://evil.example.net/", EgressReason.HOST_NOT_ALLOWED),
        ("https://b.example.org:8443/", EgressReason.REDIRECT_INVALID),
        ("//b.example.org/", EgressReason.REDIRECT_INVALID),
        ("relative/path", EgressReason.REDIRECT_INVALID),
        ("https://user@b.example.org/", EgressReason.REDIRECT_INVALID),
        ("file:///etc/passwd", EgressReason.REDIRECT_INVALID),
        ("", EgressReason.REDIRECT_INVALID),
        (None, EgressReason.REDIRECT_INVALID),
    ],
)
async def test_redirect_targets_denied(location, reason: EgressReason) -> None:
    transport = FakeTransport([TransportResponse(302, location=location)])
    client, _, _ = make(transport=transport)
    r = await mail(client, "https://api.example.com/")
    assert r.status is EgressStatus.EGRESS_DENIED and r.reason is reason
    assert len(transport.requests) == 1


async def test_redirect_to_private_resolving_host_denied() -> None:
    resolver = FakeResolver({"api.example.com": [PUBLIC], "evil.example.org": ["192.168.0.10"]})
    transport = FakeTransport([TransportResponse(302, location="https://evil.example.org/")])
    client, _, _ = make(resolver=resolver, transport=transport)
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.ADDRESS_BLOCKED
    assert len(transport.requests) == 1


async def test_redirect_https_to_http_downgrade_denied_even_if_http_allowed() -> None:
    pol = policy(allowed_schemes=("https", "http"), allowed_ports=(443, 80))
    transport = FakeTransport([TransportResponse(302, location="http://b.example.org/")])
    client, _, _ = make(pol, transport=transport)
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.REDIRECT_DOWNGRADE
    assert len(transport.requests) == 1


async def test_too_many_redirects() -> None:
    transport = FakeTransport(
        [TransportResponse(302, location=f"https://h{i}.example.org/") for i in range(10)]
    )
    client, _, _ = make(policy(max_redirects=2), transport=transport)
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.REDIRECT_TOO_MANY
    assert len(transport.requests) == 3


async def test_zero_redirects_policy() -> None:
    transport = FakeTransport([TransportResponse(302, location="https://b.example.org/")])
    client, _, _ = make(policy(max_redirects=0), transport=transport)
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.REDIRECT_TOO_MANY


async def test_redirect_loop_denied() -> None:
    transport = FakeTransport(
        [
            TransportResponse(302, location="https://b.example.org/"),
            TransportResponse(302, location="https://api.example.com/"),
        ]
    )
    client, _, _ = make(policy(max_redirects=5), transport=transport)
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.REDIRECT_LOOP


async def test_feed_relative_redirect_same_origin_ok() -> None:
    transport = FakeTransport(
        [TransportResponse(302, location="/v2/feed.json"), TransportResponse(200, b"x")]
    )
    client, _, _ = make(transport=transport)
    r = await client.fetch("https://api.example.com/feed.json", purpose=FEED)
    assert r.status is EgressStatus.OK
    assert transport.requests[1].path == "/v2/feed.json"


# --------------------------------------------------------------------------
# Limits: size, timeouts, rate, transport errors
# --------------------------------------------------------------------------


async def test_oversize_response_failed() -> None:
    client, _, _ = make(
        policy(max_response_bytes=4), transport=FakeTransport([TransportResponse(200, b"12345")])
    )
    r = await mail(client, "https://api.example.com/")
    assert (r.status, r.reason) == (EgressStatus.EGRESS_FAILED, EgressReason.RESPONSE_TOO_LARGE)
    assert r.body == b"" and r.evaluated is False


async def test_truncated_flag_failed() -> None:
    client, _, _ = make(transport=FakeTransport([TransportResponse(200, b"x", truncated=True)]))
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.RESPONSE_TOO_LARGE


async def test_transport_error_failed_with_safe_code() -> None:
    class Boom:
        async def fetch(self, request):
            raise RuntimeError(f"secret {CANARY}")

    sink = InMemoryAuditSink()
    client = EgressClient(
        policy(), transport=Boom(), resolver=FakeResolver(default=[PUBLIC]), audit=sink,
        now=FakeClock(), master_enabled=lambda: True,
    )  # fmt: skip
    r = await mail(client, "https://api.example.com/")
    assert (r.status, r.reason) == (EgressStatus.EGRESS_FAILED, EgressReason.TRANSPORT_ERROR)
    assert CANARY not in repr(r) and CANARY not in repr(sink.records)


async def test_bad_transport_response_failed() -> None:
    class Bad:
        async def fetch(self, request):
            return "nope"

    client = EgressClient(
        policy(), transport=Bad(), resolver=FakeResolver(default=[PUBLIC]),  # type: ignore[arg-type]
        now=FakeClock(), master_enabled=lambda: True,
    )  # fmt: skip
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.BAD_RESPONSE


async def test_transport_timeout_total_deadline() -> None:
    class Hang:
        async def fetch(self, request):
            await asyncio.sleep(5)

    client = EgressClient(
        policy(connect_timeout=0.05, read_timeout=0.05, total_timeout=0.1),
        transport=Hang(), resolver=FakeResolver(default=[PUBLIC]),  # type: ignore[arg-type]
        now=FakeClock(), master_enabled=lambda: True,
    )  # fmt: skip
    r = await mail(client, "https://api.example.com/")
    assert (r.status, r.reason) == (EgressStatus.EGRESS_FAILED, EgressReason.TIMEOUT)


async def test_resolver_timeout() -> None:
    class Hang:
        async def resolve(self, host):
            await asyncio.sleep(5)

    transport = FakeTransport()
    client = EgressClient(
        policy(connect_timeout=0.05, read_timeout=0.05, total_timeout=0.1),
        transport=transport, resolver=Hang(),  # type: ignore[arg-type]
        now=FakeClock(), master_enabled=lambda: True,
    )  # fmt: skip
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.TIMEOUT and transport.requests == []


async def test_total_deadline_enforced_with_injected_clock() -> None:
    clock = FakeClock()

    class Slow:
        def __init__(self) -> None:
            self.n = 0

        async def fetch(self, request):
            self.n += 1
            clock.advance(11)  # past total_timeout=10
            return TransportResponse(302, location="https://b.example.org/")

    slow = Slow()
    client = EgressClient(
        policy(), transport=slow, resolver=FakeResolver(default=[PUBLIC]),  # type: ignore[arg-type]
        now=clock, master_enabled=lambda: True,
    )  # fmt: skip
    r = await mail(client, "https://api.example.com/")
    assert r.reason is EgressReason.TIMEOUT and slow.n == 1


async def test_rate_bound_with_injected_clock() -> None:
    clock = FakeClock()
    client, _, transport = make(policy(max_requests_per_window=2, window_seconds=60), clock=clock)
    assert (await mail(client, "https://api.example.com/")).status is EgressStatus.OK
    assert (await mail(client, "https://api.example.com/")).status is EgressStatus.OK
    r = await mail(client, "https://api.example.com/")
    assert r.status is EgressStatus.EGRESS_DENIED and r.reason is EgressReason.RATE_LIMITED
    assert len(transport.requests) == 2
    clock.advance(61)
    assert (await mail(client, "https://api.example.com/")).status is EgressStatus.OK


# --------------------------------------------------------------------------
# Audit / privacy
# --------------------------------------------------------------------------


async def test_audit_records_safe_bounded_and_canary_free() -> None:
    sink = InMemoryAuditSink(max_records=3)
    client, _, _ = make(audit=sink)
    urls = [
        f"https://api.example.com/{CANARY}?q={CANARY}#{CANARY}",
        f"https://{CANARY}:{CANARY}@api.example.com/{CANARY}",
        f"https://evil-{CANARY}.example.net/{CANARY}",
        f"https://api.example.com/{CANARY}\\x",
        f"https://xn--{CANARY}.example.org/",
    ]
    with capture_logs() as logs:
        results = [await mail(client, u) for u in urls]
    assert len(sink.records) == 3  # bounded
    for rec in sink.records:
        text = repr(rec)
        assert CANARY not in text.replace(CANARY.lower(), "")
        assert "api.example.com" not in text and "example.net" not in text
        assert rec.purpose == "mail_url_reputation" and rec.account_scope == ACCT
        assert rec.decision in {"allow", "deny", "fail"}
        assert rec.timestamp == T0
    assert CANARY not in repr(logs) and all(CANARY not in repr(r) for r in results)
    assert sink.records[0].decision in {"allow", "deny"}


async def test_audit_allow_and_deny_records_fields() -> None:
    sink = InMemoryAuditSink()
    client, _, _ = make(audit=sink)
    await mail(client, "https://api.example.com/x?y=1")
    await mail(client, "https://other.example.net/")
    allow, deny = sink.records
    assert (allow.decision, allow.reason, allow.hops) == ("allow", "OK", 0)
    assert (deny.decision, deny.reason) == ("deny", "HOST_NOT_ALLOWED")
    assert len(allow.host_hash) == 12 and allow.host_hash != deny.host_hash


async def test_audit_on_global_deny_has_no_host_and_sink_failure_is_swallowed() -> None:
    class BadSink:
        def record(self, record):
            raise RuntimeError(CANARY)

    sink = InMemoryAuditSink()
    client, _, _ = make(policy(enabled=False), audit=sink)
    await mail(client, "https://api.example.com/")
    assert sink.records[0].host_hash == "-" and sink.records[0].reason == "EGRESS_DISABLED"
    client2 = EgressClient(
        policy(), transport=FakeTransport(), resolver=FakeResolver(default=[PUBLIC]),
        audit=BadSink(), now=FakeClock(), master_enabled=lambda: True,
    )  # fmt: skip
    with capture_logs() as logs:
        r = await mail(client2, "https://api.example.com/")
    assert r.status is EgressStatus.OK and CANARY not in repr(logs)


async def test_failure_results_never_evaluated() -> None:
    client, _, _ = make(policy(enabled=False))
    r = await mail(client, "https://api.example.com/")
    assert r.evaluated is False and r.body == b"" and r.http_status is None


# --------------------------------------------------------------------------
# Policy object
# --------------------------------------------------------------------------


def test_policy_is_frozen_and_normalizes_to_tuples() -> None:
    p = policy(host_allow=["api.example.com"], allowed_ports=[443])
    assert isinstance(p.host_allow, tuple) and isinstance(p.allowed_ports, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.enabled = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.host_allow = ("*",)  # type: ignore[misc]
    g = grant()
    with pytest.raises(dataclasses.FrozenInstanceError):
        g.account_id = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    "entry",
    ["*", "*.example.com", "example.*", ".", "", "com", ".com", ".co.", "a..b.com", "EXAMPLE.com",
     " example.com", "exa mple.com", "xn--pple-43d.com", "exämple.com", "127.0.0.1", "0.0.0.0",
     "a.b.123", "-a.example.com", "example.com."],
)  # fmt: skip
def test_policy_rejects_allow_all_host_entries(entry: str) -> None:
    with pytest.raises(EgressPolicyError):
        EgressPolicy(host_allow=(entry,))
    with pytest.raises(EgressPolicyError):
        EgressPolicy(host_deny=(entry,))


@pytest.mark.parametrize(
    "entry",
    [
        "0.0.0.0/0",
        "::/0",
        "0.0.0.0/1",
        "128.0.0.0/1",
        "10.0.0.0/7",
        "2000::/3",
        "*",
        "garbage",
        "1.2.3.4/33",
        "10.0.0.1/8",
    ],
)
def test_policy_rejects_allow_all_ip_entries(entry: str) -> None:
    with pytest.raises(EgressPolicyError):
        EgressPolicy(ip_allow=(entry,))


def test_policy_accepts_narrow_ip_entries_and_broad_deny() -> None:
    EgressPolicy(ip_allow=("93.184.216.0/24", "2606:4700::/32"))
    EgressPolicy(ip_deny=("0.0.0.0/0",))  # denying everything is safe


@pytest.mark.parametrize(
    "kw",
    [
        {"enabled": 1},
        {"enabled": "yes"},
        {"allowed_schemes": ("ftp",)},
        {"allowed_schemes": ("*",)},
        {"allowed_ports": (0,)},
        {"allowed_ports": (70000,)},
        {"allowed_ports": (True,)},
        {"max_redirects": -1},
        {"max_redirects": 99},
        {"connect_timeout": 0},
        {"connect_timeout": -1},
        {"read_timeout": 1000},
        {"total_timeout": 1.0, "connect_timeout": 2.0},
        {"total_timeout": float("nan")},
        {"max_response_bytes": 0},
        {"max_response_bytes": 10**10},
        {"max_requests_per_window": 0},
        {"window_seconds": 0},
        {"max_url_length": 0},
        {"consent_grants": ("yes",)},
    ],
)
def test_policy_validation_rejects(kw) -> None:
    with pytest.raises(EgressPolicyError):
        EgressPolicy(**kw)


def test_policy_error_carries_code_only() -> None:
    with pytest.raises(EgressPolicyError) as ei:
        EgressPolicy(host_allow=(f"*{CANARY}",))
    assert CANARY not in str(ei.value) and ei.value.code == "HOST_RULE_INVALID"


# --------------------------------------------------------------------------
# Static: no network / subprocess imports, not wired anywhere
# --------------------------------------------------------------------------

FORBIDDEN = {
    "socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "subprocess",
    "ftplib", "smtplib", "telnetlib", "asyncio.subprocess", "websockets",
}  # fmt: skip


def test_egress_module_imports_no_network_or_subprocess() -> None:
    tree = ast.parse(pathlib.Path(egress.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for name in imported:
        assert name.split(".")[0] not in FORBIDDEN, name
        assert name not in FORBIDDEN, name


def test_egress_not_wired_into_other_modules() -> None:
    root = pathlib.Path(egress.__file__).resolve().parents[1]
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "egress.py" or "__pycache__" in path.parts:
            continue
        text = path.read_text()
        if "mail_security.egress" in text or "mail_security import egress" in text:
            offenders.append(str(path))
    assert offenders == []
