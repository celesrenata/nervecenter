"""OmniRoute key management (NS-ADR-117, NS-ADR-118): resolver, endpoints, consumers."""

from __future__ import annotations

import json

import httpx
import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.agents.runner.skillhub import SkillHubClient
from backend.agents.tools.gateway import ToolGateway
from backend.api.auth import current_access
from backend.api.endpoints import omniroute_keys as ep
from backend.connectors.vault import FernetVault, generate_key
from backend.inference import key_resolver as kr
from backend.inference.key_store import OmniRouteKeyStore
from backend.inference.omniroute import OmniRouteTransport
from backend.persistence.connection import make_session_factory
from backend.persistence.models.omniroute_keys import OmniRouteKeyAudit
from backend.persistence.models.personal import SecretRow
from backend.persistence.repositories import RepositoryRegistry

ENV_REGULAR = "env-regular-key-0001"
ENV_PRIV = "env-privileged-key-0002"
VAULT_REGULAR = "vault-regular-key-9991"
VAULT_PRIV = "vault-privileged-key-9992"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (kr.REGULAR_ENV, kr.PRIVILEGED_ENV, kr.SKILLHUB_ENV):
        monkeypatch.delenv(name, raising=False)
    kr.reset()
    yield
    kr.reset()


# -- resolver precedence -----------------------------------------------------------


def test_vault_overrides_env_and_clearing_falls_back(monkeypatch):
    monkeypatch.setenv(kr.REGULAR_ENV, ENV_REGULAR)
    monkeypatch.setenv(kr.PRIVILEGED_ENV, ENV_PRIV)
    assert kr.regular_key() == ENV_REGULAR and kr.source_for(kr.REGULAR) == "env"
    kr.apply_vault_values({kr.REGULAR: VAULT_REGULAR, kr.PRIVILEGED: VAULT_PRIV})
    assert kr.regular_key() == VAULT_REGULAR and kr.privileged_key() == VAULT_PRIV
    assert kr.source_for(kr.REGULAR) == "vault"
    kr.apply_vault_values({})
    assert kr.regular_key() == ENV_REGULAR and kr.privileged_key() == ENV_PRIV


def test_skillhub_order_is_explicit_then_privileged_then_regular(monkeypatch):
    kr.apply_vault_values({kr.REGULAR: VAULT_REGULAR})
    assert kr.skillhub_key() == VAULT_REGULAR
    kr.apply_vault_values({kr.REGULAR: VAULT_REGULAR, kr.PRIVILEGED: VAULT_PRIV})
    assert kr.skillhub_key() == VAULT_PRIV
    monkeypatch.setenv(kr.SKILLHUB_ENV, "explicit-skillhub-key")
    assert kr.skillhub_key() == "explicit-skillhub-key"


def test_every_consumer_reads_the_effective_key_live(monkeypatch):
    monkeypatch.setenv(kr.REGULAR_ENV, ENV_REGULAR)
    monkeypatch.setenv(kr.PRIVILEGED_ENV, ENV_PRIV)
    transport, hub, gateway = OmniRouteTransport(), SkillHubClient(), ToolGateway()
    assert transport.current_key() == ENV_REGULAR and hub._key == ENV_PRIV
    assert gateway._omni_key == ENV_PRIV
    kr.apply_vault_values({kr.REGULAR: VAULT_REGULAR, kr.PRIVILEGED: VAULT_PRIV})  # no restart
    assert transport.current_key() == VAULT_REGULAR and hub._key == VAULT_PRIV
    assert gateway._omni_key == VAULT_PRIV and gateway.omniroute_configured
    assert SkillHubClient(key="explicit-key-xyz")._key == "explicit-key-xyz"
    assert OmniRouteTransport(api_key="explicit-key-xyz").current_key() == "explicit-key-xyz"


def test_gateway_opens_a_new_mcp_session_when_the_key_changes():
    kr.apply_vault_values({kr.PRIVILEGED: VAULT_PRIV})
    gateway = ToolGateway()
    first = gateway._client("omniroute")
    assert gateway._client("omniroute") is first
    kr.apply_vault_values({kr.PRIVILEGED: "another-privileged-key-77"})
    assert gateway._client("omniroute") is not first


# -- endpoints -----------------------------------------------------------------------


def _mock_omniroute(request: httpx.Request) -> httpx.Response:
    auth = request.headers.get("authorization", "")
    if auth not in (f"Bearer {VAULT_REGULAR}", f"Bearer {VAULT_PRIV}"):
        return httpx.Response(401, json={"error": "bad key echo-" + auth})
    if request.url.path == "/v1/models":
        return httpx.Response(
            200, json={"object": "list", "data": [{"id": "secure/coder"}, {"id": "auto/fast"}]}
        )
    if request.url.path == "/api/skills/catalog":
        return httpx.Response(
            200, json={"query": "", "count": 950, "results": [{"id": "s1"}]}
        )
    if request.url.path == "/api/mcp/stream":
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {}}, headers={"mcp-session-id": "s"}
        )
    return httpx.Response(404)


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        for model in (SecretRow, OmniRouteKeyAudit):
            await conn.run_sync(model.__table__.create)
    yield eng
    await eng.dispose()


@pytest.fixture
def api(engine, monkeypatch):
    monkeypatch.setenv("NERVE_OMNIROUTE_URL", "https://omni.test")
    monkeypatch.setattr("backend.api.wiring._bind_semantic_observer", lambda app: None)
    real = httpx.AsyncClient

    class Mocked(real):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(_mock_omniroute)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", Mocked)
    vault = FernetVault(generate_key())
    registry = RepositoryRegistry(make_session_factory(engine), secret_codec=vault)
    app = FastAPI()
    app.include_router(ep.router)
    app.state.omniroute_key_store = OmniRouteKeyStore(registry, vault)
    app.dependency_overrides[current_access] = lambda: {
        "sub": "owner@example.test",
        "typ": "access",
    }
    app.state.test_vault = vault
    with TestClient(app) as c:
        yield c, app, engine


def test_status_shows_set_state_only(api, monkeypatch):
    c, _, _ = api
    monkeypatch.setenv(kr.REGULAR_ENV, ENV_REGULAR)
    body = c.get("/api/settings/omniroute").json()
    assert body["keys"]["regular"] == {
        "configured": True,
        "source": "env",
        "vault_set": False,
        "changed_at": None,
    }
    assert body["keys"]["privileged"]["configured"] is False
    assert ENV_REGULAR not in json.dumps(body)


def test_set_key_is_encrypted_never_echoed_and_applies_live(api, monkeypatch):
    c, app, engine = api
    monkeypatch.setenv(kr.REGULAR_ENV, ENV_REGULAR)
    with structlog.testing.capture_logs() as logs:
        resp = c.put("/api/settings/omniroute/keys/regular", json={"value": VAULT_REGULAR})
    assert resp.status_code == 200
    body = resp.json()
    assert body["keys"]["regular"]["source"] == "vault" and body["keys"]["regular"]["changed_at"]
    assert VAULT_REGULAR not in resp.text and VAULT_REGULAR not in json.dumps(logs, default=str)
    assert kr.regular_key() == VAULT_REGULAR  # live, no restart
    assert VAULT_REGULAR not in c.get("/api/settings/omniroute").text
    # Stored encrypted: the raw row holds no plaintext.
    import asyncio

    async def rows():
        async with make_session_factory(engine)() as s:
            from sqlalchemy import select

            return [r.payload for r in (await s.execute(select(SecretRow))).scalars()]

    payloads = asyncio.run(rows())
    assert payloads and VAULT_REGULAR not in json.dumps(payloads)


def test_clear_falls_back_to_env(api, monkeypatch):
    c, _, _ = api
    monkeypatch.setenv(kr.PRIVILEGED_ENV, ENV_PRIV)
    c.put("/api/settings/omniroute/keys/privileged", json={"value": VAULT_PRIV})
    assert kr.privileged_key() == VAULT_PRIV
    cleared = c.delete("/api/settings/omniroute/keys/privileged").json()
    assert cleared["keys"]["privileged"]["source"] == "env" and kr.privileged_key() == ENV_PRIV


def test_bad_values_are_rejected_without_echo(api):
    c, _, _ = api
    secretish = "short"
    for payload in ({"value": secretish}, {"value": "has space inside key"}, {"nope": 1}):
        resp = c.put("/api/settings/omniroute/keys/regular", json=payload)
        assert resp.status_code == 422
        assert secretish not in resp.text and "space inside" not in resp.text
    assert c.put("/api/settings/omniroute/keys/regular", content=b"not json").status_code == 422
    big = c.put("/api/settings/omniroute/keys/regular", json={"value": "k" * 5000})
    assert big.status_code == 413 and "kkkk" not in big.text
    assert (
        c.put("/api/settings/omniroute/keys/other", json={"value": VAULT_REGULAR}).status_code
        == 404
    )
    assert kr.regular_key() is None


def test_write_refused_without_an_encrypting_vault(api):
    c, app, _ = api
    app.state.omniroute_key_store = OmniRouteKeyStore(app.state.omniroute_key_store._registry, None)
    resp = c.put("/api/settings/omniroute/keys/regular", json={"value": VAULT_REGULAR})
    assert resp.status_code == 503 and VAULT_REGULAR not in resp.text
    assert c.get("/api/settings/omniroute").json()["vault_writable"] is False


def test_requires_a_signed_in_person(api):
    c, app, _ = api
    app.dependency_overrides[current_access] = lambda: {"sub": "device:phone", "typ": "device"}
    assert (
        c.put("/api/settings/omniroute/keys/regular", json={"value": VAULT_REGULAR}).status_code
        == 403
    )
    assert c.get("/api/settings/omniroute").status_code == 403
    app.dependency_overrides.clear()
    assert c.get("/api/settings/omniroute").status_code in (401, 500)  # no session: not served


def test_test_buttons_report_counts_and_status_only(api):
    c, _, _ = api
    c.put("/api/settings/omniroute/keys/regular", json={"value": VAULT_REGULAR})
    c.put("/api/settings/omniroute/keys/privileged", json={"value": VAULT_PRIV})
    regular = c.post("/api/settings/omniroute/keys/regular/test").json()
    assert regular["ok"] is True and regular["inference"] == {
        "ok": True,
        "status": "ok",
        "routes": 2,
    }
    priv = c.post("/api/settings/omniroute/keys/privileged/test").json()
    assert priv["ok"] is True
    assert priv["catalog"] == {"ok": True, "status": "ok", "skills": 950}
    assert priv["mcp"] == {"ok": True, "status": "ok"}
    for text in (json.dumps(regular), json.dumps(priv)):
        assert VAULT_REGULAR not in text and VAULT_PRIV not in text


def test_test_reports_a_rejected_key_without_the_response_body(api):
    c, _, _ = api
    c.put("/api/settings/omniroute/keys/privileged", json={"value": "rejected-key-000111"})
    out = c.post("/api/settings/omniroute/keys/privileged/test")
    body = out.json()
    assert body["ok"] is False and body["catalog"]["status"] == "unauthorized"
    assert body["mcp"]["status"] == "unauthorized"
    assert "echo-" not in out.text and "rejected-key-000111" not in out.text
    assert (
        c.post("/api/settings/omniroute/keys/regular/test").json()["inference"]["status"]
        == "not_set"
    )


def test_audit_records_who_which_key_and_outcome_never_the_value(api):
    c, _, _ = api
    c.put("/api/settings/omniroute/keys/regular", json={"value": VAULT_REGULAR})
    c.put("/api/settings/omniroute/keys/regular", json={"value": "x"})  # rejected
    c.post("/api/settings/omniroute/keys/regular/test")
    c.delete("/api/settings/omniroute/keys/regular")
    audit = c.get("/api/settings/omniroute").json()["audit"]
    seen = {(a["action"], a["outcome"]) for a in audit}
    assert {("set", "ok"), ("set", "rejected_422"), ("test", "ok"), ("clear", "ok")} <= seen
    assert all(
        a["actor"] == "owner@example.test" and a["key"] == "regular" and a["at"] for a in audit
    )
    assert VAULT_REGULAR not in json.dumps(audit)


async def test_refresh_loads_vault_values_for_other_replicas(engine):
    vault = FernetVault(generate_key())
    registry = RepositoryRegistry(make_session_factory(engine), secret_codec=vault)
    writer = OmniRouteKeyStore(registry, vault)
    await writer.set(kr.PRIVILEGED, VAULT_PRIV)
    assert kr.privileged_key() is None  # this process has not refreshed yet
    await OmniRouteKeyStore(registry, vault).refresh()
    assert kr.privileged_key() == VAULT_PRIV
    assert kr.status_for(kr.PRIVILEGED)["changed_at"] is not None
