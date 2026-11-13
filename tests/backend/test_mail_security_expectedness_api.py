"""Expectedness feedback API + its effect (NS-ADR-078).

Over the real routers, AuthContext and SQLite repos:
- scoped to the caller's accounts: another account's assessment is a 404 and
  writes nothing; another account's feedback cannot be undone;
- only messages with an UNEXPECTED high-impact ask accept it (409 otherwise);
- the body cannot carry anything but an idempotency key; device credentials
  are refused; replays do not duplicate;
- effect: the message leaves the review queue; future scoring shifts toward
  EXPECTED but feedback never counts as history; undo restores both exactly;
- expired feedback stops counting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from test_mail_security_api import _SECURITY_TABLES, _assessment, _auth_ctx, _Env, _mint_token

import backend.api.auth_service as auth_service
from backend.api.endpoints import mail_security, mail_security_expectedness
from backend.mail_security.expectedness import Status, score
from backend.mail_security.models import Disposition
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mail_security import (
    MailCampaignBandRow,
    MailCampaignMemberRow,
    MailExpectednessCountRow,
    MailExpectednessFeedbackRow,
    MailSecurityFeedbackRow,
    MailSecurityHeldPromotionRow,
    MailSemanticObservationRow,
    MailThreadLedgerRow,
)
from backend.persistence.models.personal import CommitmentRow, FollowupRow
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.mail_security_expectedness_repo import (
    MailExpectednessRepository,
)
from backend.persistence.repositories.mail_security_review_repo import load_queue_candidates

SCOPE_A = "conn:aaaa1111"
SCOPE_B = "conn:bbbb2222"
SENDER = "s" * 24
UNUSUAL = [
    {
        "request_type": "PAY",
        "status": "UNEXPECTED",
        "high_impact": True,
        "history": 40,
        "probability": 0.01,
    },
    {
        "request_type": "OPEN_LINK",
        "status": "EXPECTED",
        "high_impact": False,
        "history": 40,
        "probability": 0.9,
    },
]


@pytest.fixture
def env(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    auth_service.reset_memory_store()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ex.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [
        *_SECURITY_TABLES,
        MailSecurityFeedbackRow.__table__,
        MailSecurityHeldPromotionRow.__table__,
        MailSemanticObservationRow.__table__,
        MailExpectednessCountRow.__table__,
        MailExpectednessFeedbackRow.__table__,
        MailThreadLedgerRow.__table__,
        MailCampaignMemberRow.__table__,
        MailCampaignBandRow.__table__,
        CommitmentRow.__table__,
        FollowupRow.__table__,
    ]
    tables = list({t.name: t for t in tables}.values())

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(tables[0].metadata.create_all, tables=tables)

    loop.run_until_complete(_setup())
    registry = RepositoryRegistry(make_session_factory(engine))
    app = FastAPI()
    app.include_router(mail_security.router)
    app.include_router(mail_security_expectedness.router)
    app.state.auth_ctx = _auth_ctx()
    app.state.repository_registry = registry
    try:
        yield _Env(app, registry, loop)
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()
        asyncio.set_event_loop(None)


def _req(env, method: str, path: str, *, scopes: list[str], body=None, sub="user-a"):
    token = env.run(_mint_token(sub, account_scopes=scopes))
    with TestClient(env.app) as c:
        return c.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})


def _seed(env, scope: str, aid: str, *, expectedness=UNUSUAL) -> str:
    env.seed(_assessment(scope, assessment_id=aid, disposition=Disposition.LOW_PRIORITY))

    async def go() -> str:
        async with env.registry.session_factory() as s:
            row = await env.registry.mail_security.get_assessment(scope, aid)
            s.add(
                MailSemanticObservationRow(
                    account_scope=scope,
                    source_key=row.source_id,
                    revision=row.revision,
                    status="COMPLETED",
                    observation={
                        "requests": [],
                        "expectedness": expectedness,
                        "expectedness_sender_key": SENDER,
                    },
                )
            )
            await s.commit()
            return row.source_id

    return env.run(go())


def _count(env, model) -> int:
    async def go() -> int:
        async with env.registry.session_factory() as s:
            return int((await s.execute(select(func.count()).select_from(model))).scalar())

    return env.run(go())


def _queued(env, scope: str) -> dict[str, bool]:
    cands = env.run(load_queue_candidates(env.registry.session_factory, scope))
    return {c.assessment_id: c.unexpected_ask for c in cands}


URL = "/api/mail-security/assessments/{}/expected"


def test_mark_expected_and_undo_round_trip(env):
    _seed(env, SCOPE_A, "as-1")
    assert _queued(env, SCOPE_A) == {"as-1": True}
    r = _req(env, "POST", URL.format("as-1"), scopes=[SCOPE_A], body={"idempotency_key": "key-00000001"})
    assert r.status_code == 201
    (fb,) = r.json()["data"]
    assert fb["request_type"] == "PAY" and fb["live"] and fb["mine"]
    assert _queued(env, SCOPE_A) == {}  # left the queue
    # Replay with the same key: no duplicate.
    r2 = _req(env, "POST", URL.format("as-1"), scopes=[SCOPE_A], body={"idempotency_key": "key-00000001"})
    assert r2.status_code == 200 and _count(env, MailExpectednessFeedbackRow) == 1
    # Undo restores the queue item; the row is kept for audit.
    u = _req(
        env,
        "DELETE",
        f"/api/mail-security/expectedness-feedback/{fb['feedback_id']}",
        scopes=[SCOPE_A],
    )
    assert u.status_code == 200 and u.json()["data"]["revoked"] and not u.json()["data"]["live"]
    assert _queued(env, SCOPE_A) == {"as-1": True}
    assert _count(env, MailExpectednessFeedbackRow) == 1
    listed = _req(env, "GET", "/api/mail-security/expectedness-feedback", scopes=[SCOPE_A])
    assert [x["revoked"] for x in listed.json()["data"]] == [True]


def test_cross_account_is_404_and_writes_nothing(env):
    _seed(env, SCOPE_A, "as-1")
    r = _req(env, "POST", URL.format("as-1"), scopes=[SCOPE_B], body={})
    assert r.status_code == 404 and _count(env, MailExpectednessFeedbackRow) == 0
    ok = _req(env, "POST", URL.format("as-1"), scopes=[SCOPE_A], body={})
    fid = ok.json()["data"][0]["feedback_id"]
    u = _req(env, "DELETE", f"/api/mail-security/expectedness-feedback/{fid}", scopes=[SCOPE_B])
    assert u.status_code == 404
    assert _queued(env, SCOPE_A) == {}  # still marked


def test_only_unusual_high_impact_asks_accept_feedback(env):
    _seed(env, SCOPE_A, "as-ok", expectedness=[UNUSUAL[1]])
    r = _req(env, "POST", URL.format("as-ok"), scopes=[SCOPE_A], body={})
    assert r.status_code == 409 and _count(env, MailExpectednessFeedbackRow) == 0


@pytest.mark.parametrize(
    "extra", [{"account_scope": SCOPE_B}, {"request_type": "PAY"}, {"note": "free text"}]
)
def test_body_carries_nothing_but_an_idempotency_key(env, extra):
    _seed(env, SCOPE_A, "as-1")
    r = _req(env, "POST", URL.format("as-1"), scopes=[SCOPE_A], body=extra)
    assert r.status_code == 422 and _count(env, MailExpectednessFeedbackRow) == 0


def test_device_credentials_cannot_mark(env):
    _seed(env, SCOPE_A, "as-1")
    r = _req(env, "POST", URL.format("as-1"), scopes=[SCOPE_A], body={}, sub="device:abc")
    assert r.status_code == 403 and _count(env, MailExpectednessFeedbackRow) == 0


def test_feedback_shifts_scoring_never_history_and_expires(env):
    repo = MailExpectednessRepository(env.registry.session_factory)
    now = datetime.now(UTC)
    counts = {"OPEN_LINK": 40.0}
    assert score("PAY", counts).status is Status.UNEXPECTED
    for i in range(6):
        env.run(
            repo.add_feedback(
                feedback_id=f"efb_{i}",
                account_scope=SCOPE_A,
                assessment_id=f"a{i}",
                source_key=f"s{i}",
                sender_key=SENDER,
                request_type="PAY",
                weight=2,
                expires_at=now + timedelta(days=180),
                reviewer_ref="r",
            )
        )
    live = env.run(repo.live_feedback_weights(SCOPE_A, SENDER, now=now))
    assert live == {"PAY": 12.0}
    shifted = score("PAY", counts, live)
    assert shifted.status is not Status.UNEXPECTED and shifted.history == 40
    # Feedback never lifts a sparse sender out of ABSTAIN.
    assert score("PAY", {"OPEN_LINK": 2.0}, live).status is Status.ABSTAIN

    async def expire() -> None:
        async with env.registry.session_factory() as s:
            await s.execute(
                update(MailExpectednessFeedbackRow).values(expires_at=now - timedelta(seconds=1))
            )
            await s.commit()

    env.run(expire())
    assert env.run(repo.live_feedback_weights(SCOPE_A, SENDER, now=now)) == {}
