"""Exfiltration and SSRF guard for the Gremlin hub ``fetch`` tool (NS-ADR-114).

Every agent ``fetch`` passes :meth:`FetchGuard.check` before the hub sees it:

- only ``http``/``https``, no credentials in the URL, URL and query length capped;
- internal hostnames (``localhost``, single-label names, ``*.svc``,
  ``*.cluster.local``, ``*.local``, ``*.internal``, ``*.lan``, ``*.home.arpa``)
  and every non-global address (private, loopback, link-local, CGNAT, cluster
  CIDRs, multicast, reserved) are refused, checked after DNS resolution;
- redirects are walked here with ``HEAD`` and no automatic following, and every
  hop is checked the same way; the hub receives the final URL only;
- URLs carrying personal data (emails, phone numbers, long digit runs, key-like
  strings, opaque 32+ character letter-and-digit segments) or values taken from
  the run's own context (personal tokens and numbers on lines about health,
  finance, mental health, contacts) are refused;
- runs marked sensitive (agent in a sensitive class or flagged ``sensitive``)
  may fetch only hosts on ``NERVE_AGENT_FETCH_SENSITIVE_ALLOWLIST``;
- the response length is capped through ``max_length`` (the gateway also
  truncates).

Reason codes are short strings; callers log the host and the code, never the URL.
Residual risk: the hub resolves and connects again on its own, so a host that
changes its DNS answer between this check and the hub's fetch (rebinding) or that
redirects only for the hub's ``GET`` is not caught here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from backend.agents.tools import policy

ALLOWLIST_ENV = "NERVE_AGENT_FETCH_SENSITIVE_ALLOWLIST"
ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_ARGS = frozenset({"url", "max_length", "start_index", "raw"})
MAX_URL_CHARS = 2_048
MAX_QUERY_CHARS = 256
MAX_RESPONSE_CHARS = 20_000
MAX_REDIRECTS = 5

_INTERNAL_SUFFIXES = (
    ".svc",
    ".cluster.local",
    ".local",
    ".localdomain",
    ".internal",
    ".lan",
    ".home.arpa",
    ".localhost",
)
#: Cluster pod and service CIDRs (k3s defaults); private anyway, listed for clarity.
_CLUSTER_NETS = (ipaddress.ip_network("10.42.0.0/16"), ipaddress.ip_network("10.43.0.0/16"))

_SENSITIVE_LINE = re.compile(
    r"balance|account|iban|routing|salary|income|debt|loan|bill|payment|invoice|card"
    r"|phq|gad|mood|anxiety|depress|therap|medicat|diagnos|symptom"
    r"|heart|pulse|blood|glucose|weight|sleep|steps|vital|health|wellness"
    r"|contact|phone|email|address|birth|ssn|\$|€|£",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"\d[\d.,:/-]*\d")
#: The NS-ADR-113 personal patterns without the long-opaque-string rule, which
#: would match any long URL path; opaque tokens are checked per segment instead.
_URL_PERSONAL = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.-]+"
    r"|\+?\d[\d\s().-]{7,}\d"
    r"|\d{9,}"
    r"|\b(?:sk|pk|ghp|xox[bp])[-_][A-Za-z0-9_-]{16,}"
)
_SEGMENT_SPLIT = re.compile(r"[/?&=#.,;:~!*'()\s-]+")

Resolver = Callable[[str], Awaitable[list[str]]]


async def _default_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def context_values(context: str) -> set[str]:
    """Personal values in a run's context that must never appear in a URL."""
    values = {m.group(0).strip().lower() for m in _URL_PERSONAL.finditer(context)}
    values.update(
        seg.lower()
        for seg in _SEGMENT_SPLIT.split(context)
        if len(seg) >= 32 and re.search(r"\d", seg) and re.search(r"[A-Za-z]", seg)
    )
    for line in context.splitlines():
        if _SENSITIVE_LINE.search(line):
            values.update(m.group(0) for m in _NUMBER.finditer(line) if len(m.group(0)) >= 3)
    return {v for v in values if len(v) >= 3}


def _url_has_context_value(url: str, values: set[str]) -> bool:
    text = unquote(url).lower()
    digits = _digits(text)
    for value in values:
        if value in text:
            return True
        value_digits = _digits(value)
        if len(value_digits) >= 6 and value_digits in digits:
            return True
    return False


def url_has_personal_data(text: str) -> bool:
    """Emails, phone numbers, long digit runs, key-like strings or opaque tokens.

    An opaque token is one path/query segment of 32+ characters mixing letters
    and digits (base64, hex, JWT parts); ordinary word slugs are split on
    ``-``/``/`` and never reach that length.
    """
    if _URL_PERSONAL.search(text):
        return True
    return any(
        len(seg) >= 32 and re.search(r"\d", seg) and re.search(r"[A-Za-z]", seg)
        for seg in _SEGMENT_SPLIT.split(text)
    )


def _host_is_internal(host: str) -> bool:
    host = host.rstrip(".").lower()
    if host == "localhost" or "." not in host:
        return True
    return host.endswith(_INTERNAL_SUFFIXES)


def address_is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if any(ip in net for net in _CLUSTER_NETS):
        return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def host_of(url: str) -> str:
    """The hostname only (for audit logs); ``-`` when there is none."""
    try:
        return (urlsplit(url).hostname or "-").lower()
    except ValueError:
        return "-"


class FetchGuard:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sensitive_allowlist: frozenset[str] | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._resolve = resolver or _default_resolver
        self._transport = transport
        self._timeout = timeout_s
        if sensitive_allowlist is None:
            raw = os.environ.get(ALLOWLIST_ENV, "")
            sensitive_allowlist = frozenset(
                h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip()
            )
        self._allowlist = sensitive_allowlist

    def _static(self, url: str, values: set[str]) -> str | None:
        if len(url) > MAX_URL_CHARS:
            return "url_too_long"
        try:
            parts = urlsplit(url)
            host = parts.hostname
            parts.port  # noqa: B018 - raises on a malformed port
        except ValueError:
            return "bad_url"
        if parts.scheme.lower() not in ALLOWED_SCHEMES:
            return "scheme_not_allowed"
        if not host:
            return "bad_url"
        if parts.username is not None or parts.password is not None:
            return "url_credentials"
        if len(parts.query) > MAX_QUERY_CHARS:
            return "query_too_large"
        if url_has_personal_data(unquote(f"{parts.path}?{parts.query}#{parts.fragment}")):
            return "url_personal_data"
        if values and _url_has_context_value(url, values):
            return "url_context_data"
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            return None if address_is_public(str(literal)) else "private_address"
        if _host_is_internal(host):
            return "internal_host"
        return None

    async def _resolved(self, url: str) -> str | None:
        host = urlsplit(url).hostname or ""
        try:
            addresses = await self._resolve(host)
        except (OSError, UnicodeError):
            return "dns_failed"
        if not addresses:
            return "dns_failed"
        if not all(address_is_public(a) for a in addresses):
            return "private_address"
        return None

    async def _hop(self, url: str, values: set[str]) -> str | None:
        return self._static(url, values) or await self._resolved(url)

    async def check(
        self, agent: Any, args: dict[str, Any], *, context: str = "", sensitive: bool = False
    ) -> tuple[str | None, dict[str, Any]]:
        """``(reason, safe_args)``; ``reason`` is ``None`` when the fetch may go ahead."""
        if not set(args) <= ALLOWED_ARGS:
            return "bad_arguments", {}
        url = args.get("url")
        if not isinstance(url, str) or not url.strip():
            return "bad_url", {}
        url = url.strip()
        sensitive = sensitive or bool(getattr(agent, "sensitive", False))
        sensitive = sensitive or policy.sensitive_class(agent)
        if sensitive and host_of(url) not in self._allowlist:
            return "sensitive_run", {}
        values = context_values(context) if context else set()
        reason = await self._hop(url, values)
        if reason is not None:
            return reason, {}
        final, reason = await self._walk_redirects(url, values)
        if reason is not None:
            return reason, {}
        if sensitive and host_of(final) not in self._allowlist:
            return "sensitive_run", {}
        safe: dict[str, Any] = {"url": final}
        try:
            requested = int(args.get("max_length") or MAX_RESPONSE_CHARS)
            start = int(args.get("start_index") or 0)
        except (TypeError, ValueError):
            return "bad_arguments", {}
        safe["max_length"] = max(1, min(requested, MAX_RESPONSE_CHARS))
        if start:
            safe["start_index"] = max(0, start)
        if "raw" in args:
            safe["raw"] = bool(args["raw"])
        return None, safe

    async def _walk_redirects(self, url: str, values: set[str]) -> tuple[str, str | None]:
        async with httpx.AsyncClient(
            transport=self._transport, timeout=self._timeout, follow_redirects=False
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                try:
                    resp = await client.head(url)
                except httpx.HTTPError:
                    return url, "unreachable"
                location = resp.headers.get("location")
                if not resp.is_redirect or not location:
                    return url, None
                url = urljoin(url, location)
                reason = await self._hop(url, values)
                if reason is not None:
                    return url, f"redirect_{reason}"
        return url, "too_many_redirects"
