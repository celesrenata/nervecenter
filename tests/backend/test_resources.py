"""Owner resources, agent links and read-only agent tools (NS-ADR-125). Fakes only."""
from __future__ import annotations

import pathlib
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import backend.api.app  # noqa: F401 - import-order guard
from backend.agents.tools import policy
from backend.agents.tools.gateway import ToolGateway
from backend.agents.tools.nerve import NerveTools
from backend.persistence.connection import make_session_factory
from backend.persistence.models.resources import AgentResourceLink, Resource
from backend.resources.service import (
    MAX_BYTES,
    ResourceInputError,
    ResourceService,
    ResourceStoreUnavailable,
    ResourceTooLarge,
    clean_name,
)
from backend.storage.memory import InMemoryObjectStore

OWNER = {"sub": "owner", "typ": "access", "roles": ["owner"]}
DEVICE = {"sub": "device:mac", "typ": "device", "roles": ["device"]}


@pytest.fixture
async def svc(tmp_path: pathlib.Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Resource.__table__.metadata.create_all,
                            tables=[Resource.__table__, AgentResourceLink.__table__])
    store = InMemoryObjectStore()
    service = ResourceService(make_session_factory(engine), store)
    service._test_store = store  # type: ignore[attr-defined]
    yield service
    await engine.dispose()


def _app(svc: ResourceService, claims: dict[str, Any] | None) -> FastAPI:
    from backend.api.auth import current_access
    from backend.api.endpoints import resources as ep

    app = FastAPI()
    app.include_router(ep.router)
    app.state.resource_service = svc
    if claims is not None:
        app.dependency_overrides[current_access] = lambda: claims
    return app


def _gateway(svc: ResourceService, blocked: bool = False) -> ToolGateway:
    gw = ToolGateway(hub_token="", omniroute_key="")
    gw.nerve = NerveTools(SimpleNamespace(state=SimpleNamespace(
        resource_service=svc, services={}, db_engine=None, nats_client=None)))

    async def nerve_blocked(agent_id: str) -> bool:
        return blocked

    gw.nerve_blocked = nerve_blocked
    return gw


def _agent(agent_type: str) -> Any:
    return SimpleNamespace(id=agent_type, type=agent_type, name=agent_type, tools=[])


async def _call(gw: ToolGateway, agent: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return await gw.call(_agent(agent), "nerve", tool, args, context="run1")


# -- service ---------------------------------------------------------------------------
async def test_put_list_download_delete_roundtrip(svc) -> None:
    row = await svc.put("../../etc/notes.md", b"# hello", uploaded_by="owner")
    assert row["name"] == "notes.md" and row["size"] == 7 and row["sensitivity"] == "normal"
    assert len(row["sha256"]) == 64 and row["content_type"] == "text/markdown"
    assert await svc._test_store.exists(f"resources/{row['id']}")
    assert [r["id"] for r in await svc.list_all()] == [row["id"]]
    view, data = await svc.download(row["id"])
    assert data == b"# hello" and view["id"] == row["id"]
    await svc.delete(row["id"])
    assert not await svc._test_store.exists(f"resources/{row['id']}")
    with pytest.raises(KeyError):
        await svc.get(row["id"])


async def test_size_cap_empty_and_bad_sensitivity(svc) -> None:
    with pytest.raises(ResourceTooLarge):
        await svc.put("big.bin", b"x" * (MAX_BYTES + 1))
    with pytest.raises(ResourceInputError):
        await svc.put("e.txt", b"")
    with pytest.raises(ResourceInputError):
        await svc.put("a.txt", b"a", sensitivity="public")
    assert (await svc.put("ok.txt", b"x" * MAX_BYTES))["size"] == MAX_BYTES


def test_name_is_sanitised() -> None:
    assert clean_name("a/b\\c<>.txt") == "c_.txt"
    assert clean_name("...") == "file" and clean_name(None) == "file"


async def test_unconfigured_store_is_503_not_a_crash(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("backend.ingest.supervisor._object_store_from_env", lambda: None)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'u.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Resource.__table__.metadata.create_all,
                            tables=[Resource.__table__, AgentResourceLink.__table__])
    service = ResourceService(make_session_factory(engine))
    with pytest.raises(ResourceStoreUnavailable):
        await service.put("a.txt", b"a")
    with TestClient(_app(service, OWNER)) as c:
        r = c.put("/api/resources", params={"name": "a.txt"}, content=b"abc")
        assert r.status_code == 503
    await engine.dispose()


async def test_links_are_many_to_many(svc) -> None:
    a = (await svc.put("a.txt", b"alpha"))["id"]
    b = (await svc.put("b.txt", b"beta"))["id"]
    await svc.set_links("planner", [a, b])
    await svc.set_links("verifier", [a])
    assert {r["id"] for r in await svc.get_links("planner")} == {a, b}
    assert {r["id"] for r in await svc.get_links("verifier")} == {a}
    await svc.set_links("planner", [b])
    assert {r["id"] for r in await svc.get_links("planner")} == {b}
    await svc.delete(a)
    assert await svc.get_links("verifier") == []
    with pytest.raises(KeyError):
        await svc.set_links("planner", ["missing"])


async def test_secret_resource_cannot_be_linked(svc) -> None:
    s = (await svc.put("fw.xml", b"<x/>", sensitivity="secret"))["id"]
    with pytest.raises(ResourceInputError, match="secret"):
        await svc.set_links("planner", [s])
    assert await svc.get_links("planner") == []


# -- HTTP ------------------------------------------------------------------------------
async def test_owner_api_end_to_end_and_people_only(svc) -> None:
    with TestClient(_app(svc, OWNER)) as c:
        r = c.put("/api/resources", params={"name": "n.txt", "sensitivity": "normal"},
                  content=b"hello")
        assert r.status_code == 201, r.text
        rid = r.json()["id"]
        assert c.get("/api/resources").json()["resources"][0]["id"] == rid
        d = c.get(f"/api/resources/{rid}/download")
        assert d.status_code == 200 and d.content == b"hello"
        assert "attachment" in d.headers["content-disposition"]
        assert c.put("/api/agents/planner/resources", json={"resource_ids": [rid]}
                     ).json()["resources"][0]["id"] == rid
        assert c.get("/api/agents/planner/resources").json()["resources"][0]["id"] == rid
        sec = c.put("/api/resources", params={"name": "s.xml", "sensitivity": "secret"},
                    content=b"<a/>").json()["id"]
        bad = c.put("/api/agents/planner/resources", json={"resource_ids": [sec]})
        assert bad.status_code == 422 and "secret" in bad.text
        assert c.put("/api/agents/planner/resources", json={"resource_ids": "x"}
                     ).status_code == 422
        assert c.delete(f"/api/resources/{rid}").status_code == 204
        assert c.delete(f"/api/resources/{rid}").status_code == 404
        assert c.put("/api/resources", params={"name": "z", "sensitivity": "weird"},
                     content=b"a").status_code == 422


async def test_upload_size_cap_over_http(svc) -> None:
    with TestClient(_app(svc, OWNER)) as c:
        r = c.put("/api/resources", params={"name": "big.bin"}, content=b"x" * (MAX_BYTES + 1))
        assert r.status_code == 413


async def test_device_and_anonymous_are_refused(svc) -> None:
    with TestClient(_app(svc, DEVICE)) as c:
        assert c.get("/api/resources").status_code == 403
        assert c.put("/api/resources", params={"name": "a"}, content=b"a").status_code == 403
        assert c.delete("/api/resources/x").status_code == 403
        assert c.get("/api/agents/planner/resources").status_code == 403
    # No override: the real default-deny gate answers.
    with TestClient(_app(svc, None)) as c:
        assert c.get("/api/resources").status_code in (401, 403)
        assert c.put("/api/agents/planner/resources", json={}).status_code in (401, 403)


# -- agent tools -------------------------------------------------------------------------
def test_grants_follow_the_plan() -> None:
    for t in ("planner", "research_agent", "context_critic", "verifier"):
        assert {"resources_list", "resources_read"} <= policy.nerve_tools_for(_agent(t))
    assert "resources_list" not in policy.nerve_tools_for(_agent("route_critic"))


async def test_tools_see_only_linked_normal_resources(svc) -> None:
    text = "line\n" * 2_000
    a = (await svc.put("big.txt", text.encode()))["id"]
    other = (await svc.put("other.txt", b"not linked"))["id"]
    binary = (await svc.put("img.png", b"\x89PNG\r\n\x1a\n\x00\x00"))["id"]
    await svc.set_links("planner", [a, binary])
    gw = _gateway(svc)
    listed = await _call(gw, "planner", "resources_list", {})
    assert listed["ok"] and {i["id"] for i in listed["result"]["items"]} == {a, binary}
    page = await _call(gw, "planner", "resources_read", {"resource_id": a, "limit": 99_999})
    res = page["result"]
    assert page["ok"] and len(res["text"]) == 5_000 and res["next_offset"] == 5_000
    assert res["total_chars"] == len(text)
    nxt = await _call(gw, "planner", "resources_read",
                      {"resource_id": a, "offset": res["next_offset"]})
    assert nxt["result"]["offset"] == 5_000
    last = await _call(gw, "planner", "resources_read", {"resource_id": a, "offset": 9_990})
    assert last["result"]["next_offset"] is None
    unlinked = await _call(gw, "planner", "resources_read", {"resource_id": other})
    assert unlinked == {"ok": False, "denied_reason": "resource_not_linked"}
    not_text = await _call(gw, "planner", "resources_read", {"resource_id": binary})
    assert not_text["denied_reason"] == "resource_not_text"
    wrong_agent = await _call(gw, "verifier", "resources_read", {"resource_id": a})
    assert wrong_agent["denied_reason"] == "resource_not_linked"


async def test_agent_with_no_links_sees_nothing(svc) -> None:
    await svc.put("a.txt", b"alpha")
    gw = _gateway(svc)
    listed = await _call(gw, "verifier", "resources_list", {})
    assert listed["ok"] and listed["result"]["items"] == []
    missing = await _call(gw, "verifier", "resources_read", {})
    assert missing["denied_reason"] == "missing_resource_id"


async def test_secret_is_never_read_even_if_a_link_row_exists(svc) -> None:
    s = (await svc.put("fw.xml", b"<secret/>", sensitivity="secret"))["id"]
    async with svc._sessions() as session:  # a stale or hand-made link row
        session.add(AgentResourceLink(agent_id="planner", resource_id=s))
        await session.commit()
    gw = _gateway(svc)
    assert (await _call(gw, "planner", "resources_list", {}))["result"]["items"] == []
    denied = await _call(gw, "planner", "resources_read", {"resource_id": s})
    assert denied == {"ok": False, "denied_reason": "resource_not_linked"}


async def test_tools_refused_on_openai_routes_and_for_ungranted_types(svc) -> None:
    a = (await svc.put("a.txt", b"alpha"))["id"]
    await svc.set_links("planner", [a])
    blocked = _gateway(svc, blocked=True)
    out = await _call(blocked, "planner", "resources_read", {"resource_id": a})
    assert out == {"ok": False, "denied_reason": "nerve_blocked_openai_route"}
    open_gw = _gateway(svc)
    denied = await _call(open_gw, "route_critic", "resources_list", {})
    assert denied == {"ok": False, "denied_reason": "tool_not_allowed"}
