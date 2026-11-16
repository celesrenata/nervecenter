"""Skill Hub catalog client (NS-ADR-111): approved-only, budget, pinning, TTL, degradation."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from backend.agents.runner.skillhub import SkillHubClient

pytestmark = pytest.mark.asyncio


class Hub:
    """A fake catalog: records every request and serves search and load."""

    def __init__(self, hits: list[dict[str, Any]], content: dict[str, str] | None = None) -> None:
        self.hits = hits
        self.content = content or {h["id"]: f"body of {h['id']}" for h in hits}
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.fail: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body or dict(request.url.params)))
        if self.fail:
            return httpx.Response(self.fail)
        if request.method == "GET":
            return httpx.Response(200, json={"results": self.hits})
        skill_id = body["id"]
        if skill_id not in self.content:
            return httpx.Response(404, json={"found": False})
        return httpx.Response(
            200,
            json={
                "found": True,
                "id": skill_id,
                "name": skill_id,
                "revision": "2.0.0",
                "content": self.content[skill_id],
            },
        )

    def client(self, **kw: Any) -> SkillHubClient:
        return SkillHubClient(
            key="test-key",
            url="https://hub.test",
            transport=httpx.MockTransport(self.handler),
            **kw,
        )

    def posted_actions(self) -> list[str]:
        return [b.get("action") for m, _, b in self.requests if m == "POST"]


def hit(skill_id: str, status: str = "approved") -> dict[str, Any]:
    return {"id": skill_id, "name": skill_id, "revision": "1.0.0", "reviewStatus": status}


async def test_only_approved_skills_load_and_are_recorded() -> None:
    hub = Hub([hit("a"), hit("b", "edit"), hit("c", "chain"), hit("d")])
    sel = await hub.client().select("planner agent", "inspect kubernetes deployment")
    assert sel.status == "loaded"
    assert [u["id"] for u in sel.used] == ["a", "d"]
    assert all(u["review_status"] == "approved" and u["revision"] == "2.0.0" for u in sel.used)
    assert "body of a" in sel.block and "body of b" not in sel.block


async def test_budget_cuts_off_loading() -> None:
    hub = Hub([hit("a"), hit("b"), hit("c")], {k: "x" * 900 for k in "abc"})
    sel = await hub.client(budget_chars=1_000, max_skills=3).select("role", "kubernetes task")
    assert [u["id"] for u in sel.used] == ["a"]
    assert sel.used[0]["chars"] == 900
    sel = await hub.client(budget_chars=1_500, max_skills=3).select("role", "other kubernetes")
    assert sum(u["chars"] for u in sel.used) <= 1_500


async def test_max_skills_limit() -> None:
    hub = Hub([hit(c) for c in "abcd"])
    sel = await hub.client(max_skills=2).select("role", "kubernetes task")
    assert len(sel.used) == 2


async def test_pinned_edit_skill_loads_but_an_unpinned_edit_hit_does_not() -> None:
    hub = Hub(
        [hit("e", "edit"), hit("a")], {"e": "edit body", "a": "approved body", "p": "pinned body"}
    )
    sel = await hub.client().select("role", "kubernetes task", pinned=["p"])
    assert [u["id"] for u in sel.used] == ["p", "a"]
    assert sel.used[0]["review_status"] == "pinned"
    assert "e" not in [u["id"] for u in sel.used]


async def test_cache_hit_then_expiry() -> None:
    now = [0.0]
    hub = Hub([hit("a")])
    client = hub.client(ttl_s=60, clock=lambda: now[0])
    await client.select("role", "kubernetes task")
    first = len(hub.requests)
    await client.select("role", "kubernetes task")
    assert len(hub.requests) == first  # served from cache
    now[0] = 120.0
    await client.select("role", "kubernetes task")
    assert len(hub.requests) == 2 * first  # expired, fetched again


@pytest.mark.parametrize("status", [401, 500])
async def test_http_failure_degrades_to_unavailable(status: int) -> None:
    hub = Hub([hit("a")])
    hub.fail = status
    sel = await hub.client().select("role", "kubernetes task")
    assert sel.status == "unavailable" and sel.used == [] and sel.block == ""


async def test_timeout_degrades_to_unavailable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = SkillHubClient(key="k", url="https://hub.test", transport=httpx.MockTransport(boom))
    sel = await client.select("role", "kubernetes task")
    assert sel.status == "unavailable"


async def test_no_hits_is_none_and_unconfigured_is_reported() -> None:
    assert (await Hub([]).client().select("role", "kubernetes task")).status == "none"
    assert (await SkillHubClient(key="").select("role", "task")).status == "unconfigured"


async def test_execute_action_is_never_sent() -> None:
    hub = Hub([hit("a"), hit("b", "edit")])
    client = hub.client()
    await client.select("role", "kubernetes task", pinned=["b"])
    await client.skills_context("kubernetes")
    assert set(hub.posted_actions()) == {"load"}
    assert all("execute" not in path for _, path, _ in hub.requests)


async def test_search_uses_bearer_key_and_bounded_limit() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    client = SkillHubClient(
        key="k1", url="https://hub.test", transport=httpx.MockTransport(handler)
    )
    await client.select("role", "kubernetes task")
    assert seen[0].headers["authorization"] == "Bearer k1"
    assert int(seen[0].url.params["limit"]) <= 20
