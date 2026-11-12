"""Task 7.1: ``GET /api/mail-security/assessments/for-communication/{id}``.

Proves, over the real router, AuthContext and SQLite repos:
- the newest assessment for the communication's own source is returned
  (not an arbitrary/oldest one, and never another source's);
- a communication in a scope the caller is not authorized for is a 404
  indistinguishable from an absent one (R-023.4, T-15);
- malformed ids, ref/id digest mismatches, unscheduled refs and refs with no
  assessment are all the same safe 404;
- the body is the redacted detail (no raw subject/content).
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from test_mail_security_api import (
    _SECURITY_TABLES,
    _assessment,
    _auth_ctx,
    _Env,
    _mint_token,
)

import backend.api.auth_service as auth_service
from backend.api.endpoints import mail_security
from backend.mail_security.models import Disposition
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import CommunicationRow, PersonRow
from backend.persistence.repositories import RepositoryRegistry

CONN_A = "11111111-1111-4111-8111-111111111111"
CONN_B = "22222222-2222-4222-8222-222222222222"
SCOPE_A = f"conn:{CONN_A}"
SCOPE_B = f"conn:{CONN_B}"
_SUBJECT_CANARY = "SUBJECT-CANARY-never-in-response-7c1d"


def _digest(conn: str, mail_id: str) -> str:
    return hashlib.sha256(f"{conn}:{mail_id}".encode()).hexdigest()[:24]


def _ids(conn: str, mail_id: str) -> tuple[str, str]:
    d = _digest(conn, mail_id)
    return f"comm-mail-{d}", f"content://mail/{conn}/{d}"


class _CommEnv(_Env):
    def comm(self, conn: str, mail_id: str, *, ref: str | None = None) -> str:
        comm_id, content_ref = _ids(conn, mail_id)
        self.run(
            self.registry.communications.create_communication(
                id=comm_id,
                person_id="person-1",
                channel="email",
                direction="inbound",
                content_ref=ref or content_ref,
                subject=_SUBJECT_CANARY,
            )
        )
        return comm_id

    def job(self, scope: str, conn: str, mail_id: str, source_key: str) -> None:
        _, content_ref = _ids(conn, mail_id)
        self.run(
            self.registry.mail_security.upsert_job(
                account_scope=scope,
                outbox_job_key=f"job:{source_key}",
                source_key=source_key,
                analysis_profile_version="2026.01",
                meta={"content_ref": content_ref},
            )
        )


@pytest.fixture
def env(tmp_path):
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    auth_service.reset_memory_store()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'for_comm.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [*_SECURITY_TABLES, PersonRow.__table__, CommunicationRow.__table__]

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(tables[0].metadata.create_all, tables=tables)

    loop.run_until_complete(_setup())
    registry = RepositoryRegistry(make_session_factory(engine))
    loop.run_until_complete(
        registry.communications.create_person(id="person-1", display_name="P")
    )
    app = FastAPI()
    app.include_router(mail_security.router)
    app.state.auth_ctx = _auth_ctx()
    app.state.repository_registry = registry
    try:
        yield _CommEnv(app, registry, loop)
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()
        asyncio.set_event_loop(None)


def _get(env: _CommEnv, comm_id: str, scopes: list[str]) -> object:
    token = env.run(_mint_token(account_scopes=scopes))
    with TestClient(env.app) as client:
        return client.get(
            f"/api/mail-security/assessments/for-communication/{comm_id}",
            headers={"Authorization": f"Bearer {token}"},
        )


def _seed_full(env: _CommEnv, scope: str, conn: str, mail_id: str, key: str) -> str:
    comm_id = env.comm(conn, mail_id)
    env.job(scope, conn, mail_id, key)
    return comm_id


def test_returns_newest_assessment_for_own_source(env):
    comm_id = _seed_full(env, SCOPE_A, CONN_A, "gmail:aa01", "src:a1")
    env.comm(CONN_A, "gmail:aa02")
    env.job(SCOPE_A, CONN_A, "gmail:aa02", "src:a2")
    # Another source in the same scope, older: must never be returned.
    env.seed(
        _assessment(
            SCOPE_A,
            assessment_id="asm:other",
            source_ref="src:a2",
            disposition=Disposition.RESTRICTED,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
    )
    env.seed(
        _assessment(
            SCOPE_A,
            assessment_id="asm:old",
            source_ref="src:a1",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    # A rescan pins a higher source revision (row timestamps are server-set,
    # so the revision is what orders same-second rows deterministically).
    env.seed(
        _assessment(
            SCOPE_A,
            assessment_id="asm:new",
            source_ref="src:a1",
            disposition=Disposition.SAFE_TO_PROCESS,
            created_at=datetime(2026, 2, 1, tzinfo=UTC),
        ).model_copy(update={"source_revision": 2})
    )
    resp = _get(env, comm_id, [SCOPE_A])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assessment_id"] == "asm:new"
    assert body["disposition"] == "SAFE_TO_PROCESS"
    assert body["mode"] == "SHADOW"
    assert body["completeness"]["checks_not_run"] == ["INTEL_MATCH"]
    assert _SUBJECT_CANARY not in resp.text


def test_cross_account_is_indistinguishable_404(env):
    comm_b = _seed_full(env, SCOPE_B, CONN_B, "gmail:bb01", "src:b1")
    env.seed(_assessment(SCOPE_B, assessment_id="asm:b", source_ref="src:b1"))
    absent, _ = _ids(CONN_A, "gmail:never")
    cross = _get(env, comm_b, [SCOPE_A])
    missing = _get(env, absent, [SCOPE_A])
    assert cross.status_code == missing.status_code == 404
    assert cross.json() == missing.json() == {"detail": {"error_code": "not_found"}}
    # The owner still sees it.
    assert _get(env, comm_b, [SCOPE_B]).status_code == 200


def test_job_in_wrong_scope_is_not_used(env):
    # Ref belongs to CONN_A but a job for it exists only under SCOPE_B.
    comm_id = env.comm(CONN_A, "gmail:aa09")
    env.job(SCOPE_B, CONN_A, "gmail:aa09", "src:x")
    env.seed(_assessment(SCOPE_B, assessment_id="asm:x", source_ref="src:x"))
    assert _get(env, comm_id, [SCOPE_A, SCOPE_B]).status_code == 404


@pytest.mark.parametrize(
    "comm_id",
    ["comm-mail-XYZ", "comm-mail-" + "a" * 23, "../assessments", "comm-other-" + "a" * 24],
)
def test_malformed_id_is_404(env, comm_id):
    assert _get(env, comm_id, [SCOPE_A]).status_code == 404


def test_ref_digest_mismatch_is_404(env):
    _, other_ref = _ids(CONN_A, "gmail:different")
    comm_id = env.comm(CONN_A, "gmail:aa03", ref=other_ref)
    env.job(SCOPE_A, CONN_A, "gmail:different", "src:d")
    env.seed(_assessment(SCOPE_A, assessment_id="asm:d", source_ref="src:d"))
    assert _get(env, comm_id, [SCOPE_A]).status_code == 404


def test_unscheduled_and_unassessed_are_404(env):
    unscheduled = env.comm(CONN_A, "gmail:aa04")
    unassessed = _seed_full(env, SCOPE_A, CONN_A, "gmail:aa05", "src:a5")
    assert _get(env, unscheduled, [SCOPE_A]).status_code == 404
    assert _get(env, unassessed, [SCOPE_A]).status_code == 404


def test_no_scopes_is_404(env):
    comm_id = _seed_full(env, SCOPE_A, CONN_A, "gmail:aa06", "src:a6")
    env.seed(_assessment(SCOPE_A, assessment_id="asm:a6", source_ref="src:a6"))
    assert _get(env, comm_id, []).status_code == 404


def test_unauthenticated_is_401(env):
    comm_id, _ = _ids(CONN_A, "gmail:aa07")
    with TestClient(env.app) as client:
        resp = client.get(
            f"/api/mail-security/assessments/for-communication/{comm_id}"
        )
    assert resp.status_code == 401
