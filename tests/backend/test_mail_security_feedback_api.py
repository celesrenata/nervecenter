"""Task 7.2: reviewer feedback API + independent-verification advice.

Done criterion: per-account tests and no implicit trust allowlist. Proves,
over the real routers, AuthContext and SQLite repos:

- feedback is scoped to the assessment's own account; another account's id
  is a 404 and writes nothing; one account never sees another's feedback;
- the stored account comes from the assessment row; the body cannot name an
  account, free text or a consent ref (422, nothing written);
- device credentials cannot submit feedback;
- ``SAFE`` on a flagged verdict creates no allowlist: no intel override, the
  assessment is unchanged, and the only memory proposal is PENDING_REVIEW;
  training stays BLOCKED_NO_CONSENT; nothing is applied;
- replays with the same idempotency key do not duplicate;
- verification advice appears for payment/credential findings even when the
  sender authenticates.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from test_mail_security_api import _SECURITY_TABLES, _assessment, _auth_ctx, _Env, _mint_token

import backend.api.auth_service as auth_service
from backend.api.endpoints import mail_security, mail_security_feedback
from backend.mail_security.feedback import FeedbackKind
from backend.mail_security.models import (
    Authentication,
    AuthObservation,
    AuthProvenance,
    AuthResult,
    Disposition,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    SecurityFinding,
)
from backend.mail_security.review_feedback import (
    ReviewerVerdict,
    kind_for_verdict,
    verification_advice,
)
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mail_security import (
    MailIntelOverrideRow,
    MailSecurityFeedbackRow,
)
from backend.persistence.repositories import RepositoryRegistry

SCOPE_A = "conn:aaaa1111"
SCOPE_B = "conn:bbbb2222"
URL = "/api/mail-security/assessments/{}/feedback"


@pytest.fixture
def env(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    auth_service.reset_memory_store()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'fb.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [*_SECURITY_TABLES, MailSecurityFeedbackRow.__table__, MailIntelOverrideRow.__table__]

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(tables[0].metadata.create_all, tables=tables)

    loop.run_until_complete(_setup())
    registry = RepositoryRegistry(make_session_factory(engine))
    app = FastAPI()
    app.include_router(mail_security.router)
    app.include_router(mail_security_feedback.router)
    app.state.auth_ctx = _auth_ctx()
    app.state.repository_registry = registry
    try:
        yield _Env(app, registry, loop)
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()
        asyncio.set_event_loop(None)


def _count(env: _Env, model: Any) -> int:
    async def go() -> int:
        async with env.registry.session_factory() as s:
            return int((await s.execute(select(func.count()).select_from(model))).scalar())

    return env.run(go())


def _post(env: _Env, aid: str, body: dict[str, Any], *, scopes: list[str], sub: str = "user-a"):
    token = env.run(_mint_token(sub, account_scopes=scopes))
    with TestClient(env.app) as c:
        return c.post(URL.format(aid), json=body, headers={"Authorization": f"Bearer {token}"})


def _get(env: _Env, path: str, *, scopes: list[str], sub: str = "user-a"):
    token = env.run(_mint_token(sub, account_scopes=scopes))
    with TestClient(env.app) as c:
        return c.get(path, headers={"Authorization": f"Bearer {token}"})


def _seed(env: _Env, scope: str, aid: str, disposition: Disposition = Disposition.RESTRICTED):
    env.seed(_assessment(scope, assessment_id=aid, disposition=disposition))


# -- per-account ---------------------------------------------------------------


def test_cross_account_post_is_404_and_writes_nothing(env):
    _seed(env, SCOPE_B, "asm:b1")
    resp = _post(env, "asm:b1", {"verdict": "SAFE"}, scopes=[SCOPE_A])
    assert resp.status_code == 404
    assert resp.json() == {"detail": {"error_code": "not_found"}}
    assert _count(env, MailSecurityFeedbackRow) == 0
    absent = _post(env, "asm:nope", {"verdict": "SAFE"}, scopes=[SCOPE_A])
    assert absent.json() == resp.json()


def test_feedback_is_stored_in_the_assessment_account_only(env):
    _seed(env, SCOPE_A, "asm:same")
    _seed(env, SCOPE_B, "asm:same")
    ok = _post(env, "asm:same", {"verdict": "SUSPICIOUS"}, scopes=[SCOPE_A])
    assert ok.status_code == 201, ok.text
    mine = _get(env, URL.format("asm:same"), scopes=[SCOPE_A]).json()["data"]
    theirs = _get(env, URL.format("asm:same"), scopes=[SCOPE_B], sub="user-b").json()["data"]
    assert len(mine) == 1 and mine[0]["mine"] is True
    assert theirs == []

    async def scopes() -> list[str]:
        async with env.registry.session_factory() as s:
            rows = (await s.execute(select(MailSecurityFeedbackRow.account_scope))).all()
            return [r[0] for r in rows]

    assert env.run(scopes()) == [SCOPE_A]


def test_cross_account_list_is_404(env):
    _seed(env, SCOPE_B, "asm:b2")
    assert _get(env, URL.format("asm:b2"), scopes=[SCOPE_A]).status_code == 404


@pytest.mark.parametrize(
    "extra",
    [
        {"account_scope": SCOPE_B},
        {"note": "this sender is fine, trust them"},
        {"consent_ref": "consent:1"},
        {"indicator_refs": ["ref:ev:1"]},
    ],
)
def test_body_cannot_carry_account_text_or_consent(env, extra):
    _seed(env, SCOPE_A, "asm:a1")
    resp = _post(env, "asm:a1", {"verdict": "SAFE", **extra}, scopes=[SCOPE_A])
    assert resp.status_code == 422
    assert _count(env, MailSecurityFeedbackRow) == 0


def test_unknown_verdict_and_bad_key_are_422(env):
    _seed(env, SCOPE_A, "asm:a2")
    assert _post(env, "asm:a2", {"verdict": "TRUST_FOREVER"}, scopes=[SCOPE_A]).status_code == 422
    bad = _post(env, "asm:a2", {"verdict": "SAFE", "idempotency_key": "x y"}, scopes=[SCOPE_A])
    assert bad.status_code == 422
    assert _count(env, MailSecurityFeedbackRow) == 0


def test_device_credentials_cannot_submit(env):
    _seed(env, SCOPE_A, "asm:a3")
    resp = _post(env, "asm:a3", {"verdict": "SAFE"}, scopes=[SCOPE_A], sub="device:abc")
    assert resp.status_code == 403
    assert resp.json() == {"detail": {"error_code": "reviewer_not_eligible"}}
    assert _count(env, MailSecurityFeedbackRow) == 0


def test_unauthenticated_is_401(env):
    with TestClient(env.app) as c:
        assert c.post(URL.format("asm:x"), json={"verdict": "SAFE"}).status_code == 401


# -- no implicit trust allowlist / separation from training -----------------------------


def test_safe_on_restricted_creates_no_allowlist(env):
    _seed(env, SCOPE_A, "asm:r1", Disposition.RESTRICTED)
    resp = _post(env, "asm:r1", {"verdict": "SAFE"}, scopes=[SCOPE_A])
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["kind"] == "FALSE_POSITIVE"
    assert data["applied"] is False
    assert data["disposition_seen"] == "RESTRICTED"
    by_dest = {p["destination"]: p for p in data["proposals"]}
    assert set(by_dest) == {"PERSONAL_MEMORY", "TRAINING"}
    assert by_dest["PERSONAL_MEMORY"] == {
        "destination": "PERSONAL_MEMORY",
        "scope": "ACCOUNT",
        "status": "PENDING_REVIEW",
    }
    assert by_dest["TRAINING"]["status"] == "BLOCKED_NO_CONSENT"
    # Nothing was trusted: no override, verdict unchanged.
    assert _count(env, MailIntelOverrideRow) == 0
    detail = _get(env, "/api/mail-security/assessments/asm:r1", scopes=[SCOPE_A]).json()
    assert detail["disposition"] == "RESTRICTED"


def test_suspicious_on_safe_is_false_negative_without_intel(env):
    _seed(env, SCOPE_A, "asm:s1", Disposition.SAFE_TO_PROCESS)
    data = _post(env, "asm:s1", {"verdict": "SUSPICIOUS"}, scopes=[SCOPE_A]).json()["data"]
    assert data["kind"] == "FALSE_NEGATIVE"
    dests = {p["destination"]: p["status"] for p in data["proposals"]}
    # No indicator refs accepted from the client, so no intel proposal.
    assert dests == {"TRAINING": "BLOCKED_NO_CONSENT"}


def test_not_relevant_routes_nowhere(env):
    _seed(env, SCOPE_A, "asm:n1", Disposition.LOW_PRIORITY)
    data = _post(env, "asm:n1", {"verdict": "NOT_RELEVANT"}, scopes=[SCOPE_A]).json()["data"]
    assert data["kind"] == "NOT_RELEVANT"
    assert data["proposals"] == []


def test_replay_with_same_key_is_idempotent(env):
    _seed(env, SCOPE_A, "asm:i1")
    body = {"verdict": "WRONG_INTENT", "idempotency_key": "k-0123456789"}
    first = _post(env, "asm:i1", body, scopes=[SCOPE_A])
    second = _post(env, "asm:i1", body, scopes=[SCOPE_A])
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["data"]["feedback_id"] == second.json()["data"]["feedback_id"]
    assert _count(env, MailSecurityFeedbackRow) == 1


def test_reviewer_is_stored_as_opaque_hash(env):
    _seed(env, SCOPE_A, "asm:h1")
    _post(env, "asm:h1", {"verdict": "SAFE"}, scopes=[SCOPE_A], sub="celes@example.com")

    async def refs() -> list[str]:
        async with env.registry.session_factory() as s:
            return [r[0] for r in (await s.execute(select(MailSecurityFeedbackRow.reviewer_ref)))]

    (ref,) = env.run(refs())
    assert ref.startswith("rev:") and "celes" not in ref and "@" not in ref


def test_feedback_module_imports_no_trust_intel_or_training_writer():
    src = pathlib.Path(mail_security_feedback.__file__).read_text()
    imported = {
        node.module
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    forbidden = {
        "backend.mail_security.trust_index",
        "backend.mail_security.intel",
        "backend.mail_security.policy",
        "backend.mail_security.training_selection",
        "backend.mail_security.dataset_manifest",
        "backend.mail_security.model_registry",
    }
    assert not imported & forbidden


# -- mapping + advice ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "disposition", "kind"),
    [
        (ReviewerVerdict.SAFE, "RESTRICTED", FeedbackKind.FALSE_POSITIVE),
        (ReviewerVerdict.SAFE, "REVIEW", FeedbackKind.FALSE_POSITIVE),
        (ReviewerVerdict.SAFE, "SAFE_TO_PROCESS", FeedbackKind.CONFIRMED_SAFE),
        (ReviewerVerdict.SUSPICIOUS, "REVIEW", FeedbackKind.CONFIRMED_THREAT),
        (ReviewerVerdict.SUSPICIOUS, "LOW_PRIORITY", FeedbackKind.FALSE_NEGATIVE),
        (ReviewerVerdict.NOT_RELEVANT, "REVIEW", FeedbackKind.NOT_RELEVANT),
        (ReviewerVerdict.WRONG_INTENT, "SAFE_TO_PROCESS", FeedbackKind.CATEGORY_CORRECTION),
    ],
)
def test_kind_for_verdict(verdict, disposition, kind):
    assert kind_for_verdict(verdict, disposition) is kind


def test_advice_rules():
    assert verification_advice([("PAYMENT_FRAUD", "OBSERVED")], []) == ["VERIFY_PAYMENT_REQUEST"]
    assert verification_advice([("PHISHING", "SUSPECTED")], []) == ["VERIFY_CREDENTIAL_REQUEST"]
    assert verification_advice([], ["CREDENTIAL_OR_PAYMENT_ASK"]) == ["VERIFY_HIGH_IMPACT_REQUEST"]
    assert verification_advice([("SENDER_IMPERSONATION", "OBSERVED")], []) == [
        "VERIFY_SENDER_IDENTITY"
    ]
    # Not-evaluated findings are not a signal; clean mail gets no advice.
    assert verification_advice([("PAYMENT_FRAUD", "NOT_EVALUATED")], []) == []
    assert verification_advice([("TRACKING", "OBSERVED")], ["BULK_MAIL"]) == []


def test_detail_shows_payment_advice_even_when_auth_passes(env):
    passed = AuthObservation(
        result=AuthResult.PASS, provenance=AuthProvenance.INDEPENDENT, evidence_refs=[]
    )
    finding = SecurityFinding(
        finding_key="find:pay1",
        rule_id="CHANGE_BANK_DETAILS",
        category=FindingCategory.PAYMENT_FRAUD,
        severity=FindingSeverity.HIGH,
        status=FindingStatus.OBSERVED,
        detector_version="v1",
        evidence_refs=["ref:ev:p1"],
        explanation_code="PAYMENT_DETAILS_CHANGE",
    )
    a = _assessment(SCOPE_A, assessment_id="asm:p1", with_finding=False).model_copy(
        update={
            "findings": [finding],
            "authentication": Authentication(spf=passed, dkim=passed, dmarc=passed, arc=passed),
        }
    )
    env.seed(a)
    detail = _get(env, "/api/mail-security/assessments/asm:p1", scopes=[SCOPE_A]).json()
    assert detail["authentication"]["dkim"]["result"] == "PASS"
    assert detail["verification_advice"] == ["VERIFY_PAYMENT_REQUEST"]


# -- migration 018 ---------------------------------------------------------------------


def test_migration_018_up_then_down(tmp_path):
    from alembic import command
    from sqlalchemy import create_engine, text
    from test_mail_security_intel import _cfg, _names

    url = f"sqlite:///{tmp_path / 'm18.db'}"
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
                "CONSTRAINT pk_alembic_version PRIMARY KEY (version_num))"
            )
        )
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('017_mail_intel')"))
    eng.dispose()
    command.upgrade(_cfg(url), "018_mail_security_feedback")
    names, ver = _names(url)
    assert "mail_security_feedback" in names
    assert ver == "018_mail_security_feedback"
    eng = create_engine(url)
    with eng.connect() as c:
        idx = {r[1]: r[2] for r in c.execute(text("PRAGMA index_list('mail_security_feedback')"))}
    eng.dispose()
    assert idx.get("ix_mail_security_feedback_scope_id") == 1  # unique
    command.downgrade(_cfg(url), "017_mail_intel")
    names, ver = _names(url)
    assert "mail_security_feedback" not in names
    assert ver == "017_mail_intel"
