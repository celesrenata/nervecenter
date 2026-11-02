"""Durable inference provider + combo coverage (providers feature).

Covers the persistent registry that replaces the read-only env-driven
``GET /api/inference/providers``:

- :class:`InferenceProviderService` — CRUD with the in-band ``api_key``
  masked on every projection; live test-connection and model-recall.
- :class:`InferenceComboService` — CRUD; bound-agent cascade preview and
  unbind on delete.
- :class:`AgentService.get_inference` — lazy combo resolution: a bound
  combo wins over the per-agent ``inference`` jsonb, and re-pointing the
  combo re-binds the agent with no write.

Pattern mirrors :mod:`tests.backend.test_agents_edit` (in-memory SQLite +
StaticPool, ``app.state.db_engine``) and :mod:`test_connections_auth` (real
signed JWT + in-process session store for the ``current_access`` gate), so the
write path is exercised against a real DB — no fabricated success.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.auth_service import (
    ACCESS_TOKEN_TTL,
    TYP_ACCESS,
    AuthContext,
    record_session,
    reset_memory_store,
)
from backend.api.endpoints import inference_durable
from backend.persistence.connection import make_session_factory
from backend.persistence.models import (
    Agent as AgentORM,
    InferenceCombo,
    InferenceProvider,
)
from backend.services.inference_providers import (
    InferenceComboService,
    InferenceProviderService,
    ProviderNotFoundError,
)
from backend.services.incident_agent import AgentService

JWT_SECRET = "inference-durable-test-" + "k" * 48
JWT_ISSUER = "https://sso.test.example/application/o/nervecenter/"
SUB = "test-principal"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    """In-memory SQLite engine with the providers/combos/agents tables."""
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        # InferenceProvider first (its table has no FKs), then InferenceCombo,
        # then Agent (combo_id is a plain string, no DB-level FK).
        await conn.run_sync(InferenceProvider.__table__.create)
        await conn.run_sync(InferenceCombo.__table__.create)
        await conn.run_sync(AgentORM.__table__.create)
    yield eng
    await eng.dispose()


@pytest.fixture
def provider_service(engine) -> InferenceProviderService:
    return InferenceProviderService(
        engine, session_factory=make_session_factory(engine)
    )


@pytest.fixture
def combo_service(engine) -> InferenceComboService:
    return InferenceComboService(
        engine, session_factory=make_session_factory(engine)
    )


@pytest.fixture
def agent_service(engine) -> AgentService:
    return AgentService(engine, session_factory=make_session_factory(engine))


@pytest.fixture
def auth_ctx() -> AuthContext:
    reset_memory_store()
    return AuthContext(
        secret=JWT_SECRET,
        issuer=JWT_ISSUER,
        client_id="inference-durable-test-client",
        local_admin_enabled=False,
        local_admin_password=None,
        session_factory=None,
    )


async def _token(auth_ctx: AuthContext) -> str:
    token, exp, claims = auth_ctx._mint(
        sub=SUB, typ=TYP_ACCESS, roles=["admin"], ttl=ACCESS_TOKEN_TTL
    )
    await record_session(
        auth_ctx, jti=claims["jti"], sub=SUB, typ=TYP_ACCESS, expires_at=exp, source="test"
    )
    return token


def _app(engine, auth_ctx) -> FastAPI:
    app = FastAPI()
    app.include_router(inference_durable.router)
    app.state.db_engine = engine
    app.state.auth_ctx = auth_ctx
    return app


# ---------------------------------------------------------------------------
# ProviderService
# ---------------------------------------------------------------------------


class TestProviderService:
    async def test_create_masks_key_on_read(self, provider_service):
        row = await provider_service.create(
            name="OpenAI Prod",
            provider_type="openai",
            base_url="https://api.openai.com/v1",
            api_key="sk-secret-value",
            models=["gpt-4o"],
        )
        # The key is stored but never echoed on any projection.
        assert row["api_key"] == "••••••••"
        assert row["key_configured"] is True
        assert row["models"] == ["gpt-4o"]
        assert row["model_count"] == 1
        # get() masks too
        fetched = await provider_service.get(row["id"])
        assert fetched["api_key"] == "••••••••"
        # list() masks too
        listed = await provider_service.list()
        assert [r["api_key"] for r in listed] == ["••••••••"]

    async def test_no_key_yields_null_mask(self, provider_service):
        row = await provider_service.create(name="Local", provider_type="local")
        assert row["api_key"] is None
        assert row["key_configured"] is False

    async def test_get_unknown_raises(self, provider_service):
        with pytest.raises(ProviderNotFoundError):
            await provider_service.get("does-not-exist")

    async def test_duplicate_name_conflict(self, provider_service):
        await provider_service.create(name="Dup", provider_type="openai")
        from backend.services.inference_providers import ProviderNameConflictError

        with pytest.raises(ProviderNameConflictError):
            await provider_service.create(name="Dup", provider_type="anthropic")

    async def test_update_clears_key_with_empty_string(
        self, provider_service
    ):
        row = await provider_service.create(
            name="ClearMe", provider_type="openai", api_key="sk-abc"
        )
        updated = await provider_service.update(row["id"], api_key="")
        assert updated["key_configured"] is False
        assert updated["api_key"] is None

    async def test_update_ignores_sentinel_key(self, provider_service):
        row = await provider_service.create(
            name="Sentinel", provider_type="openai", api_key="sk-keep"
        )
        # Posting the mask back must NOT clear the stored key.
        updated = await provider_service.update(row["id"], api_key="••••••••")
        assert updated["key_configured"] is True

    async def test_delete_removes_row(self, provider_service):
        row = await provider_service.create(name="Gone", provider_type="openai")
        await provider_service.delete(row["id"])
        with pytest.raises(ProviderNotFoundError):
            await provider_service.get(row["id"])

    async def test_parse_models_openai_shape(self):
        svc = InferenceProviderService.__new__(InferenceProviderService)
        data = {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]}
        assert svc._parse_models("openai", data) == ["gpt-4o", "gpt-4o-mini"]

    async def test_parse_models_ollama_shape(self):
        svc = InferenceProviderService.__new__(InferenceProviderService)
        data = {"models": [{"name": "llama3.1"}, {"name": "phi3"}]}
        assert svc._parse_models("local", data) == ["llama3.1", "phi3"]


# ---------------------------------------------------------------------------
# ComboService
# ---------------------------------------------------------------------------


class TestComboService:
    async def test_create_resolves_provider_name(self, combo_service, provider_service):
        prov = await provider_service.create(name="OpenAI", provider_type="openai")
        row = await combo_service.create(
            name="gpt4o", provider_id=prov["id"], model_id="gpt-4o"
        )
        assert row["provider_name"] == "OpenAI"
        assert row["model_id"] == "gpt-4o"

    async def test_create_unknown_provider_raises(self, combo_service):
        from backend.services.inference_providers import ProviderNotFoundError as PNFE

        with pytest.raises(PNFE):
            await combo_service.create(
                name="x", provider_id="nope", model_id="m"
            )

    async def test_list_returns_provider_names(self, combo_service, provider_service):
        p1 = await provider_service.create(name="A", provider_type="openai")
        p2 = await provider_service.create(name="B", provider_type="anthropic")
        await combo_service.create(name="c1", provider_id=p1["id"], model_id="m1")
        await combo_service.create(name="c2", provider_id=p2["id"], model_id="m2")
        rows = await combo_service.list()
        names = {r["name"]: r["provider_name"] for r in rows}
        assert names == {"c1": "A", "c2": "B"}

    async def test_bound_agents_and_unbind_on_delete(
        self, combo_service, provider_service, agent_service
    ):
        prov = await provider_service.create(name="P", provider_type="openai")
        combo = await combo_service.create(
            name="bind", provider_id=prov["id"], model_id="gpt-4o"
        )
        await agent_service.create(
            name="agent-1", agent_type="planner", combo_id=combo["id"]
        )
        bound = await combo_service.list_bound_agents(combo["id"])
        assert [b["name"] for b in bound] == ["agent-1"]
        # Delete the combo → the agent is unbound (combo_id → None), not deleted.
        await combo_service.delete(combo["id"])
        listed = await agent_service.list_persisted()
        assert [a["combo_id"] for a in listed if a["name"] == "agent-1"] == [None]


# ---------------------------------------------------------------------------
# Agent combo resolution
# ---------------------------------------------------------------------------


class TestAgentComboResolution:
    async def test_combo_wins_over_jsonb(self, agent_service, combo_service, provider_service):
        prov = await provider_service.create(
            name="OpenAI", provider_type="openai", base_url="https://api.openai.com/v1"
        )
        combo = await combo_service.create(
            name="cb", provider_id=prov["id"], model_id="combo-model"
        )
        agent = await agent_service.create(
            name="a1",
            agent_type="planner",
            combo_id=combo["id"],
            inference={"provider": "omniroute", "model": "jsonb-model"},
        )
        binding = await agent_service.get_inference(agent["id"])
        # The combo (openai / combo-model) shadows the per-agent jsonb.
        assert binding == {"provider": "openai", "model": "combo-model"}

    async def test_jsonb_used_when_no_combo(self, agent_service):
        agent = await agent_service.create(
            name="a2",
            agent_type="planner",
            inference={"provider": "omniroute", "model": "jsonb-model"},
        )
        binding = await agent_service.get_inference(agent["id"])
        assert binding == {"provider": "omniroute", "model": "jsonb-model"}

    async def test_repointing_combo_rebinds_agent(self, agent_service, combo_service, provider_service):
        p1 = await provider_service.create(name="P1", provider_type="openai")
        p2 = await provider_service.create(name="P2", provider_type="anthropic")
        combo = await combo_service.create(
            name="cb", provider_id=p1["id"], model_id="m1"
        )
        agent = await agent_service.create(
            name="a3", agent_type="planner", combo_id=combo["id"]
        )
        assert await agent_service.get_inference(agent["id"]) == {
            "provider": "openai",
            "model": "m1",
        }
        # Re-point the combo (no agent write) → the agent now resolves P2/m2.
        await combo_service.update(
            combo["id"], provider_id=p2["id"], model_id="m2"
        )
        assert await agent_service.get_inference(agent["id"]) == {
            "provider": "anthropic",
            "model": "m2",
        }

    async def test_dangling_combo_falls_back_to_jsonb(
        self, agent_service, combo_service, provider_service
    ):
        prov = await provider_service.create(name="P", provider_type="openai")
        combo = await combo_service.create(
            name="cb", provider_id=prov["id"], model_id="m1"
        )
        agent = await agent_service.create(
            name="a4",
            agent_type="planner",
            combo_id=combo["id"],
            inference={"provider": "omniroute", "model": "fb-model"},
        )
        # Delete the combo → dangling combo_id; resolution falls back to jsonb.
        await combo_service.delete(combo["id"])
        assert await agent_service.get_inference(agent["id"]) == {
            "provider": "omniroute",
            "model": "fb-model",
        }

    async def test_update_rebinds_and_clears(self, agent_service, combo_service, provider_service):
        prov = await provider_service.create(name="P", provider_type="openai")
        combo = await combo_service.create(
            name="cb", provider_id=prov["id"], model_id="m1"
        )
        agent = await agent_service.create(name="a5", agent_type="planner")
        updated = await agent_service.update(agent["id"], combo_id=combo["id"])
        assert updated["combo_id"] == combo["id"]
        # Clear with an empty string.
        cleared = await agent_service.update(agent["id"], combo_id="")
        assert cleared["combo_id"] is None


# ---------------------------------------------------------------------------
# Durable endpoints (auth + degradation)
# ---------------------------------------------------------------------------


class TestDurableEndpoints:
    async def test_mutating_without_token_is_401(self, engine, auth_ctx):
        app = _app(engine, auth_ctx)
        with TestClient(app) as client:
            resp = client.post(
                "/api/inference/durable/providers",
                json={"name": "x", "provider_type": "openai"},
            )
        assert resp.status_code == 401
        assert resp.json() == {"detail": "missing bearer token"}

    async def test_no_db_engine_is_503(self, auth_ctx):
        # No db_engine on state → honest 503, not a fabricated acceptance.
        app = FastAPI()
        app.include_router(inference_durable.router)
        app.state.auth_ctx = auth_ctx
        with TestClient(app) as client:
            token = await _token(auth_ctx)
            resp = client.post(
                "/api/inference/durable/providers",
                json={"name": "x", "provider_type": "openai"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 503

    async def test_provider_crud_via_api(self, engine, auth_ctx):
        app = _app(engine, auth_ctx)
        with TestClient(app) as client:
            token = await _token(auth_ctx)
            h = {"Authorization": f"Bearer {token}"}

            created = client.post(
                "/api/inference/durable/providers",
                json={
                    "name": "OpenAI",
                    "provider_type": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-x",
                    "models": ["gpt-4o"],
                },
                headers=h,
            )
            assert created.status_code == 201, created.text
            pid = created.json()["id"]
            # The API response masks the key too.
            assert created.json()["api_key"] == "••••••••"

            # Unsupported provider_type → 422
            bad = client.post(
                "/api/inference/durable/providers",
                json={"name": "Bad", "provider_type": "warpdrive"},
                headers=h,
            )
            assert bad.status_code == 422

            # Name conflict → 409
            dup = client.post(
                "/api/inference/durable/providers",
                json={"name": "OpenAI", "provider_type": "openai"},
                headers=h,
            )
            assert dup.status_code == 409

            # GET list
            listed = client.get("/api/inference/durable/providers")
            assert listed.status_code == 200
            assert len(listed.json()) == 1

            # PATCH
            patched = client.patch(
                f"/api/inference/durable/providers/{pid}",
                json={"base_url": "https://new.example/v1"},
                headers=h,
            )
            assert patched.status_code == 200, patched.text

            # DELETE
            deleted = client.delete(f"/api/inference/durable/providers/{pid}", headers=h)
            assert deleted.status_code == 204
            # Gone
            gone = client.get(f"/api/inference/durable/providers/{pid}")
            assert gone.status_code == 404

    async def test_combo_crud_via_api(self, engine, auth_ctx):
        app = _app(engine, auth_ctx)
        with TestClient(app) as client:
            token = await _token(auth_ctx)
            h = {"Authorization": f"Bearer {token}"}

            prov = client.post(
                "/api/inference/durable/providers",
                json={"name": "OpenAI", "provider_type": "openai"},
                headers=h,
            )
            pid = prov.json()["id"]

            # Combo with unknown provider → 404
            bad = client.post(
                "/api/inference/durable/combos",
                json={"name": "c", "provider_id": "nope", "model_id": "m"},
                headers=h,
            )
            assert bad.status_code == 404

            combo = client.post(
                "/api/inference/durable/combos",
                json={"name": "gpt4o", "provider_id": pid, "model_id": "gpt-4o"},
                headers=h,
            )
            assert combo.status_code == 201, combo.text
            cid = combo.json()["id"]
            assert combo.json()["provider_name"] == "OpenAI"

            # Name conflict → 409
            dup = client.post(
                "/api/inference/durable/combos",
                json={"name": "gpt4o", "provider_id": pid, "model_id": "gpt-4o"},
                headers=h,
            )
            assert dup.status_code == 409

            # Bound agents (empty)
            agents = client.get(f"/api/inference/durable/combos/{cid}/agents")
            assert agents.status_code == 200
            assert agents.json() == []

            # PATCH (re-point model)
            patched = client.patch(
                f"/api/inference/durable/combos/{cid}",
                json={"model_id": "gpt-4o-mini"},
                headers=h,
            )
            assert patched.status_code == 200
            assert patched.json()["model_id"] == "gpt-4o-mini"

            # DELETE
            deleted = client.delete(f"/api/inference/durable/combos/{cid}", headers=h)
            assert deleted.status_code == 204

    async def test_read_degrades_without_auth_ok(self, engine, auth_ctx):
        """Read routes are open (200) when a DB engine is wired, even unauthenticated —
        only mutating routes are auth-gated."""
        app = _app(engine, auth_ctx)
        with TestClient(app) as client:
            resp = client.get("/api/inference/durable/providers")
        assert resp.status_code == 200
        assert resp.json() == []
