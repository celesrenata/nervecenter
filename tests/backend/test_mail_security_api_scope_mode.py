"""Scope-mode tests for the NerveShield assessments API (NS-ADR-025).

Covers ``NERVE_MAIL_SECURITY_API_SCOPE_MODE`` (``claim_only`` default,
opt-in ``single_tenant``) against the real router, a real ``AuthContext`` and
a real account-scoped repository over SQLite. Invariants: R-017, R-020, R-023
(incl. R-023.4), I-01, T-14, T-15, T-26.
"""
from __future__ import annotations

import asyncio
import pathlib
import uuid
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from test_mail_security_api import (
    _CANARY,
    _SECURITY_TABLES,
    _assessment,
    _auth_ctx,
)

import backend.api.auth_service as auth_service
from backend.api.endpoints import mail_security
from backend.mail_security.identifiers import assessment_id as derive_assessment_id
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import ConnectionRow
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.connections_repo import ConnectionsRepository

MODE_ENV = "NERVE_MAIL_SECURITY_API_SCOPE_MODE"
LIST = "/api/mail-security/assessments"
HEALTH = "/api/mail-security/health"


class _Env:
    def __init__(self, app: FastAPI, registry: RepositoryRegistry, loop: Any) -> None:
        self.app, self.registry, self._loop = app, registry, loop
        self.scopes: dict[str, str] = {}

    def run(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    def add_connection(self, name: str, kind: str, enabled: bool, label: str = "L") -> str:
        row = self.run(
            self.registry.connections.create_connection(
                kind=kind, label=label, enabled=enabled
            )
        )
        scope = f"conn:{row.id}"
        self.scopes[name] = scope
        self.seed(scope, name)
        return scope

    def seed(self, scope: str, name: str) -> str:
        aid = derive_assessment_id(scope, f"msg:{name}", 1, "2026.01")
        self.run(
            self.registry.mail_security.upsert_assessment(
                _assessment(scope, assessment_id=aid)
            )
        )
        self.scopes[f"aid:{name}"] = aid
        return aid

    def token(
        self,
        sub: str = "user-a",
        *,
        account_scopes: Any = "absent",
        roles: list[str] | None = None,
        typ: str = "access",
    ) -> str:
        async def _go() -> str:
            ctx = _auth_ctx()
            extra: dict[str, Any] = {}
            if account_scopes != "absent":
                extra["account_scopes"] = account_scopes
            tok, exp, claims = ctx._mint(
                sub=sub,
                typ=typ,
                roles=roles if roles is not None else ["admin"],
                ttl=auth_service.ACCESS_TOKEN_TTL,
                extra=extra,
            )
            await auth_service.record_session(
                ctx, jti=claims["jti"], sub=sub, typ=typ, expires_at=exp, source="test"
            )
            return tok

        return self.run(_go())

    def get(self, path: str, token: str) -> Any:
        with TestClient(self.app) as client:
            return client.get(path, headers={"Authorization": f"Bearer {token}"})

    def list_ids(self, token: str, path: str = LIST) -> list[str]:
        resp = self.get(path, token)
        assert resp.status_code == 200
        return sorted(r["assessment_id"] for r in resp.json()["data"])


@pytest.fixture
def env(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.delenv(MODE_ENV, raising=False)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    auth_service.reset_memory_store()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'scope_mode.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    async def _create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(
                ConnectionRow.__table__.metadata.create_all,
                tables=[*_SECURITY_TABLES, ConnectionRow.__table__],
            )

    loop.run_until_complete(_create())
    registry = RepositoryRegistry(make_session_factory(engine))
    app = FastAPI()
    app.include_router(mail_security.router)
    app.state.auth_ctx = _auth_ctx()
    app.state.repository_registry = registry
    e = _Env(app, registry, loop)
    # enabled gmail / enabled imap / disabled gmail / enabled calendar
    e.add_connection("gmail", "gmail", True, label=_CANARY)
    e.add_connection("imap", "imap", True)
    e.add_connection("off", "gmail", False)
    e.add_connection("cal", "calendar", True)
    e.add_connection("imsg", "imessage", True)
    try:
        yield e
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()
        asyncio.set_event_loop(None)


def _enabled_ids(e: _Env) -> list[str]:
    return sorted([e.scopes["aid:gmail"], e.scopes["aid:imap"]])


# ---- mode parsing: anything but exactly single_tenant is claim_only --------


@pytest.mark.parametrize(
    "value",
    [None, "", "  ", "claim_only", "garbage", "single-tenant", "SINGLE_TENANTX",
     "1", "true", "single_tenant\n;x"],
)
def test_default_and_bad_modes_are_claim_only(env, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv(MODE_ENV, value)
    assert mail_security.scope_mode() == "claim_only"
    assert env.list_ids(env.token()) == []  # no claim -> nothing, as before
    assert env.get(HEALTH, env.token()).status_code == 404
    aid = env.scopes["aid:gmail"]
    assert env.get(f"{LIST}/{aid}", env.token()).status_code == 404


def test_claim_only_with_claim_unchanged_even_for_unresolved_scope(env, monkeypatch):
    monkeypatch.setenv(MODE_ENV, "claim_only")
    ids = env.list_ids(env.token(account_scopes=[env.scopes["off"]]))
    assert ids == [env.scopes["aid:off"]]  # explicit claim honoured exactly as today


def test_single_tenant_value_is_case_and_space_tolerant(env, monkeypatch):
    monkeypatch.setenv(MODE_ENV, " Single_Tenant ")
    assert mail_security.scope_mode() == "single_tenant"


# ---- single_tenant ----------------------------------------------------------


@pytest.fixture
def st(env, monkeypatch):
    monkeypatch.setenv(MODE_ENV, "single_tenant")
    return env


def test_single_tenant_human_sees_enabled_mail_only(st):
    tok = st.token()
    assert st.list_ids(tok) == _enabled_ids(st)
    # disabled mail, calendar and imessage assessments are invisible
    for name in ("off", "cal", "imsg"):
        assert st.get(f"{LIST}/{st.scopes['aid:' + name]}", tok).status_code == 404
    for name in ("gmail", "imap"):
        assert st.get(f"{LIST}/{st.scopes['aid:' + name]}", tok).status_code == 200


def test_disable_takes_effect_immediately(st):
    tok = st.token()
    assert len(st.list_ids(tok)) == 2
    cid = st.scopes["gmail"].removeprefix("conn:")
    st.run(st.registry.connections.update_connection(cid, enabled=False))
    assert st.list_ids(tok) == [st.scopes["aid:imap"]]


def test_foreign_and_absent_ids_are_identical_404(st):
    tok = st.token()
    foreign = st.get(f"{LIST}/{st.scopes['aid:off']}", tok)
    absent = st.get(f"{LIST}/{'f' * 40}", tok)
    assert foreign.status_code == absent.status_code == 404
    assert foreign.json() == absent.json() == {"detail": {"error_code": "not_found"}}


def test_claim_narrows_never_widens(st):
    sub = st.token(account_scopes=[st.scopes["gmail"]])
    assert st.list_ids(sub) == [st.scopes["aid:gmail"]]
    # claim naming a scope the server does not resolve grants nothing
    for name in ("off", "cal"):
        assert st.list_ids(st.token(account_scopes=[st.scopes[name]])) == []
    assert st.list_ids(st.token(account_scopes=["src:anything"])) == []
    assert st.list_ids(st.token(account_scopes=[])) == []
    assert st.list_ids(st.token(account_scopes="conn:*")) == []
    assert st.list_ids(st.token(account_scopes=None)) == []
    # claim of resolved + unresolved -> only the resolved one
    mixed = st.token(account_scopes=[st.scopes["gmail"], st.scopes["off"]])
    assert st.list_ids(mixed) == [st.scopes["aid:gmail"]]


def test_src_scopes_not_server_resolved(st):
    st.seed("src:api-ingest", "srcrow")
    assert st.scopes["aid:srcrow"] not in st.list_ids(st.token())
    assert st.get(f"{LIST}/{st.scopes['aid:srcrow']}", st.token()).status_code == 404


def test_account_id_filter_cannot_widen(st):
    tok = st.token()
    assert st.list_ids(tok, f"{LIST}?account_id={st.scopes['gmail']}") == [st.scopes["aid:gmail"]]
    assert st.list_ids(tok, f"{LIST}?account_id={st.scopes['off']}") == []
    assert st.list_ids(tok, f"{LIST}?account_id=src:x") == []
    narrow = st.token(account_scopes=[st.scopes["gmail"]])
    assert st.list_ids(narrow, f"{LIST}?account_id={st.scopes['imap']}") == []


def test_device_like_principals_get_claim_only(st):
    cases = [
        st.token(sub="device:abc"),
        st.token(sub="user-a", roles=["device"]),
        st.token(sub=""),
        st.token(sub="   "),
    ]
    for tok in cases:
        assert st.list_ids(tok) == []
        assert st.get(HEALTH, tok).status_code == 404
        assert st.get(f"{LIST}/{st.scopes['aid:gmail']}", tok).status_code == 404
    # explicit claim still works for them exactly as in claim_only
    dev = st.token(sub="device:abc", account_scopes=[st.scopes["gmail"]])
    assert st.list_ids(dev) == [st.scopes["aid:gmail"]]


def test_device_typ_token_rejected_at_auth(st):
    assert st.get(LIST, st.token(sub="device:abc", typ="device")).status_code == 401


def test_unauthenticated_still_rejected(st):
    with TestClient(st.app) as client:
        assert client.get(LIST).status_code == 401
        assert client.get(HEALTH).status_code == 401


def test_lookup_failure_fails_closed_with_safe_log(st, monkeypatch):
    async def boom(self, kinds):
        raise RuntimeError(f"db down {_CANARY}")

    monkeypatch.setattr(ConnectionsRepository, "list_enabled_ids_for_kinds", boom)
    tok = st.token()
    with structlog.testing.capture_logs() as logs:
        assert st.list_ids(tok) == []
        assert st.get(HEALTH, tok).status_code == 404
        resp = st.get(f"{LIST}/{st.scopes['aid:gmail']}", tok)
    assert resp.status_code == 404 and _CANARY not in resp.text
    assert _CANARY not in repr(logs)
    assert any(r.get("code") == "lookup_error" for r in logs)


def test_absent_plane_fails_closed(st):
    st.app.state.repository_registry = None
    assert st.list_ids(st.token()) == []
    assert st.get(HEALTH, st.token()).status_code == 404


def test_health_modes(st, monkeypatch):
    assert st.get(HEALTH, st.token()).status_code == 200
    assert st.get(HEALTH, st.token(account_scopes=[st.scopes["off"]])).status_code == 404
    monkeypatch.setenv(MODE_ENV, "claim_only")
    assert st.get(HEALTH, st.token()).status_code == 404
    assert st.get(HEALTH, st.token(account_scopes=[st.scopes["gmail"]])).status_code == 200


def test_routes_get_only():
    methods: set[str] = set()
    for route in mail_security.router.routes:
        methods |= set(getattr(route, "methods", set()) or set())
    assert methods <= {"GET", "HEAD", "OPTIONS"}


def test_scope_query_is_narrow_and_no_canary(st):
    tok = st.token()
    with structlog.testing.capture_logs() as logs:
        responses = [
            st.get(LIST, tok),
            st.get(f"{LIST}/{st.scopes['aid:gmail']}", tok),
            st.get(HEALTH, tok),
            st.get(f"{LIST}/nope", tok),
        ]
    blob = " ".join(r.text for r in responses) + repr(logs)
    assert _CANARY not in blob
    for name in ("gmail", "imap"):
        assert st.scopes[name] not in repr(logs)
    # ids only: the repo projection returns plain strings, nothing else
    ids = st.run(
        st.registry.connections.list_enabled_ids_for_kinds({"gmail", "imap"})
    )
    assert sorted(f"conn:{i}" for i in ids) == sorted([st.scopes["gmail"], st.scopes["imap"]])
    assert all(isinstance(i, str) for i in ids)
    uuid.UUID(ids[0])


def test_requests_cause_no_writes(st):
    async def _snapshot() -> tuple[int, int]:
        n = 0
        for k in ("gmail", "imap", "off", "cal", "imsg"):
            n += len(await st.registry.mail_security.list_assessments(st.scopes[k]))
        return n, len(await st.registry.connections.list_connections())

    before = st.run(_snapshot())
    tok = st.token()
    st.get(LIST, tok)
    st.get(HEALTH, tok)
    st.get(f"{LIST}/{st.scopes['aid:gmail']}", tok)
    assert st.run(_snapshot()) == before
