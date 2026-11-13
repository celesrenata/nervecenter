"""SHADOW per-account content campaigns (NS-ADR-069, G8 item 6).

Pinned here:
- near-duplicate texts share a campaign, unrelated texts do not;
- signatures and band keys are keyed per account (no cross-account joins);
- short texts are not fingerprinted; expired members are pruned;
- a flagged message never flags itself, only its siblings;
- end to end: a LOW_PRIORITY sibling of a REVIEW message reaches the review
  queue as CAMPAIGN_OF_FLAGGED with its disposition unchanged;
- without the key the worker records nothing.
"""
from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta

import _mail_security_harness_helpers as H
import pytest
from sqlalchemy import select

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.mail_security import telemetry
from backend.mail_security.campaigns import (
    THRESHOLD,
    band_keys,
    set_campaign_repository,
    siblings_of_flagged,
    signature,
    similarity,
)
from backend.mail_security.review_queue import QueueReason, build_queue
from backend.mail_security.thread_ledger import ENV_LEDGER_KEY
from backend.persistence.models.mail_security import (
    MailCampaignMemberRow,
    MailSecurityAssessmentRow,
)
from backend.persistence.repositories.mail_security_campaign_repo import MailCampaignRepository
from backend.persistence.repositories.mail_security_review_repo import load_queue_candidates

SECRET = os.urandom(32)
NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)

PHISH = (
    "Dear customer your mailbox storage is almost full and incoming messages will be "
    "rejected within 24 hours unless you verify your account at the secure portal today "
    "thank you for choosing our service the security team {n}"
)
OTHER = (
    "Hi team the quarterly planning meeting moved to Thursday afternoon in the large "
    "conference room please bring the updated roadmap slides and the budget numbers"
)


def test_near_duplicates_match_and_unrelated_do_not() -> None:
    a = signature(SECRET, "conn:a", PHISH.format(n="ref 1001"))
    b = signature(SECRET, "conn:a", PHISH.format(n="ref 2002"))
    c = signature(SECRET, "conn:a", OTHER)
    assert similarity(a, b) >= THRESHOLD
    assert similarity(a, c) < THRESHOLD
    assert set(band_keys(SECRET, "conn:a", a)) & set(band_keys(SECRET, "conn:a", b))


def test_keys_are_per_account_and_short_texts_skipped() -> None:
    a = signature(SECRET, "conn:a", PHISH.format(n="x"))
    b = signature(SECRET, "conn:b", PHISH.format(n="x"))
    assert similarity(a, b) < THRESHOLD
    assert not set(band_keys(SECRET, "conn:a", a)) & set(band_keys(SECRET, "conn:b", b))
    assert signature(SECRET, "conn:a", "click here now") == ()


def test_flagged_message_never_flags_itself() -> None:
    members = [("s1", "c1"), ("s2", "c1"), ("s3", "c1"), ("s4", "c2")]
    assert siblings_of_flagged(members, {"s1"}) == {"s2": 1, "s3": 1}
    assert siblings_of_flagged(members, {"s1", "s2"}) == {"s1": 1, "s2": 1, "s3": 2}
    assert siblings_of_flagged(members, {"s4"}) == {}


@pytest.fixture
async def harness(tmp_path):
    from backend.persistence.models.mail_security import (
        MailSecurityFeedbackRow,
        MailSecurityHeldPromotionRow,
    )
    from backend.persistence.models.personal import CommitmentRow, FollowupRow

    h = await H.make_harness(tmp_path / "campaigns.sqlite")
    extra = [MailSecurityFeedbackRow.__table__, MailSecurityHeldPromotionRow.__table__,
             CommitmentRow.__table__, FollowupRow.__table__]
    async with h.engine.begin() as conn:
        await conn.run_sync(lambda sc: extra[0].metadata.create_all(sc, tables=extra))
    telemetry.reset_sweeper_heartbeat()
    yield h
    telemetry.reset_sweeper_heartbeat()
    await H.teardown_harness(h)


async def test_repo_joins_isolates_and_expires(harness) -> None:
    repo = MailCampaignRepository(harness.registry.session_factory)

    async def put(scope: str, src: str, text: str, now: datetime = NOW):
        sig = signature(SECRET, scope, text)
        return await repo.record(scope, source_key=src, content_ref=None, sig=sig,
                                 bands=band_keys(SECRET, scope, sig), now=now)

    c1, m1 = await put("conn:a", "s1", PHISH.format(n="1"))
    c2, m2 = await put("conn:a", "s2", PHISH.format(n="2"))
    c3, _ = await put("conn:a", "s3", OTHER)
    assert (m1, m2) == (0, 1) and c1 == c2 and c3 != c1
    assert await put("conn:a", "s2", PHISH.format(n="2")) is None  # replay
    cb, mb = await put("conn:b", "s9", PHISH.format(n="1"))  # other account
    assert mb == 0 and cb != c1
    assert sorted(await repo.live_members("conn:a", NOW)) == [("s1", c1), ("s2", c1)]
    # 31 days later the old members are pruned on the next write.
    await put("conn:a", "s4", OTHER, now=NOW + timedelta(days=31))
    async with harness.registry.session_factory() as s:
        left = {r.source_key for r in (await s.execute(select(MailCampaignMemberRow))).scalars()}
    assert left == {"s4"}


def _mime(n: str) -> bytes:
    return (
        "From: Help Desk <help@mailbox-support.example>\r\n"
        f"Subject: Storage full {n}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        + PHISH.format(n=n) + "\r\n"
    ).encode()


async def test_sibling_of_flagged_reaches_queue(harness, monkeypatch) -> None:
    monkeypatch.setenv(ENV_LEDGER_KEY, base64.b64encode(SECRET).decode())
    set_campaign_repository(MailCampaignRepository(harness.registry.session_factory))
    await H.ingest(harness, monkeypatch, H.mailitem("<c-1@x>", _mime("ref 1001")), uid=5)
    await H.ingest(harness, monkeypatch, H.mailitem("<c-2@x>", _mime("ref 2002")), uid=6)
    for _ in range(2):
        await H.security_worker(harness).process_one()
    async with harness.registry.session_factory() as s:
        members = list((await s.execute(select(MailCampaignMemberRow))).scalars())
        assert len(members) == 2 and len({m.campaign_id for m in members}) == 1
        rows = list((await s.execute(select(MailSecurityAssessmentRow))).scalars())
        # Pretend one sibling was assessed REVIEW (the other stays as assessed).
        flagged, other = rows[0], rows[1]
        flagged.disposition = "REVIEW"
        other.disposition = "LOW_PRIORITY"
        await s.commit()
    candidates = await load_queue_candidates(harness.registry.session_factory, harness.scope)
    by_source = {c.assessment_id: c for c in candidates}
    sib = by_source[other.assessment_id]
    assert sib.campaign_flagged == 1 and sib.disposition == "LOW_PRIORITY"
    assert by_source[flagged.assessment_id].campaign_flagged == 0  # never counts itself
    result = build_queue(candidates, now=datetime(2026, 10, 27, 12, tzinfo=UTC))
    reasons = {i.candidate.assessment_id: i.reasons for i in result.items}
    assert QueueReason.CAMPAIGN_OF_FLAGGED in reasons[other.assessment_id]
    assert QueueReason.CAMPAIGN_OF_FLAGGED not in reasons[flagged.assessment_id]


async def test_without_key_nothing_is_recorded(harness, monkeypatch) -> None:
    monkeypatch.delenv(ENV_LEDGER_KEY, raising=False)
    set_campaign_repository(MailCampaignRepository(harness.registry.session_factory))
    await H.ingest(harness, monkeypatch, H.mailitem("<c-3@x>", _mime("ref 3003")))
    await H.security_worker(harness).process_one()
    async with harness.registry.session_factory() as s:
        assert (await s.execute(select(MailCampaignMemberRow))).first() is None


def test_shadow_only_items_never_displace_review_items() -> None:
    """Live regression: 24 campaign siblings pushed 4 REVIEW items past the
    visible cap into the digest. Within a tier, disposition-queued items rank
    first; SHADOW-only items fill whatever visible room is left."""
    from backend.mail_security.review_queue import Delivery, QueueCandidate, QueueSettings

    def cand(i: int, disposition: str, campaign: int) -> QueueCandidate:
        return QueueCandidate(
            account_scope="conn:a", assessment_id=f"a{i:02d}", disposition=disposition,
            created_at=NOW - timedelta(days=2, minutes=i), campaign_flagged=campaign,
        )

    # Shadow-only items are NEWER than the REVIEW items (the live ordering).
    reviews = [cand(10 + i, "REVIEW", 0) for i in range(12)]
    shadows = [cand(i, "LOW_PRIORITY", 1) for i in range(10)]
    res = build_queue(reviews + shadows, now=NOW, settings=QueueSettings(visible_cap=10))
    visible = [i for i in res.items if i.delivery is not Delivery.DIGEST]
    assert len(visible) == 10
    assert all(i.candidate.disposition == "REVIEW" for i in visible)
