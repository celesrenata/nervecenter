"""Constrained-egress policy engine and client for NerveShield (Phase 2, Task 2.5).

This module is the **outbound-request control layer** (R-005.3, R-017.3, T-09).
It ships a *policy engine* and an *orchestrating client* around an **injected**
transport and resolver. It contains **no network code**: no ``socket``, ``ssl``,
``http.client``, ``urllib.request``, ``requests``/``httpx``/``aiohttp`` and no
``subprocess``. A real transport is a deployment-time item (see below).

Security stance: **default deny, explicit consent, when in doubt deny.**

- Nothing is enabled by default. With the default :class:`EgressPolicy` (and
  with ``NERVE_MAIL_SECURITY_ENABLED`` off) every request is refused and the
  transport/resolver are never called.
- Offline lexical analysis of the URL (scheme, port, host, allow/deny, numeric
  host tricks, userinfo, ...) happens **before** any resolution (R-005.2).
- Mail-derived URLs need egress enabled **and** an explicit, account-scoped,
  purpose-bound, unexpired :class:`ConsentGrant` (R-017.3, design §10). Without
  one the lookup is denied with a typed code.
- **URL minimization.** For :attr:`EgressPurpose.MAIL_URL_REPUTATION` the
  outbound request is reduced to *host only*: the path is forced to ``/`` and
  the query and fragment are dropped, on every hop. Userinfo
  (``user:pass@host``) is never stripped silently; it causes denial. Full paths,
  query strings, tokens and tracking parameters therefore never leave the
  machine. :attr:`EgressPurpose.PUBLIC_FEED` carries configured (non mail
  derived) URLs and may keep path and query; it still forbids userinfo and drops
  the fragment.
- **Rebinding / TOCTOU.** The hostname is resolved twice per hop; *every*
  address of *both* answers must be allowed and the two answer sets must be
  identical (DNS rotation is denied). The first validated address is then
  **pinned** and handed to the transport as ``pinned_ip``; the transport must
  connect to that IP and never re-resolve. Every redirect hop re-runs the whole
  validation.
- Failures never read as safe: any denial/failure returns a typed
  :class:`EgressResult` whose :attr:`EgressResult.evaluated` is ``False``
  (downstream treats it as *not evaluated*, never clean; I-02).
- Every decision is written to an :class:`EgressAuditSink` as a bounded safe
  record (purpose, account scope, decision, reason code, hashed host, time,
  hop count). Never the URL, path, query, userinfo or any mail text. Errors,
  exceptions and logs carry safe codes only.

Not wired anywhere: no worker, inspections, policy, feeds, app startup or
scheduler imports this module. Explicit-call API only; no migration.

Transport contract a deployment-time implementation MUST meet
(see :class:`EgressTransport`): connect to ``pinned_ip`` only (never re-resolve
``host``); use ``host`` for SNI/certificate verification and the ``Host``
header; ignore proxy environment variables; do **not** follow redirects (return
the 3xx and ``location`` to this client); stop reading at
``max_response_bytes`` and flag ``truncated``; honour the connect/read
timeouts; send GET only with no cookies/credentials. A real transport and its
loopback integration test are out of scope here (open item for deployment
review).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

import structlog

from backend.mail_security._domains import is_confusable_domain
from backend.mail_security.ingestion_seam import mail_security_enabled

logger = structlog.get_logger(__name__)


class EgressPurpose(StrEnum):
    """Why an outbound request is made. Purposes never share consent."""

    #: Reputation lookup derived from mail content. Needs explicit consent.
    MAIL_URL_REPUTATION = "mail_url_reputation"
    #: Fetch of a configured public feed (Task 2.3). Carries no mail-derived input.
    PUBLIC_FEED = "public_feed"


class EgressStatus(StrEnum):
    OK = "OK"
    EGRESS_DENIED = "EGRESS_DENIED"
    EGRESS_FAILED = "EGRESS_FAILED"


class EgressReason(StrEnum):
    OK = "OK"
    MASTER_DISABLED = "MASTER_DISABLED"
    EGRESS_DISABLED = "EGRESS_DISABLED"
    INVALID_PURPOSE = "INVALID_PURPOSE"
    ACCOUNT_REQUIRED = "ACCOUNT_REQUIRED"
    CONSENT_MISSING = "CONSENT_MISSING"
    CONSENT_EXPIRED = "CONSENT_EXPIRED"
    URL_INVALID = "URL_INVALID"
    URL_TOO_LONG = "URL_TOO_LONG"
    URL_NON_ASCII = "URL_NON_ASCII"
    URL_BACKSLASH_OR_WHITESPACE = "URL_BACKSLASH_OR_WHITESPACE"
    USERINFO_PRESENT = "USERINFO_PRESENT"
    SCHEME_NOT_ALLOWED = "SCHEME_NOT_ALLOWED"
    PORT_NOT_ALLOWED = "PORT_NOT_ALLOWED"
    HOST_INVALID = "HOST_INVALID"
    HOST_CONFUSABLE = "HOST_CONFUSABLE"
    IP_LITERAL = "IP_LITERAL"
    NUMERIC_HOST = "NUMERIC_HOST"
    HOST_BLOCKED = "HOST_BLOCKED"
    HOST_DENIED = "HOST_DENIED"
    HOST_NOT_ALLOWED = "HOST_NOT_ALLOWED"
    RATE_LIMITED = "RATE_LIMITED"
    DNS_FAILED = "DNS_FAILED"
    DNS_NO_ADDRESS = "DNS_NO_ADDRESS"
    DNS_ROTATION = "DNS_ROTATION"
    ADDRESS_INVALID = "ADDRESS_INVALID"
    ADDRESS_BLOCKED = "ADDRESS_BLOCKED"
    ADDRESS_NOT_ALLOWED = "ADDRESS_NOT_ALLOWED"
    REDIRECT_TOO_MANY = "REDIRECT_TOO_MANY"
    REDIRECT_LOOP = "REDIRECT_LOOP"
    REDIRECT_DOWNGRADE = "REDIRECT_DOWNGRADE"
    REDIRECT_INVALID = "REDIRECT_INVALID"
    TIMEOUT = "TIMEOUT"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    BAD_RESPONSE = "BAD_RESPONSE"


class EgressPolicyError(ValueError):
    """Invalid policy/consent configuration. Carries a safe code only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Outcome(Exception):
    """Internal control flow carrying only a typed reason (never a value)."""

    def __init__(self, status: EgressStatus, reason: EgressReason) -> None:
        super().__init__(reason.value)
        self.status = status
        self.reason = reason
        self.host: str | None = None
        self.hops = 0


def _deny(reason: EgressReason) -> _Outcome:
    return _Outcome(EgressStatus.EGRESS_DENIED, reason)


def _fail(reason: EgressReason) -> _Outcome:
    return _Outcome(EgressStatus.EGRESS_FAILED, reason)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

_APPROVAL_REF_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORT = {"http": 80, "https": 443}
_MIN_V4_PREFIX = 8
_MIN_V6_PREFIX = 16


@dataclass(frozen=True)
class ConsentGrant:
    """Opaque, account-scoped, purpose-bound, expiring approval (R-017.3).

    ``approval_ref`` is an opaque reference to the recorded approval (like the
    Task 2.2 override approvals); no personal data lives here.
    """

    approval_ref: str
    account_id: str
    purpose: EgressPurpose
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.approval_ref, str) or not _APPROVAL_REF_RE.match(self.approval_ref):
            raise EgressPolicyError("CONSENT_APPROVAL_REF_INVALID")
        if not isinstance(self.account_id, str) or not (1 <= len(self.account_id) <= 128):
            raise EgressPolicyError("CONSENT_ACCOUNT_INVALID")
        if self.purpose is not EgressPurpose.MAIL_URL_REPUTATION:
            # Only mail-derived lookups use consent; never a blanket purpose.
            raise EgressPolicyError("CONSENT_PURPOSE_INVALID")
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise EgressPolicyError("CONSENT_EXPIRY_INVALID")


def _canon_host_rule(entry: object) -> str:
    """Validate a host allow/deny entry: exact ``a.b.c`` or suffix ``.b.c``."""
    if not isinstance(entry, str) or not entry or not entry.isascii():
        raise EgressPolicyError("HOST_RULE_INVALID")
    rule = entry.strip().lower()
    if rule != entry or "*" in rule or rule in {".", ""} or ".." in rule:
        raise EgressPolicyError("HOST_RULE_INVALID")
    if rule.endswith("."):
        raise EgressPolicyError("HOST_RULE_INVALID")
    body = rule[1:] if rule.startswith(".") else rule
    labels = body.split(".")
    # Need at least two labels so a bare TLD ('.com') cannot become allow-all.
    if len(labels) < 2 or any(not _LABEL_RE.match(label) for label in labels):
        raise EgressPolicyError("HOST_RULE_INVALID")
    if is_confusable_domain(body):
        raise EgressPolicyError("HOST_RULE_INVALID")
    if labels[-1].isdigit():
        raise EgressPolicyError("HOST_RULE_INVALID")
    return rule


def _canon_networks(entries: object, *, allow_list: bool) -> tuple[str, ...]:
    if isinstance(entries, str) or not isinstance(entries, Iterable):
        raise EgressPolicyError("IP_RULE_INVALID")
    out: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.isascii():
            raise EgressPolicyError("IP_RULE_INVALID")
        try:
            net = ipaddress.ip_network(entry, strict=True)
        except ValueError:
            raise EgressPolicyError("IP_RULE_INVALID") from None
        if allow_list:
            floor = _MIN_V4_PREFIX if net.version == 4 else _MIN_V6_PREFIX
            if net.prefixlen < floor:
                raise EgressPolicyError("IP_RULE_TOO_BROAD")
        out.append(str(net))
    return tuple(out)


def _bounded_float(value: object, lo: float, hi: float, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EgressPolicyError(code)
    if not (lo < float(value) <= hi):
        raise EgressPolicyError(code)
    return float(value)


def _bounded_int(value: object, lo: int, hi: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
        raise EgressPolicyError(code)
    return value


@dataclass(frozen=True)
class EgressPolicy:
    """Frozen, validated egress policy. **Everything is denied by default.**

    Defaults: ``enabled=False``, no consent grants, https only, port 443 only,
    empty host allowlist (nothing allowed), no extra deny lists, 3 redirects,
    3s connect / 5s read / 10s total, 1 MiB response cap, 20 requests per 60s,
    2048-char URLs. Allow-all entries (``*``, bare TLDs, ``0.0.0.0/0``, ``::/0``,
    very broad networks) are rejected at construction.
    """

    enabled: bool = False
    consent_grants: tuple[ConsentGrant, ...] = ()
    allowed_schemes: tuple[str, ...] = ("https",)
    allowed_ports: tuple[int, ...] = (443,)
    host_allow: tuple[str, ...] = ()
    host_deny: tuple[str, ...] = ()
    ip_allow: tuple[str, ...] = ()
    ip_deny: tuple[str, ...] = ()
    max_redirects: int = 3
    connect_timeout: float = 3.0
    read_timeout: float = 5.0
    total_timeout: float = 10.0
    max_response_bytes: int = 1_048_576
    max_requests_per_window: int = 20
    window_seconds: float = 60.0
    max_url_length: int = 2048

    def __post_init__(self) -> None:
        setter = object.__setattr__
        if not isinstance(self.enabled, bool):
            raise EgressPolicyError("ENABLED_INVALID")
        grants = tuple(self.consent_grants)
        if not all(isinstance(g, ConsentGrant) for g in grants):
            raise EgressPolicyError("CONSENT_INVALID")
        setter(self, "consent_grants", grants)
        schemes = tuple(str(s) for s in self.allowed_schemes)
        if any(s not in _SCHEMES for s in schemes):
            raise EgressPolicyError("SCHEME_INVALID")
        setter(self, "allowed_schemes", schemes)
        ports = tuple(self.allowed_ports)
        setter(
            self,
            "allowed_ports",
            tuple(_bounded_int(p, 1, 65535, "PORT_INVALID") for p in ports),
        )
        setter(self, "host_allow", tuple(_canon_host_rule(e) for e in self.host_allow))
        setter(self, "host_deny", tuple(_canon_host_rule(e) for e in self.host_deny))
        setter(self, "ip_allow", _canon_networks(self.ip_allow, allow_list=True))
        setter(self, "ip_deny", _canon_networks(self.ip_deny, allow_list=False))
        _bounded_int(self.max_redirects, 0, 5, "REDIRECTS_INVALID")
        _bounded_float(self.connect_timeout, 0, 60, "TIMEOUT_INVALID")
        _bounded_float(self.read_timeout, 0, 60, "TIMEOUT_INVALID")
        _bounded_float(self.total_timeout, 0, 120, "TIMEOUT_INVALID")
        if self.connect_timeout > self.total_timeout or self.read_timeout > self.total_timeout:
            raise EgressPolicyError("TIMEOUT_INVALID")
        _bounded_int(self.max_response_bytes, 1, 16 * 1024 * 1024, "MAX_BYTES_INVALID")
        _bounded_int(self.max_requests_per_window, 1, 1000, "RATE_INVALID")
        _bounded_float(self.window_seconds, 0, 86_400, "RATE_INVALID")
        _bounded_int(self.max_url_length, 1, 8192, "URL_LENGTH_INVALID")


# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------

_BLOCKED_V4 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local incl. 169.254.169.254 metadata
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved incl. 255.255.255.255 broadcast
    )
)
_BLOCKED_V6 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "::/96",  # unspecified, loopback, deprecated IPv4-compatible
        "::ffff:0:0/96",  # IPv4-mapped
        "64:ff9b::/96",  # NAT64
        "64:ff9b:1::/48",  # local-use NAT64
        "100::/64",  # discard
        "2001::/23",  # IETF protocol assignments incl. Teredo
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4 (embeds IPv4)
        "3fff::/20",  # documentation
        "fc00::/7",  # ULA
        "fe80::/10",  # link-local
        "fec0::/10",  # site-local
        "ff00::/8",  # multicast
    )
)
_GLOBAL_UNICAST_V6 = ipaddress.ip_network("2000::/3")


def parse_ip(text: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Strictly parse an IP literal. ``None`` for anything dubious (scope ids,
    whitespace, non-ASCII, leading-zero octets)."""
    if not isinstance(text, str) or not text.isascii() or not text or "%" in text:
        return None
    if text != text.strip():
        return None
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def is_address_blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Built-in SSRF address block. Fail-closed: anything not clearly public."""
    if isinstance(addr, ipaddress.IPv4Address):
        if any(addr in net for net in _BLOCKED_V4):
            return True
    else:
        if addr.ipv4_mapped is not None or addr not in _GLOBAL_UNICAST_V6:
            return True
        if any(addr in net for net in _BLOCKED_V6):
            return True
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return True
    return not addr.is_global


# ---------------------------------------------------------------------------
# URL parsing (lexical, offline; R-005.2)
# ---------------------------------------------------------------------------

_BLOCKED_HOST_NAMES = frozenset({"localhost"})
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
    ".localdomain",
    ".intranet",
    ".corp",
    ".arpa",
)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]{0,15}$")


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    path: str
    query: str


def _parse_url(url: object, policy: EgressPolicy, purpose: EgressPurpose) -> _Target:
    """Strictly parse and canonicalize ``url`` or raise a typed denial.

    Never echoes any part of the URL in an error.
    """
    if not isinstance(url, str) or not url:
        raise _deny(EgressReason.URL_INVALID)
    if len(url) > policy.max_url_length:
        raise _deny(EgressReason.URL_TOO_LONG)
    if not url.isascii():
        raise _deny(EgressReason.URL_NON_ASCII)
    if "\\" in url or any(c.isspace() or ord(c) < 0x21 or ord(c) == 0x7F for c in url):
        raise _deny(EgressReason.URL_BACKSLASH_OR_WHITESPACE)
    scheme, sep, rest = url.partition("://")
    scheme = scheme.lower()
    if not sep or not _SCHEME_RE.match(scheme):
        raise _deny(EgressReason.URL_INVALID)
    if scheme not in policy.allowed_schemes:
        raise _deny(EgressReason.SCHEME_NOT_ALLOWED)
    # Authority ends at the first '/', '?' or '#' (so "evil.com#@good.com" has
    # host evil.com and "good.com?@evil.com" is split before the '@').
    cut = len(rest)
    for ch in "/?#":
        idx = rest.find(ch)
        if idx != -1:
            cut = min(cut, idx)
    authority, remainder = rest[:cut], rest[cut:]
    if "@" in authority:
        raise _deny(EgressReason.USERINFO_PRESENT)
    if authority.startswith("["):
        raise _deny(EgressReason.IP_LITERAL)
    if "%" in authority:
        raise _deny(EgressReason.HOST_INVALID)
    host_part, colon, port_text = authority.partition(":")
    if ":" in port_text:
        raise _deny(EgressReason.HOST_INVALID)
    if colon:
        if not (port_text.isascii() and port_text.isdigit()) or len(port_text) > 5:
            raise _deny(EgressReason.PORT_NOT_ALLOWED)
        if len(port_text) > 1 and port_text.startswith("0"):
            raise _deny(EgressReason.PORT_NOT_ALLOWED)
        port = int(port_text)
    else:
        port = _DEFAULT_PORT[scheme]
    if port not in policy.allowed_ports:
        raise _deny(EgressReason.PORT_NOT_ALLOWED)
    host = _canon_host(host_part)
    # Fragment is always dropped. Mail-derived URLs are reduced to host only.
    remainder = remainder.split("#", 1)[0]
    if purpose is EgressPurpose.MAIL_URL_REPUTATION:
        path, query = "/", ""
    else:
        path, _, query = remainder.partition("?")
        path = path or "/"
    return _Target(scheme=scheme, host=host, port=port, path=path, query=query)


def _canon_host(host_part: str) -> str:
    if not host_part:
        raise _deny(EgressReason.HOST_INVALID)
    host = host_part.lower()
    if host.endswith("."):
        host = host[:-1]
    if not host or len(host) > 253:
        raise _deny(EgressReason.HOST_INVALID)
    labels = host.split(".")
    last = labels[-1]
    # Any host that looks numeric to a lenient resolver (decimal, octal, hex,
    # mixed, shortened IPv4) is refused outright; strict dotted quads are IP
    # literals, also refused (hostnames only).
    if last.isdigit() or any(re.fullmatch(r"0x[0-9a-f]*", label) for label in labels):
        if parse_ip(host) is not None:
            raise _deny(EgressReason.IP_LITERAL)
        raise _deny(EgressReason.NUMERIC_HOST)
    if ":" in host:
        raise _deny(EgressReason.HOST_INVALID)
    if any(not _LABEL_RE.match(label) for label in labels):
        if is_confusable_domain(host) or "xn--" in host:
            raise _deny(EgressReason.HOST_CONFUSABLE)
        raise _deny(EgressReason.HOST_INVALID)
    if is_confusable_domain(host):
        raise _deny(EgressReason.HOST_CONFUSABLE)
    if len(labels) < 2:
        raise _deny(EgressReason.HOST_BLOCKED)
    if host in _BLOCKED_HOST_NAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise _deny(EgressReason.HOST_BLOCKED)
    return host


def _host_matches(host: str, rules: Iterable[str]) -> bool:
    for rule in rules:
        if rule.startswith("."):
            if host.endswith(rule):
                return True
        elif host == rule:
            return True
    return False


# ---------------------------------------------------------------------------
# Injected capabilities
# ---------------------------------------------------------------------------


class EgressResolver(Protocol):
    """Injected DNS resolver. The client never resolves any other way."""

    async def resolve(self, host: str) -> Sequence[str]: ...


@dataclass(frozen=True)
class TransportRequest:
    """What the client hands to the transport for one hop (GET only)."""

    scheme: str
    host: str  # SNI / certificate name / Host header
    port: int
    pinned_ip: str  # the ONLY address the transport may connect to
    path: str
    query: str
    connect_timeout: float
    read_timeout: float
    max_response_bytes: int


@dataclass(frozen=True)
class TransportResponse:
    status: int
    body: bytes = b""
    location: str | None = None
    truncated: bool = False


class EgressTransport(Protocol):
    """Injected transport. See the module docstring for the contract."""

    async def fetch(self, request: TransportRequest) -> TransportResponse: ...


class EgressAuditSink(Protocol):
    def record(self, record: EgressAuditRecord) -> None: ...


@dataclass(frozen=True)
class EgressAuditRecord:
    """Bounded safe audit record. Never contains a URL, path, query, userinfo
    or mail-derived text; the host is only a truncated hash."""

    purpose: str
    account_scope: str
    decision: str  # "allow" | "deny" | "fail"
    reason: str
    host_hash: str
    timestamp: datetime
    hops: int


class InMemoryAuditSink:
    """Bounded in-memory audit sink."""

    def __init__(self, max_records: int = 1000) -> None:
        self._records: deque[EgressAuditRecord] = deque(maxlen=max_records)

    def record(self, record: EgressAuditRecord) -> None:
        self._records.append(record)

    @property
    def records(self) -> tuple[EgressAuditRecord, ...]:
        return tuple(self._records)


@dataclass(frozen=True)
class EgressResult:
    status: EgressStatus
    reason: EgressReason
    http_status: int | None = None
    body: bytes = b""
    hops: int = 0

    @property
    def evaluated(self) -> bool:
        """``True`` only for a completed fetch. Denied/failed means *not
        evaluated*, never safe or clean (I-02)."""
        return self.status is EgressStatus.OK


def _host_hash(host: str | None) -> str:
    if not host:
        return "-"
    return hashlib.sha256(host.encode("ascii", "ignore")).hexdigest()[:12]


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ABS_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_MAX_ADDRESSES = 16


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EgressClient:
    """Orchestrates policy checks, pinned resolution, redirects and limits.

    ``transport``/``resolver`` are injected; this class opens no connections.
    """

    def __init__(
        self,
        policy: EgressPolicy,
        *,
        transport: EgressTransport,
        resolver: EgressResolver,
        audit: EgressAuditSink | None = None,
        now: Callable[[], datetime] = _utc_now,
        master_enabled: Callable[[], bool] = mail_security_enabled,
    ) -> None:
        self._policy = policy
        self._transport = transport
        self._resolver = resolver
        self._audit = audit
        self._now = now
        self._master_enabled = master_enabled
        self._sent: deque[datetime] = deque()

    @property
    def policy(self) -> EgressPolicy:
        return self._policy

    async def fetch(
        self,
        url: str,
        *,
        purpose: EgressPurpose,
        account_id: str | None = None,
    ) -> EgressResult:
        """Fetch ``url`` under the policy. Never raises for policy outcomes."""
        host_for_audit: str | None = None
        hops = 0
        try:
            result, host_for_audit, hops = await self._run(url, purpose, account_id)
        except _Outcome as out:
            hops = out.hops
            result = EgressResult(out.status, out.reason, hops=hops)
            host_for_audit = out.host
        except Exception:  # noqa: BLE001 - never leak exception text
            result = EgressResult(EgressStatus.EGRESS_FAILED, EgressReason.TRANSPORT_ERROR)
        self._emit(purpose, account_id, result, host_for_audit)
        return result

    # -- internals ---------------------------------------------------------

    def _emit(
        self,
        purpose: object,
        account_id: object,
        result: EgressResult,
        host: str | None,
    ) -> None:
        decision = (
            "allow"
            if result.status is EgressStatus.OK
            else "deny"
            if result.status is EgressStatus.EGRESS_DENIED
            else "fail"
        )
        purpose_text = purpose.value if isinstance(purpose, EgressPurpose) else "invalid"
        scope = account_id[:64] if isinstance(account_id, str) and account_id else "-"
        logger.info(
            "mail_security.egress",
            purpose=purpose_text,
            decision=decision,
            reason=result.reason.value,
            hops=result.hops,
        )
        if self._audit is None:
            return
        try:
            self._audit.record(
                EgressAuditRecord(
                    purpose=purpose_text,
                    account_scope=scope,
                    decision=decision,
                    reason=result.reason.value,
                    host_hash=_host_hash(host),
                    timestamp=self._now(),
                    hops=result.hops,
                )
            )
        except Exception:  # noqa: BLE001 - audit failure must not leak text
            logger.warning("mail_security.egress.audit_failed")

    def _check_gates(self, purpose: object, account_id: object) -> EgressPurpose:
        if not self._master_enabled():
            raise _deny(EgressReason.MASTER_DISABLED)
        if not self._policy.enabled:
            raise _deny(EgressReason.EGRESS_DISABLED)
        if not isinstance(purpose, EgressPurpose):
            raise _deny(EgressReason.INVALID_PURPOSE)
        if purpose is EgressPurpose.MAIL_URL_REPUTATION:
            if not isinstance(account_id, str) or not account_id:
                raise _deny(EgressReason.ACCOUNT_REQUIRED)
            self._check_consent(account_id, purpose)
        return purpose

    def _check_consent(self, account_id: str, purpose: EgressPurpose) -> None:
        now = self._now()
        matched_expired = False
        for grant in self._policy.consent_grants:
            if grant.account_id != account_id or grant.purpose is not purpose:
                continue
            if grant.expires_at > now:
                return
            matched_expired = True
        raise _deny(
            EgressReason.CONSENT_EXPIRED if matched_expired else EgressReason.CONSENT_MISSING
        )

    def _check_host(self, target: _Target) -> None:
        if _host_matches(target.host, self._policy.host_deny):
            raise _deny(EgressReason.HOST_DENIED)
        if not _host_matches(target.host, self._policy.host_allow):
            raise _deny(EgressReason.HOST_NOT_ALLOWED)

    def _check_rate(self) -> None:
        policy = self._policy
        now = self._now()
        while self._sent and (now - self._sent[0]).total_seconds() >= policy.window_seconds:
            self._sent.popleft()
        if len(self._sent) >= policy.max_requests_per_window:
            raise _deny(EgressReason.RATE_LIMITED)
        self._sent.append(now)

    def _validate_addresses(
        self, raw: object
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        if isinstance(raw, str | bytes) or not isinstance(raw, Sequence):
            raise _fail(EgressReason.DNS_FAILED)
        if not raw:
            raise _fail(EgressReason.DNS_NO_ADDRESS)
        if len(raw) > _MAX_ADDRESSES:
            raise _deny(EgressReason.ADDRESS_INVALID)
        policy = self._policy
        deny_nets = [ipaddress.ip_network(n) for n in policy.ip_deny]
        allow_nets = [ipaddress.ip_network(n) for n in policy.ip_allow]
        addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for item in raw:
            addr = parse_ip(item)
            if addr is None:
                raise _deny(EgressReason.ADDRESS_INVALID)
            # ANY disallowed address denies the whole hostname.
            if is_address_blocked(addr) or any(addr in n for n in deny_nets):
                raise _deny(EgressReason.ADDRESS_BLOCKED)
            if allow_nets and not any(addr in n for n in allow_nets):
                raise _deny(EgressReason.ADDRESS_NOT_ALLOWED)
            addrs.append(addr)
        return addrs

    async def _resolve_once(
        self, host: str, deadline: datetime
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        timeout = self._remaining(deadline, self._policy.connect_timeout)
        try:
            raw = await asyncio.wait_for(self._resolver.resolve(host), timeout=timeout)
        except TimeoutError:
            raise _fail(EgressReason.TIMEOUT) from None
        except Exception:  # noqa: BLE001
            raise _fail(EgressReason.DNS_FAILED) from None
        return self._validate_addresses(raw)

    def _remaining(self, deadline: datetime, cap: float) -> float:
        left = (deadline - self._now()).total_seconds()
        if left <= 0:
            raise _fail(EgressReason.TIMEOUT)
        return min(left, cap)

    async def _run(
        self, url: str, purpose: object, account_id: object
    ) -> tuple[EgressResult, str | None, int]:
        purpose_checked = self._check_gates(purpose, account_id)
        policy = self._policy
        deadline = self._now() + timedelta(seconds=policy.total_timeout)
        target = _parse_url(url, policy, purpose_checked)
        seen: set[tuple[str, str, int, str, str]] = set()
        hops = 0
        try:
            while True:
                response = await self._hop(target, deadline, seen)
                if response.status not in _REDIRECT_STATUSES:
                    return (
                        EgressResult(
                            EgressStatus.OK,
                            EgressReason.OK,
                            http_status=response.status,
                            body=response.body,
                            hops=hops,
                        ),
                        target.host,
                        hops,
                    )
                hops += 1
                if hops > policy.max_redirects:
                    raise _deny(EgressReason.REDIRECT_TOO_MANY)
                target = self._next_target(target, response.location, purpose_checked)
        except _Outcome as out:
            out.host = target.host
            out.hops = hops
            raise

    def _next_target(self, current: _Target, location: object, purpose: EgressPurpose) -> _Target:
        if not isinstance(location, str) or not location:
            raise _deny(EgressReason.REDIRECT_INVALID)
        if _ABS_URL_RE.match(location):
            candidate = location
        elif location.startswith("/") and not location.startswith("//"):
            candidate = f"{current.scheme}://{current.host}:{current.port}{location}"
        else:
            raise _deny(EgressReason.REDIRECT_INVALID)
        try:
            nxt = _parse_url(candidate, self._policy, purpose)
        except _Outcome:
            raise _deny(EgressReason.REDIRECT_INVALID) from None
        if current.scheme == "https" and nxt.scheme != "https":
            raise _deny(EgressReason.REDIRECT_DOWNGRADE)
        return nxt

    async def _hop(
        self,
        target: _Target,
        deadline: datetime,
        seen: set[tuple[str, str, int, str, str]],
    ) -> TransportResponse:
        policy = self._policy
        key = (target.scheme, target.host, target.port, target.path, target.query)
        if key in seen:
            raise _deny(EgressReason.REDIRECT_LOOP)
        seen.add(key)
        self._check_host(target)
        # Resolve twice; both answers fully validated and identical, so DNS
        # rotation / rebinding between lookups is denied.
        first = await self._resolve_once(target.host, deadline)
        second = await self._resolve_once(target.host, deadline)
        if {str(a) for a in first} != {str(a) for a in second}:
            raise _deny(EgressReason.DNS_ROTATION)
        pinned = first[0]
        self._check_rate()
        request = TransportRequest(
            scheme=target.scheme,
            host=target.host,
            port=target.port,
            pinned_ip=str(pinned),
            path=target.path,
            query=target.query,
            connect_timeout=policy.connect_timeout,
            read_timeout=policy.read_timeout,
            max_response_bytes=policy.max_response_bytes,
        )
        timeout = self._remaining(deadline, policy.total_timeout)
        try:
            response = await asyncio.wait_for(self._transport.fetch(request), timeout=timeout)
        except TimeoutError:
            raise _fail(EgressReason.TIMEOUT) from None
        except Exception:  # noqa: BLE001
            raise _fail(EgressReason.TRANSPORT_ERROR) from None
        if (
            not isinstance(response, TransportResponse)
            or isinstance(response.status, bool)
            or not isinstance(response.status, int)
            or not isinstance(response.body, bytes)
        ):
            raise _fail(EgressReason.BAD_RESPONSE)
        if response.truncated or len(response.body) > policy.max_response_bytes:
            raise _fail(EgressReason.RESPONSE_TOO_LARGE)
        if self._now() >= deadline:
            raise _fail(EgressReason.TIMEOUT)
        return response


__all__ = [
    "ConsentGrant",
    "EgressAuditRecord",
    "EgressAuditSink",
    "EgressClient",
    "EgressPolicy",
    "EgressPolicyError",
    "EgressPurpose",
    "EgressReason",
    "EgressResolver",
    "EgressResult",
    "EgressStatus",
    "EgressTransport",
    "InMemoryAuditSink",
    "TransportRequest",
    "TransportResponse",
    "is_address_blocked",
    "parse_ip",
]
