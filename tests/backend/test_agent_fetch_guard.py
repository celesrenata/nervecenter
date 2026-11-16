"""Exfiltration and SSRF guard for agent ``fetch`` (NS-ADR-114)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import structlog

from backend.agents.tools import ToolGateway
from backend.agents.tools.fetch_guard import (
    MAX_RESPONSE_CHARS,
    FetchGuard,
    address_is_public,
    context_values,
)

AGENT = SimpleNamespace(id="a1", type="planner", name="planner")
FINANCE = SimpleNamespace(id="f1", type="finance", name="Finance helper")

DNS = {
    "example.org": ["93.184.215.14"],
    "docs.example.org": ["93.184.215.15"],
    "rebind.example.org": ["10.0.0.5"],
    "mixed.example.org": ["93.184.215.14", "192.168.42.8"],
    "v6.example.org": ["::ffff:127.0.0.1"],
    "cgnat.example.org": ["100.64.0.1"],
}


async def resolver(host: str) -> list[str]:
    if host not in DNS:
        raise OSError("nxdomain")
    return DNS[host]


def guard(
    redirects: dict[str, str] | None = None, allow: frozenset[str] = frozenset()
) -> FetchGuard:
    redirects = redirects or {}

    def head(request: httpx.Request) -> httpx.Response:
        target = redirects.get(str(request.url))
        if target is not None:
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200)

    return FetchGuard(
        resolver=resolver, transport=httpx.MockTransport(head), sensitive_allowlist=allow
    )


async def reason(url: str, **kw: Any) -> str | None:
    out, _ = await guard().check(AGENT, {"url": url}, **kw)
    return out


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("file:///etc/passwd", "scheme_not_allowed"),
        ("ftp://example.org/x", "scheme_not_allowed"),
        ("gopher://example.org/", "scheme_not_allowed"),
        ("https://user:pw@example.org/", "url_credentials"),
        ("https://example.org/" + "a" * 2100, "url_too_long"),
        ("https://example.org/?q=" + "a" * 300, "query_too_large"),
        ("http://localhost:8080/", "internal_host"),
        ("http://postgres/", "internal_host"),
        ("http://mcp-hub.mcp-hub.svc:8080/memory", "internal_host"),
        ("http://api.nervecenter.svc.cluster.local/", "internal_host"),
        ("http://127.0.0.1/", "private_address"),
        ("http://169.254.169.254/latest/meta-data", "private_address"),
        ("http://10.43.0.1/", "private_address"),
        ("http://192.168.42.8/", "private_address"),
        ("http://[::1]/", "private_address"),
        ("https://rebind.example.org/", "private_address"),
        ("https://mixed.example.org/", "private_address"),
        ("https://v6.example.org/", "private_address"),
        ("https://cgnat.example.org/", "private_address"),
        ("https://unknown.example.org/", "dns_failed"),
        ("https://example.org/?to=someone@example.com", "url_personal_data"),
        ("https://example.org/?to=someone%40example.com", "url_personal_data"),
        ("https://example.org/p/+1%20555%20123%204567", "url_personal_data"),
        ("https://example.org/?acct=123456789012", "url_personal_data"),
        (
            "https://example.org/t/eyJhbGciOiJIUzI1NiJ9eyJzdWIiOiIxMjM0NTY3ODkwIn0",
            "url_personal_data",
        ),
        ("https://example.org/?k=sk-abcdefghijklmnopqrstuv", "url_personal_data"),
        ("not a url", "scheme_not_allowed"),
    ],
)
async def test_refused_urls(url: str, code: str) -> None:
    assert await reason(url) == code


async def test_public_https_url_is_allowed_with_capped_response() -> None:
    out, safe = await guard().check(AGENT, {"url": "https://example.org/a", "max_length": 10**9})
    assert out is None
    assert safe == {"url": "https://example.org/a", "max_length": MAX_RESPONSE_CHARS}


async def test_long_word_paths_are_not_mistaken_for_personal_data() -> None:
    url = "https://example.org/docs/concepts/services-networking/network-policies/"
    assert await reason(url) is None
    assert (
        await reason("https://example.org/a/very-long-slug-made-of-many-plain-words-only") is None
    )


async def test_unknown_arguments_are_refused() -> None:
    out, _ = await guard().check(AGENT, {"url": "https://example.org/", "headers": {"x": "y"}})
    assert out == "bad_arguments"


async def test_every_redirect_hop_is_checked() -> None:
    hops = {
        "https://example.org/a": "https://docs.example.org/b",
        "https://docs.example.org/b": "http://169.254.169.254/latest",
    }
    out, _ = await guard(hops).check(AGENT, {"url": "https://example.org/a"})
    assert out == "redirect_private_address"
    out, _ = await guard({"https://example.org/a": "file:///etc/passwd"}).check(
        AGENT, {"url": "https://example.org/a"}
    )
    assert out == "redirect_scheme_not_allowed"
    out, safe = await guard({"https://example.org/a": "/b"}).check(
        AGENT, {"url": "https://example.org/a"}
    )
    assert out is None and safe["url"] == "https://example.org/b"


async def test_redirect_loops_are_bounded() -> None:
    loop = {"https://example.org/a": "https://example.org/b", "https://example.org/b": "/a"}
    out, _ = await guard(loop).check(AGENT, {"url": "https://example.org/a"})
    assert out == "too_many_redirects"


async def test_values_from_the_run_context_cannot_leave_in_a_url() -> None:
    context = (
        "Owner balance: 4,812.33 USD\n"
        "PHQ-9 score 117 this week\n"
        "Contact phone 555 867 5309\n"
        "Topic: kubernetes 1.31 release notes\n"
    )
    assert {"4,812.33", "117"} <= context_values(context)
    assert await reason("https://example.org/?q=4812.33", context=context) == "url_context_data"
    assert await reason("https://example.org/?q=4,812.33", context=context) == "url_context_data"
    assert await reason("https://example.org/s/117", context=context) == "url_context_data"
    assert await reason("https://example.org/555/867/5309", context=context) == "url_context_data"
    assert await reason("https://example.org/k8s/1.31", context=context) is None


async def test_a_url_named_in_the_prompt_can_be_fetched() -> None:
    url = "https://kubernetes.io/docs/concepts/services-networking/network-policies/"
    context = f"If useful, fetch {url} and check health with omniroute_get_health."
    assert await reason(url.replace("kubernetes.io", "example.org"), context=context) is None
    token = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8"
    assert await reason(f"https://example.org/?t={token}", context=f"key {token}") == (
        "url_personal_data"
    )


async def test_sensitive_runs_fetch_only_allowlisted_hosts() -> None:
    allow = frozenset({"docs.example.org"})
    out, _ = await guard(allow=allow).check(FINANCE, {"url": "https://example.org/"})
    assert out == "sensitive_run"
    out, _ = await guard(allow=allow).check(AGENT, {"url": "https://example.org/"}, sensitive=True)
    assert out == "sensitive_run"
    out, _ = await guard(allow=allow).check(FINANCE, {"url": "https://docs.example.org/"})
    assert out is None
    hop = {"https://docs.example.org/": "https://example.org/"}
    out, _ = await guard(hop, allow=allow).check(FINANCE, {"url": "https://docs.example.org/"})
    assert out == "sensitive_run"


def test_address_classification() -> None:
    assert address_is_public("93.184.215.14") and address_is_public("2606:4700::1111")
    for private in ("10.42.1.1", "172.16.0.1", "fe80::1", "fc00::1", "224.0.0.1", "0.0.0.0"):
        assert not address_is_public(private)


def _hub(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body["method"] == "initialize":
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
    if body["method"].startswith("notifications/"):
        return httpx.Response(202)
    seen.append(body["params"]["arguments"])
    text = "x" * (MAX_RESPONSE_CHARS * 2)
    payload = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": text}]}}
    return httpx.Response(200, json=payload)


seen: list[dict[str, Any]] = []


async def test_gateway_audits_host_and_reason_only_and_caps_the_response() -> None:
    seen.clear()
    gw = ToolGateway(
        hub_url="http://hub.test",
        hub_token="t",
        omniroute_key="",
        transport=httpx.MockTransport(_hub),
        fetch_guard=guard(),
    )
    with structlog.testing.capture_logs() as logs:
        ok = await gw.call(AGENT, "fetch", "fetch", {"url": "https://example.org/a?x=1"})
        bad = await gw.call(AGENT, "fetch", "fetch", {"url": "https://example.org/?m=a@b.co"})
    assert ok["ok"] and len(ok["result"]) <= MAX_RESPONSE_CHARS + 20
    assert seen == [{"url": "https://example.org/a?x=1", "max_length": MAX_RESPONSE_CHARS}]
    assert bad == {"ok": False, "denied_reason": "url_personal_data"}
    audit = [e for e in logs if e["event"] == "agent_tools.fetch"]
    assert [(e["host"], e["outcome"], e["reason"]) for e in audit] == [
        ("example.org", "allowed", "ok"),
        ("example.org", "refused", "url_personal_data"),
    ]
    dumped = json.dumps(logs, default=str)
    assert "x=1" not in dumped and "a@b.co" not in dumped and "/a" not in dumped
