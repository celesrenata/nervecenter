"""Agent edit coverage (persisted agent instances).

Covers the agent *detail + edit* slice for persisted instances:

- ``AgentService.update`` — partial update (only provided fields change,
  others preserved); name conflict → ``AgentNameConflictError``.
- ``PATCH /api/agents/{id}`` — 200 with updated fields; ``404`` on unknown id;
  ``409`` on duplicate name; ``503`` when no DB engine is configured.
- ``GET /agents/{id}`` — now resolves a *persisted* instance, not just native
  types.

Pattern mirrors :mod:`tests.backend.test_write_routes_gap3` (in-memory SQLite
+ StaticPool, ``app.state.db_engine``), so the write path is exercised against
a real DB — no fabricated success.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.endpoints import agents as agents_ep
from backend.persistence.connection import make_session_factory
from backend.persistence.models import Agent as AgentORM
from backend.services.incident_agent import (
    AgentNameConflictError,
    AgentNotFoundError,
    AgentService,
)


@pytest.fixture
async def async_engine():
    """In-memory SQLite async engine; creates the agents table."""
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        await conn.run_sync(AgentORM.__table__.create)
    yield eng
    await eng.dispose()


@pytest.fixture
async def agent_service(async_engine) -> AgentService:
    """AgentService over the in-memory engine (real write path)."""
    return AgentService(async_engine, session_factory=make_session_factory(async_engine))


@pytest.fixture
def app_with_db(async_engine):
    """App with the agents router, DB engine wired (legacy + canonical routes)."""
    app = FastAPI()
    app.include_router(agents_ep.router)
    app.state.db_engine = async_engine
    return app


@pytest.fixture
def app_no_db():
    """App with the agents router but no DB engine (honest degradation)."""
    app = FastAPI()
    app.include_router(agents_ep.router)
    return app


async def _seed(svc: AgentService, *, name: str = "scout-1", **overrides) -> dict:
    """Create one persisted agent instance; returns the web row."""
    payload = {
        "name": name,
        "agent_type": "planner",
        "description": "original description",
        "prompt_template": "You are a helpful planner.",
        "tools": ["read_file", "search"],
    }
    payload.update(overrides)
    return await svc.create(**payload)


# ─── AgentService.update (direct service, real DB) ─────────────────────────────


class TestServiceUpdate:
    async def test_partial_update_changes_only_provided_fields(self, agent_service):
        row = await _seed(agent_service)
        agent_id = row["id"]

        # Only `description` provided → name/prompt_template/tools preserved.
        updated = await agent_service.update(agent_id, description="new description")
        assert updated["id"] == agent_id
        assert updated["name"] == "scout-1"
        assert updated["description"] == "new description"
        assert updated["prompt_template"] == "You are a helpful planner."
        assert updated["tools"] == ["read_file", "search"]
        # status is lifecycle, not editable — unchanged.
        assert updated["status"] == "active"

    async def test_update_multiple_fields_and_nulls_preserved(self, agent_service):
        row = await _seed(agent_service)
        agent_id = row["id"]

        updated = await agent_service.update(
            agent_id,
            name="scout-2",
            prompt_template="New prompt",
            tools=["exec"],
        )
        assert updated["name"] == "scout-2"
        assert updated["prompt_template"] == "New prompt"
        assert updated["tools"] == ["exec"]
        # `description` not provided → preserved from seed.
        assert updated["description"] == "original description"

    async def test_update_unknown_id_raises_not_found(self, agent_service):
        with pytest.raises(AgentNotFoundError):
            await agent_service.update(
                "00000000-0000-4000-8000-000000000000", description="x"
            )

    async def test_update_name_conflict_raises(self, agent_service):
        await _seed(agent_service, name="a")
        row = await _seed(agent_service, name="b")
        with pytest.raises(AgentNameConflictError):
            await agent_service.update(row["id"], name="a")


# ─── PATCH /api/agents/{id} (via TestClient, real DB) ──────────────────────────


class TestPatchEndpoint:
    async def test_patch_200_updates_fields(self, app_with_db):
        with TestClient(app_with_db) as client:
            created = client.post(
                "/api/agents",
                json={
                    "name": "scout-1",
                    "type": "planner",
                    "description": "d1",
                    "prompt_template": "p1",
                    "tools": ["read_file"],
                },
            )
            assert created.status_code == 201, created.text
            agent_id = created.json()["id"]

            resp = client.patch(
                f"/api/agents/{agent_id}",
                json={"description": "d2", "tools": ["exec"]},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["id"] == agent_id
            assert body["description"] == "d2"
            assert body["tools"] == ["exec"]
            # `prompt_template` not in payload → preserved.
            assert body["prompt_template"] == "p1"

    async def test_patch_unknown_id_is_404(self, app_with_db):
        with TestClient(app_with_db) as client:
            resp = client.patch(
                "/api/agents/00000000-0000-4000-8000-000000000000",
                json={"description": "x"},
            )
            assert resp.status_code == 404, resp.text

    async def test_patch_duplicate_name_is_409(self, app_with_db):
        with TestClient(app_with_db) as client:
            client.post("/api/agents", json={"name": "a", "type": "planner"})
            created = client.post(
                "/api/agents", json={"name": "b", "type": "planner"}
            )
            assert created.status_code == 201, created.text
            agent_id = created.json()["id"]

            resp = client.patch(f"/api/agents/{agent_id}", json={"name": "a"})
            assert resp.status_code == 409, resp.text

    async def test_patch_no_db_is_503(self, app_no_db):
        with TestClient(app_no_db) as client:
            resp = client.patch(
                "/api/agents/00000000-0000-4000-8000-000000000000",
                json={"description": "x"},
            )
            assert resp.status_code == 503, resp.text

    async def test_patch_extra_field_is_422(self, app_with_db):
        with TestClient(app_with_db) as client:
            created = client.post(
                "/api/agents", json={"name": "c", "type": "planner"}
            )
            assert created.status_code == 201, created.text
            agent_id = created.json()["id"]
            resp = client.patch(
                f"/api/agents/{agent_id}",
                json={"description": "x", "bogus": 1},
            )
            assert resp.status_code == 422, resp.text


# ─── GET /agents/{id} now resolves persisted instances ─────────────────────────


class TestGetResolvesPersisted:
    async def test_get_resolves_persisted_instance(self, app_with_db):
        with TestClient(app_with_db) as client:
            created = client.post(
                "/api/agents",
                json={"name": "scout-1", "type": "planner", "description": "desc"},
            )
            assert created.status_code == 201, created.text
            agent_id = created.json()["id"]

            # The persisted id is NOT a native type → must resolve from the DB.
            resp = client.get(f"/agents/{agent_id}")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["id"] == agent_id
            assert body["name"] == "scout-1"
            assert body["type"] == "planner"
            assert body["description"] == "desc"

    async def test_get_native_type_still_works(self, app_with_db):
        with TestClient(app_with_db) as client:
            resp = client.get("/agents/planner")
            assert resp.status_code == 200, resp.text
            assert resp.json()["type"] == "planner"

    async def test_get_unknown_id_is_404(self, app_with_db):
        with TestClient(app_with_db) as client:
            resp = client.get("/agents/00000000-0000-4000-8000-000000000000")
            assert resp.status_code == 404, resp.text

    async def test_get_nonnative_id_no_db_is_404(self, app_no_db):
        # No DB, non-native id → honest 404 (no fabricated agent).
        with TestClient(app_no_db) as client:
            resp = client.get("/agents/not-a-native-type")
            assert resp.status_code == 404, resp.text
