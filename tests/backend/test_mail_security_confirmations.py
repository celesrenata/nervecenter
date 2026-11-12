"""Task 7.6: explicit confirmation workflow.

Done criterion: malicious instruction cannot escalate to approval. Also:
sending, payments and external disclosure stay locked without explicit,
independently authenticated approval; approvals are action-specific,
expiring, single-use and audited.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from test_mail_security_api import _SECURITY_TABLES, _assessment, _auth_ctx, _Env, _mint_token

import backend.api.auth_service as auth_service
from backend.api import step_up as su
from backend.api.endpoints import mail_security, mail_security_confirmations
from backend.mail_security import confirmations as cf
from backend.mail_security.models import Disposition
from backend.mail_security.task_proposals import (
    BillFacts,
    CalendarFacts,
    CalendarTitle,
    FollowupReason,
    ProposalKind,
    RelationshipFacts,
    build_proposal,
    default_proposal_policies,
)
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mail_security import MailSecurityConfirmationRow
from backend.persistence.repositories import RepositoryRegistry
from backend.policy.pdp import PolicyDecisionPoint
from schemas.nc_platform import NCPrincipal

SCOPE = "conn:11111111-1111-4111-8111-111111111111"
OTHER = "conn:22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
HUMAN = NCPrincipal(
    id="row-h",
    principal_id="prn_" + "1" * 26,
    kind="human",
    display_name="me",
    namespace="personal",
)
AGENT = HUMAN.model_copy(update={"kind": "agent"})
FRESH = cf.StepUp(auth_time=NOW - timedelta(minutes=2), proof_id="oidc:jti-1")
STALE = cf.StepUp(auth_time=NOW - timedelta(minutes=30), proof_id="oidc:jti-2")


def _facts(kind: ProposalKind) -> Any:
    return {
        ProposalKind.CALENDAR_EVENT: CalendarFacts(
            title=CalendarTitle.MEETING, starts_at=NOW + timedelta(days=1), duration_minutes=30
        ),
        ProposalKind.BILL_REMINDER: BillFacts(
            due_date=date(2026, 12, 1), amount_minor=12000, currency="USD", payee_ref="m:util"
        ),
        ProposalKind.RELATIONSHIP_FOLLOWUP: RelationshipFacts(
            person_ref="person:dana",
            follow_up_on=date(2026, 11, 20),
            reason=FollowupReason.REPLY_PENDING,
        ),
    }[kind]


def _proposal(kind: ProposalKind = ProposalKind.BILL_REMINDER, scope: str = SCOPE, aid="asm:s"):
    return build_proposal(
        account_scope=scope,
        assessment_id=aid,
        disposition="SAFE_TO_PROCESS",
        kind=kind,
        facts=_facts(kind),
    )


HIGH = [
    (ProposalKind.BILL_REMINDER, "resource.pay:bills.payments"),
    (ProposalKind.RELATIONSHIP_FOLLOWUP, "resource.send:messages.outbound"),
    (ProposalKind.CALENDAR_EVENT, "resource.disclose:calendar.invites"),
]
LOW = [
    (ProposalKind.BILL_REMINDER, "resource.write:bills.reminders"),
    (ProposalKind.RELATIONSHIP_FOLLOWUP, "resource.write:relationships.followups"),
    (ProposalKind.CALENDAR_EVENT, "resource.write:calendar.events"),
]


def _req(kind, action, **kw):
    return cf.request_confirmation(
        _proposal(kind), action, principal=HUMAN, requested_by="rev:me", now=NOW, **kw
    )


def _pdp() -> PolicyDecisionPoint:
    return PolicyDecisionPoint(default_proposal_policies(), clock=lambda: NOW)


# -- locked without explicit approval -----------------------------------------------------


@pytest.mark.parametrize(("kind", "action"), HIGH + LOW)
def test_every_action_is_locked_without_confirmation(kind, action):
    d = cf.authorize_execution(
        _pdp(), HUMAN, _proposal(kind), action, confirmation=None, approval=None, now=NOW
    )
    assert not d.allowed and d.reason_code == "NOT_APPROVED"
    # The PDP itself also default-denies the execution action.
    p = _proposal(kind)
    assert _pdp().evaluate(HUMAN, action, p.resource_uri, {"origin": "EMAIL"}).effect == "DENY"


@pytest.mark.parametrize(("kind", "action"), HIGH)
def test_high_risk_needs_fresh_step_up_and_sender_verification(kind, action):
    c = _req(kind, action)
    assert c.risk is not cf.RiskClass.LOW
    for step_up, verified, code in [
        (None, True, cf.Refusal.STEP_UP_REQUIRED),
        (STALE, True, cf.Refusal.STEP_UP_REQUIRED),
        (FRESH, False, cf.Refusal.SENDER_VERIFICATION_REQUIRED),
    ]:
        with pytest.raises(cf.ConfirmationError) as e:
            cf.approve(c, principal=HUMAN, now=NOW, step_up=step_up, sender_verified=verified)
        assert e.value.code is code
    approved, approval = cf.approve(
        c, principal=HUMAN, now=NOW, step_up=FRESH, sender_verified=True
    )
    d = cf.authorize_execution(
        _pdp(), HUMAN, _proposal(kind), action, confirmation=approved, approval=approval, now=NOW
    )
    assert d.allowed and d.reason_code == "approval_granted"


@pytest.mark.parametrize(("kind", "action"), LOW)
def test_low_risk_needs_explicit_confirmation_but_no_step_up(kind, action):
    approved, approval = cf.approve(_req(kind, action), principal=HUMAN, now=NOW)
    assert cf.authorize_execution(
        _pdp(), HUMAN, _proposal(kind), action, confirmation=approved, approval=approval, now=NOW
    ).allowed


# -- malicious instruction cannot escalate ---------------------------------------------------


def test_low_risk_approval_cannot_be_used_to_pay():
    approved, approval = cf.approve(
        _req(ProposalKind.BILL_REMINDER, "resource.write:bills.reminders"), principal=HUMAN, now=NOW
    )
    d = cf.authorize_execution(
        _pdp(),
        HUMAN,
        _proposal(),
        "resource.pay:bills.payments",
        confirmation=approved,
        approval=approval,
        now=NOW,
    )
    assert not d.allowed
    # Even forcing the action check past the confirmation, the PDP binding
    # rejects the action mismatch.
    forged = approved.__class__(**{**approved.__dict__, "action": "resource.pay:bills.payments"})
    d2 = cf.authorize_execution(
        _pdp(),
        HUMAN,
        _proposal(),
        "resource.pay:bills.payments",
        confirmation=forged,
        approval=approval,
        now=NOW,
    )
    assert not d2.allowed and d2.reason_code == "approval_rejected"


def test_tampered_payload_amount_breaks_the_binding():
    c = _req(ProposalKind.BILL_REMINDER, "resource.pay:bills.payments")
    approved, approval = cf.approve(
        c, principal=HUMAN, now=NOW, step_up=FRESH, sender_verified=True
    )
    tampered = build_proposal(
        account_scope=SCOPE,
        assessment_id="asm:s",
        disposition="SAFE_TO_PROCESS",
        kind=ProposalKind.BILL_REMINDER,
        facts=BillFacts(
            due_date=date(2026, 12, 1), amount_minor=900000, currency="USD", payee_ref="m:util"
        ),
    )
    forged = approved.__class__(**{**approved.__dict__, "proposal_id": tampered.proposal_id})
    d = cf.authorize_execution(
        _pdp(),
        HUMAN,
        tampered,
        "resource.pay:bills.payments",
        confirmation=forged,
        approval=approval,
        now=NOW,
    )
    assert not d.allowed and d.reason_code == "approval_rejected"


def test_actions_outside_the_closed_set_are_refused():
    for action in (
        "resource.pay:*",
        "resource.write:access.grants",
        "resource.delete:mail",
        "resource.send:messages.outbound",
    ):
        with pytest.raises(cf.ConfirmationError) as e:
            _req(ProposalKind.BILL_REMINDER, action)
        assert e.value.code is cf.Refusal.ACTION_NOT_ALLOWED


def test_machines_cannot_request_approve_or_execute():
    with pytest.raises(cf.ConfirmationError):
        cf.request_confirmation(
            _proposal(),
            "resource.write:bills.reminders",
            principal=AGENT,
            requested_by="x",
            now=NOW,
        )
    c = _req(ProposalKind.BILL_REMINDER, "resource.write:bills.reminders")
    with pytest.raises(cf.ConfirmationError):
        cf.approve(c, principal=AGENT, now=NOW)
    approved, approval = cf.approve(c, principal=HUMAN, now=NOW)
    assert not cf.authorize_execution(
        _pdp(), AGENT, _proposal(), c.action, confirmation=approved, approval=approval, now=NOW
    ).allowed


def test_expiry_single_use_and_state_machine():
    c = _req(ProposalKind.BILL_REMINDER, "resource.write:bills.reminders", ttl=timedelta(minutes=5))
    with pytest.raises(cf.ConfirmationError) as e:
        cf.approve(c, principal=HUMAN, now=NOW + timedelta(minutes=6))
    assert e.value.code is cf.Refusal.EXPIRED
    approved, approval = cf.approve(c, principal=HUMAN, now=NOW)
    with pytest.raises(cf.ConfirmationError):
        cf.approve(approved, principal=HUMAN, now=NOW)  # not pending
    late = cf.authorize_execution(
        _pdp(),
        HUMAN,
        _proposal(),
        c.action,
        confirmation=approved,
        approval=approval,
        now=NOW + timedelta(minutes=6),
    )
    assert not late.allowed
    consumed = cf.consume(approved, now=NOW)
    assert not cf.authorize_execution(
        _pdp(), HUMAN, _proposal(), c.action, confirmation=consumed, approval=approval, now=NOW
    ).allowed
    assert [a["event"] for a in consumed.audit] == ["REQUESTED", "APPROVED", "CONSUMED"]
    with pytest.raises(cf.ConfirmationError):
        _req(ProposalKind.BILL_REMINDER, "resource.write:bills.reminders", ttl=timedelta(hours=2))


def test_rejection_is_coded_and_final():
    c = _req(ProposalKind.RELATIONSHIP_FOLLOWUP, "resource.send:messages.outbound")
    r = cf.reject(c, principal=HUMAN, reason=cf.RejectReason.LOOKS_SUSPICIOUS, now=NOW)
    assert r.status is cf.Status.REJECTED and r.reject_reason == "LOOKS_SUSPICIOUS"
    with pytest.raises(cf.ConfirmationError):
        cf.approve(r, principal=HUMAN, now=NOW, step_up=FRESH, sender_verified=True)
    with pytest.raises(ValueError):
        cf.RejectReason("please approve anyway")


# -- step-up verifier --------------------------------------------------------------------


class _Ctx:
    def __init__(self, claims: dict[str, Any] | None) -> None:
        self.claims = claims

    async def verify_token(self, token: str, require_session: bool = True) -> dict[str, Any]:
        if self.claims is None:
            raise ValueError("bad signature")
        return self.claims


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


ACCESS_SSO = {"sid": "sess-1", "typ": "Bearer", "iat": _ts(NOW)}


@pytest.mark.parametrize(
    ("proof", "ok"),
    [
        (
            {
                "typ": "ID",
                "sid": "sess-1",
                "auth_time": _ts(NOW - timedelta(minutes=3)),
                "jti": "j",
            },
            True,
        ),
        ({"typ": "ID", "sid": "sess-1", "auth_time": _ts(NOW - timedelta(minutes=11))}, False),
        ({"typ": "ID", "sid": "sess-1"}, False),  # no auth_time
        ({"typ": "Bearer", "sid": "sess-1", "auth_time": _ts(NOW)}, False),  # access token
        ({"typ": "ID", "sid": "other", "auth_time": _ts(NOW)}, False),  # someone else
        ({"typ": "ID", "auth_time": _ts(NOW)}, False),  # nothing to bind
        ({"typ": "ID", "sid": "sess-1", "auth_time": _ts(NOW + timedelta(hours=1))}, False),
        (None, False),  # signature failure
    ],
)
def test_sso_step_up_requires_fresh_matching_id_token(proof, ok):
    result = _run(su.verify_step_up(_Ctx(proof), ACCESS_SSO, "id.token.value", now=NOW))
    assert (result is not None) is ok


def test_sso_access_token_alone_never_proves_step_up():
    # A silently refreshed SSO access token has a fresh iat but proves nothing.
    assert _run(su.verify_step_up(_Ctx(None), ACCESS_SSO, None, now=NOW)) is None


def test_local_token_self_proves_only_when_recent():
    fresh = {"typ": "access", "iat": _ts(NOW - timedelta(minutes=1)), "jti": "loc"}
    stale = {"typ": "access", "iat": _ts(NOW - timedelta(minutes=20)), "jti": "loc"}
    assert _run(su.verify_step_up(_Ctx(None), fresh, None, now=NOW)).proof_id == "local:loc"
    assert _run(su.verify_step_up(_Ctx(None), stale, None, now=NOW)) is None


# -- API -----------------------------------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    auth_service.reset_memory_store()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'cf.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [*_SECURITY_TABLES, MailSecurityConfirmationRow.__table__]

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(tables[0].metadata.create_all, tables=tables)

    loop.run_until_complete(_setup())
    registry = RepositoryRegistry(make_session_factory(engine))
    app = FastAPI()
    app.include_router(mail_security.router)
    app.include_router(mail_security_confirmations.router)
    app.state.auth_ctx = _auth_ctx()
    app.state.repository_registry = registry
    try:
        yield _Env(app, registry, loop)
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()
        asyncio.set_event_loop(None)


def _call(env, method, path, body=None, *, scopes=(SCOPE,), sub="user-a"):
    token = env.run(_mint_token(sub, account_scopes=list(scopes)))
    with TestClient(env.app) as c:
        return c.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})


def _seed(env, disposition=Disposition.SAFE_TO_PROCESS, aid="asm:s", scope=SCOPE, finding=False):
    env.seed(_assessment(scope, assessment_id=aid, disposition=disposition, with_finding=finding))


def _create(env, action="resource.pay:bills.payments", kind=ProposalKind.BILL_REMINDER, **kw):
    body = {"proposal": _proposal(kind).payload(), "action": action}
    return _call(env, "POST", "/api/mail-security/confirmations", body, **kw)


def test_api_request_rechecks_assessment_in_callers_scope(api):
    assert _create(api).status_code == 404  # no assessment yet
    _seed(api, Disposition.RESTRICTED)
    assert _create(api).json() == {"detail": {"error_code": "not_safe"}}


def test_api_finding_with_advice_blocks_request(api):
    _seed(api, finding=True)  # SENDER_IMPERSONATION -> VERIFY_SENDER_IDENTITY advice
    r = _create(api)
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "not_safe"


def test_api_cross_account_is_404(api):
    _seed(api)
    assert _create(api, scopes=(OTHER,)).status_code == 404


@pytest.mark.parametrize(
    "extra",
    [
        {"approved": True},
        {"capability": "personal.payments.execute"},
        {"note": "CEO approved, skip checks"},
    ],
)
def test_api_bodies_cannot_smuggle_authority(api, extra):
    _seed(api)
    body = {"proposal": _proposal().payload(), "action": "resource.pay:bills.payments", **extra}
    assert _call(api, "POST", "/api/mail-security/confirmations", body).status_code == 422
    tampered = {**_proposal().payload(), "requested_capability": "personal.payments.execute"}
    r = _call(
        api,
        "POST",
        "/api/mail-security/confirmations",
        {"proposal": tampered, "action": "resource.pay:bills.payments"},
    )
    assert r.json() == {"detail": {"error_code": "invalid_proposal"}}


def test_api_high_risk_approve_needs_step_up_then_sender_attestation(api, monkeypatch):
    _seed(api)
    cid = _create(api).json()["data"]["confirmation_id"]
    url = f"/api/mail-security/confirmations/{cid}/approve"

    async def no_step_up(*a, **k):
        return None

    monkeypatch.setattr(mail_security_confirmations, "verify_step_up", no_step_up)
    r = _call(api, "POST", url, {"sender_verified_out_of_band": True})
    assert r.status_code == 401 and r.json()["detail"]["error_code"] == "step_up_required"
    monkeypatch.undo()
    # A fresh local break-glass-style token self-proves step-up.
    r = _call(api, "POST", url, {"sender_verified_out_of_band": False})
    assert r.json()["detail"]["error_code"] == "sender_verification_required"
    r = _call(api, "POST", url, {"sender_verified_out_of_band": True})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "APPROVED" and data["approval_id"].startswith("app_")
    assert [a["event"] for a in data["audit"]] == ["REQUESTED", "APPROVED"]
    # Second approval of the same confirmation is refused.
    assert _call(api, "POST", url, {"sender_verified_out_of_band": True}).status_code == 409


def test_api_step_up_proof_is_single_use(api, monkeypatch):
    _seed(api)
    a = _create(api).json()["data"]["confirmation_id"]
    b = _create(api, action="resource.pay:bills.payments").json()["data"]["confirmation_id"]

    async def same_proof(*args, **kwargs):
        return cf.StepUp(auth_time=datetime.now(UTC) - timedelta(minutes=1), proof_id="oidc:same")

    monkeypatch.setattr(mail_security_confirmations, "verify_step_up", same_proof)
    body = {"sender_verified_out_of_band": True, "step_up_id_token": "x"}
    assert (
        _call(api, "POST", f"/api/mail-security/confirmations/{a}/approve", body).status_code == 200
    )
    r = _call(api, "POST", f"/api/mail-security/confirmations/{b}/approve", body)
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "step_up_reused"


def test_api_device_tokens_and_unknown_fields_refused(api):
    _seed(api)
    assert _create(api, sub="device:abc").status_code == 403
    cid = _create(api, action="resource.write:bills.reminders").json()["data"]["confirmation_id"]
    r = _call(
        api,
        "POST",
        f"/api/mail-security/confirmations/{cid}/approve",
        {"sender_verified_out_of_band": True, "force": True},
    )
    assert r.status_code == 422


def test_api_reject_and_list_are_scoped(api):
    _seed(api)
    cid = _create(api, action="resource.write:bills.reminders").json()["data"]["confirmation_id"]
    assert (
        _call(
            api,
            "POST",
            f"/api/mail-security/confirmations/{cid}/reject",
            {"reason": "LOOKS_SUSPICIOUS"},
            scopes=(OTHER,),
        ).status_code
        == 404
    )
    r = _call(
        api,
        "POST",
        f"/api/mail-security/confirmations/{cid}/reject",
        {"reason": "LOOKS_SUSPICIOUS"},
    )
    assert r.json()["data"]["status"] == "REJECTED"
    assert len(_call(api, "GET", "/api/mail-security/confirmations").json()["data"]) == 1
    assert _call(api, "GET", "/api/mail-security/confirmations", scopes=(OTHER,)).json() == {
        "data": []
    }


def test_migration_020_id_and_round_trip(tmp_path):
    from alembic import command
    from sqlalchemy import create_engine, text
    from test_mail_security_intel import _cfg, _names

    assert len("020_mail_confirmations") <= 32
    url = f"sqlite:///{tmp_path / 'm20.db'}"
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
                "CONSTRAINT pk_alembic_version PRIMARY KEY (version_num))"
            )
        )
        conn.execute(text("INSERT INTO alembic_version VALUES ('019_mail_held_promotions')"))
    eng.dispose()
    command.upgrade(_cfg(url), "020_mail_confirmations")
    assert "mail_security_confirmations" in _names(url)[0]
    command.downgrade(_cfg(url), "019_mail_held_promotions")
    assert "mail_security_confirmations" not in _names(url)[0]
