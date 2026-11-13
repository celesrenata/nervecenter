"""Task 7.3: security review queue (impact, budget, quiet hours, commitments).

Done criterion: quiet-hour and important-human-message tests. Pure-rule tests
first, then the durable loader + read-only API over SQLite.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from test_mail_security_api import _SECURITY_TABLES, _assessment, _auth_ctx, _Env, _mint_token

import backend.api.auth_service as auth_service
from backend.api.endpoints import mail_security
from backend.attention.policy import get_attention_policy, reset_attention_policy
from backend.mail_security.models import (
    Disposition,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    SecurityFinding,
)
from backend.mail_security.review_queue import (
    CommitmentRef,
    Delivery,
    ImpactTier,
    QueueCandidate,
    QueueReason,
    QueueSettings,
    build_queue,
)
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mail_security import (
    MailCampaignBandRow,
    MailCampaignMemberRow,
    MailExpectednessFeedbackRow,
    MailSecurityFeedbackRow,
    MailSecurityHeldPromotionRow,
    MailSemanticObservationRow,
    MailThreadLedgerRow,
)
from backend.persistence.models.personal import (
    AttentionItemRow,
    CommitmentRow,
    CommunicationRow,
    FollowupRow,
    PersonRow,
)
from backend.persistence.repositories import RepositoryRegistry

NOW = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
TODAY = NOW - timedelta(hours=2)
YESTERDAY = NOW - timedelta(days=1)
PAY = ("VERIFY_PAYMENT_REQUEST",)
HIGH = (("PAYMENT_FRAUD", "HIGH", "OBSERVED"),)


def _c(aid: str, disposition: str = "REVIEW", **kw) -> QueueCandidate:
    kw.setdefault("created_at", TODAY)
    return QueueCandidate(account_scope="conn:a", assessment_id=aid, disposition=disposition, **kw)


def _critical(aid: str, **kw) -> QueueCandidate:
    # RESTRICTED 3 + payment ask 2 + high finding 1 = 6
    return _c(aid, "RESTRICTED", verification_advice=PAY, findings=HIGH, **kw)


def _by_id(result):
    return {i.candidate.assessment_id: i for i in result.items}


# -- entry + scoring -------------------------------------------------------------


def test_only_unreviewed_flagged_verdicts_enter():
    r = build_queue(
        [
            _c("safe", "SAFE_TO_PROCESS"),
            _c("low", "LOW_PRIORITY"),
            _c("rev", "REVIEW"),
            _c("done", "RESTRICTED", reviewed=True),
        ],
        now=NOW,
    )
    assert set(_by_id(r)) == {"rev"}
    assert r.excluded_reviewed == 1


def test_scoring_tiers():
    assert _by_id(build_queue([_critical("x")], now=NOW))["x"].tier is ImpactTier.CRITICAL
    assert _by_id(build_queue([_c("r", "RESTRICTED", findings=HIGH)], now=NOW))["r"].tier is (
        ImpactTier.HIGH
    )
    assert _by_id(build_queue([_c("v")], now=NOW))["v"].tier is ImpactTier.MEDIUM
    assert _by_id(build_queue([_c("i", "INCOMPLETE")], now=NOW))["i"].tier is ImpactTier.LOW


def test_not_evaluated_findings_do_not_raise_impact():
    r = build_queue(
        [_c("n", "RESTRICTED", findings=(("MALWARE", "CRITICAL", "NOT_EVALUATED"),))], now=NOW
    )
    assert QueueReason.HIGH_SEVERITY_FINDING not in _by_id(r)["n"].reasons


def test_order_is_impact_then_newest_and_deterministic():
    cands = [_c("m1", created_at=TODAY), _critical("c1"), _c("m2", created_at=NOW)]
    a = build_queue(cands, now=NOW)
    b = build_queue(list(reversed(cands)), now=NOW)
    assert [i.candidate.assessment_id for i in a.items] == ["c1", "m2", "m1"]
    assert a == b


# -- interrupt budget + quiet hours -----------------------------------------------------


def test_one_novel_critical_interrupts_outside_quiet_hours():
    r = _by_id(build_queue([_critical("c1")], now=NOW))
    assert r["c1"].delivery is Delivery.INTERRUPT
    assert QueueReason.INTERRUPT_NOVEL_CRITICAL in r["c1"].reasons


def test_quiet_hours_defer_interrupts_but_keep_items_visible():
    quiet_end = NOW + timedelta(hours=6)
    r = _by_id(
        build_queue(
            [_critical("c1"), _c("m1")], now=NOW, settings=QueueSettings(quiet_until=quiet_end)
        )
    )
    assert r["c1"].delivery is Delivery.QUEUE
    assert QueueReason.INTERRUPT_DEFERRED_QUIET_PERIOD in r["c1"].reasons
    assert r["c1"].deliver_after == quiet_end
    assert r["m1"].delivery is Delivery.QUEUE  # nothing dropped during quiet hours
    assert not any(i.delivery is Delivery.INTERRUPT for i in r.values())


def test_expired_quiet_period_does_not_defer():
    r = _by_id(build_queue([_critical("c1")], now=NOW, settings=QueueSettings(quiet_until=NOW)))
    assert r["c1"].delivery is Delivery.INTERRUPT


def test_daily_interrupt_budget_earliest_first():
    cands = [
        _critical("late", created_at=NOW - timedelta(minutes=5)),
        _critical("early", created_at=NOW - timedelta(hours=3)),
        _critical("mid", created_at=NOW - timedelta(hours=1)),
    ]
    r = _by_id(build_queue(cands, now=NOW))
    assert r["early"].delivery is Delivery.INTERRUPT
    for aid in ("mid", "late"):
        assert r[aid].delivery is Delivery.QUEUE
        assert QueueReason.INTERRUPT_BUDGET_EXHAUSTED in r[aid].reasons
    r0 = _by_id(build_queue(cands, now=NOW, settings=QueueSettings(daily_interrupt_cap=0)))
    assert all(i.delivery is Delivery.QUEUE for i in r0.values())


def test_old_critical_never_interrupts():
    r = _by_id(build_queue([_critical("old", created_at=YESTERDAY)], now=NOW))
    assert r["old"].delivery is Delivery.QUEUE
    assert QueueReason.NOT_NOVEL in r["old"].reasons


# -- important human + commitments ----------------------------------------------------------


def test_low_impact_noise_is_digested_by_reason():
    cands = [_c(f"n{i}", "INCOMPLETE", reason_codes=("INSPECTION_INCOMPLETE",)) for i in range(5)]
    r = build_queue(cands, now=NOW)
    assert all(i.delivery is Delivery.DIGEST for i in r.items)
    assert r.digest.count == 5
    assert r.digest.by_reason == {"INSPECTION_INCOMPLETE": 5}


def test_important_human_message_is_never_digested_or_dropped():
    # Weakest possible verdict, buried in noise, with no visible room left.
    vip = _c("vip", "INCOMPLETE", important_sender=True, created_at=YESTERDAY)
    noise = [_c(f"n{i}", "INCOMPLETE") for i in range(20)] + [_c(f"m{i}") for i in range(20)]
    r = build_queue([vip, *noise], now=NOW, settings=QueueSettings(visible_cap=0))
    by = _by_id(r)
    assert by["vip"].delivery is Delivery.QUEUE
    assert QueueReason.IMPORTANT_HUMAN_SENDER in by["vip"].reasons
    # Everything unprotected went to the digest; the VIP message did not.
    assert r.digest.count == 40


def test_important_human_restricted_message_ranks_above_unknown():
    vip = _c("vip", "RESTRICTED", important_sender=True)
    unknown = _c("unk", "RESTRICTED")
    r = build_queue([unknown, vip], now=NOW)
    assert [i.candidate.assessment_id for i in r.items] == ["vip", "unk"]


def test_important_human_in_quiet_hours_is_queued_not_interrupting():
    vip = _critical("vip", important_sender=True)
    r = _by_id(
        build_queue([vip], now=NOW, settings=QueueSettings(quiet_until=NOW + timedelta(hours=1)))
    )
    assert r["vip"].delivery is Delivery.QUEUE
    assert r["vip"].deliver_after == NOW + timedelta(hours=1)


def test_commitments_behind_restricted_mail_are_preserved_as_review_candidates():
    refs = (CommitmentRef("commitment", "cm-1"), CommitmentRef("followup", "fu-1"))
    r = _by_id(build_queue([_c("r", "RESTRICTED", commitments=refs)], now=NOW))
    assert r["r"].candidate.commitments == refs
    assert all(ref.status == "REVIEW_CANDIDATE" for ref in r["r"].candidate.commitments)
    assert QueueReason.COMMITMENTS_PRESERVED in r["r"].reasons
    # Low impact + commitments: still protected from the digest.
    low = _by_id(build_queue([_c("i", "INCOMPLETE", commitments=refs[:1])], now=NOW))
    assert low["i"].delivery is Delivery.QUEUE


def test_visible_cap_overflows_only_unprotected_medium():
    cands = [_c(f"m{i}") for i in range(5)] + [
        _c("vip", important_sender=True),
        _c("r", "RESTRICTED", findings=HIGH),
    ]
    r = build_queue(cands, now=NOW, settings=QueueSettings(visible_cap=2))
    by = _by_id(r)
    assert by["r"].delivery is Delivery.QUEUE
    assert by["vip"].delivery is Delivery.QUEUE
    overflow = [i for i in r.items if QueueReason.VISIBLE_CAP_OVERFLOW in i.reasons]
    assert {i.tier for i in overflow} == {ImpactTier.MEDIUM}
    assert r.digest.count == len(overflow) == 5


def test_naive_clock_rejected():
    with pytest.raises(ValueError):
        build_queue([], now=datetime(2026, 1, 1))


# -- durable loader + API --------------------------------------------------------------------

CONN_A = "11111111-1111-4111-8111-111111111111"
CONN_B = "22222222-2222-4222-8222-222222222222"
SCOPE_A, SCOPE_B = f"conn:{CONN_A}", f"conn:{CONN_B}"


def _ref(conn: str, mail_id: str) -> tuple[str, str]:
    d = hashlib.sha256(f"{conn}:{mail_id}".encode()).hexdigest()[:24]
    return f"comm-mail-{d}", f"content://mail/{conn}/{d}"


@pytest.fixture
def env(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    auth_service.reset_memory_store()
    reset_attention_policy()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'q.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [
        *_SECURITY_TABLES,
        MailSecurityFeedbackRow.__table__,
        MailSecurityHeldPromotionRow.__table__,
        MailThreadLedgerRow.__table__,
        MailCampaignMemberRow.__table__,
        MailCampaignBandRow.__table__,
        MailSemanticObservationRow.__table__,
        MailExpectednessFeedbackRow.__table__,
        PersonRow.__table__,
        CommunicationRow.__table__,
        CommitmentRow.__table__,
        FollowupRow.__table__,
        AttentionItemRow.__table__,
    ]

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(tables[0].metadata.create_all, tables=tables)

    loop.run_until_complete(_setup())
    registry = RepositoryRegistry(make_session_factory(engine))
    app = FastAPI()
    app.include_router(mail_security.router)
    app.state.auth_ctx = _auth_ctx()
    app.state.repository_registry = registry
    try:
        yield _Env(app, registry, loop)
    finally:
        reset_attention_policy()
        loop.run_until_complete(engine.dispose())
        loop.close()
        asyncio.set_event_loop(None)


def _seed_msg(env, scope, conn, mail_id, aid, disposition, *, person, payment=False):
    cid, ref = _ids = _ref(conn, mail_id)
    env.run(
        env.registry.communications.create_communication(
            id=cid,
            person_id=person,
            channel="email",
            direction="inbound",
            content_ref=ref,
            subject="SUBJECT-CANARY-q",
        )
    )
    env.run(
        env.registry.mail_security.upsert_job(
            account_scope=scope,
            outbox_job_key=f"job:{aid}",
            source_key=f"src:{aid}",
            analysis_profile_version="2026.01",
            meta={"content_ref": ref},
        )
    )
    a = _assessment(
        scope,
        assessment_id=aid,
        source_ref=f"src:{aid}",
        disposition=disposition,
        with_finding=False,
    )
    if payment:
        a = a.model_copy(
            update={
                "findings": [
                    SecurityFinding(
                        finding_key="find:p",
                        rule_id="PAY",
                        category=FindingCategory.PAYMENT_FRAUD,
                        severity=FindingSeverity.HIGH,
                        status=FindingStatus.OBSERVED,
                        detector_version="v1",
                        evidence_refs=["ref:ev:p"],
                        explanation_code="PAY_CHANGE",
                    )
                ]
            }
        )
    env.seed(a)
    return cid


def _get(env, scopes):
    token = env.run(_mint_token(account_scopes=scopes))
    with TestClient(env.app) as c:
        return c.get(
            "/api/mail-security/review-queue", headers={"Authorization": f"Bearer {token}"}
        )


def test_api_queue_joins_vip_and_commitments_scoped(env):
    comms = env.registry.communications
    env.run(comms.create_person(id="p-vip", display_name="V", vip=True))
    env.run(comms.create_person(id="p-x", display_name="X"))
    vip_cid = _seed_msg(
        env, SCOPE_A, CONN_A, "m1", "asm:vip", Disposition.RESTRICTED, person="p-vip"
    )
    _seed_msg(env, SCOPE_A, CONN_A, "m2", "asm:unk", Disposition.REVIEW, person="p-x", payment=True)
    _seed_msg(env, SCOPE_A, CONN_A, "m3", "asm:safe", Disposition.SAFE_TO_PROCESS, person="p-x")
    _seed_msg(env, SCOPE_B, CONN_B, "m4", "asm:b", Disposition.RESTRICTED, person="p-x")
    env.run(
        env.registry.commitments.create_commitment(
            person_id="p-vip", text="COMMITMENT-TEXT-CANARY", source_communication_id=vip_cid
        )
    )
    env.run(
        env.registry.commitments.create_followup(
            text="FOLLOWUP-TEXT-CANARY", person_id="p-vip", linked_refs=[f"communication:{vip_cid}"]
        )
    )

    resp = _get(env, [SCOPE_A])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [i["assessment_id"] for i in body["data"]]
    assert set(ids) == {"asm:vip", "asm:unk"}  # no SAFE, no other account
    vip = next(i for i in body["data"] if i["assessment_id"] == "asm:vip")
    assert vip["important_sender"] is True
    assert vip["communication_id"] == vip_cid
    assert sorted(c["kind"] for c in vip["commitments"]) == ["commitment", "followup"]
    assert all(c["status"] == "REVIEW_CANDIDATE" for c in vip["commitments"])
    assert "COMMITMENTS_PRESERVED" in vip["reasons"]
    unk = next(i for i in body["data"] if i["assessment_id"] == "asm:unk")
    assert unk["verification_advice"] == ["VERIFY_PAYMENT_REQUEST"]
    for canary in ("SUBJECT-CANARY-q", "COMMITMENT-TEXT-CANARY", "FOLLOWUP-TEXT-CANARY"):
        assert canary not in resp.text


def test_api_quiet_period_defers_interrupt(env):
    env.run(env.registry.communications.create_person(id="p-x", display_name="X"))
    _seed_msg(
        env, SCOPE_A, CONN_A, "m9", "asm:crit", Disposition.RESTRICTED, person="p-x", payment=True
    )
    loud = _get(env, [SCOPE_A]).json()
    assert loud["data"][0]["delivery"] == "INTERRUPT"
    now = datetime.now(UTC)
    get_attention_policy().add_quiet_period(
        "sleep", now - timedelta(hours=1), now + timedelta(hours=2)
    )
    quiet = _get(env, [SCOPE_A]).json()
    item = quiet["data"][0]
    assert item["delivery"] == "QUEUE"
    assert "INTERRUPT_DEFERRED_QUIET_PERIOD" in item["reasons"]
    assert quiet["quiet_until"] is not None and item["deliver_after"] == quiet["quiet_until"]


def test_api_reviewed_items_leave_the_queue(env):
    env.run(env.registry.communications.create_person(id="p-x", display_name="X"))
    _seed_msg(env, SCOPE_A, CONN_A, "m5", "asm:r", Disposition.REVIEW, person="p-x")
    env.run(
        env.registry.mail_security.record_feedback(
            feedback_id="fb:1",
            account_scope=SCOPE_A,
            assessment_id="asm:r",
            verdict="SAFE",
            kind="FALSE_POSITIVE",
            source="ACCOUNT_OWNER",
            reviewer_ref="rev:x",
            disposition_seen="REVIEW",
            proposals=[],
        )
    )
    body = _get(env, [SCOPE_A]).json()
    assert body["data"] == [] and body["excluded_reviewed"] == 1


def test_api_no_scopes_is_404(env):
    assert _get(env, []).status_code == 404
