"""Personal expectedness, SHADOW (NS-ADR-075, 8.3 follow-on).

Pinned here:
- sparse senders abstain; personalization separates a routine ask from an
  unusual high-impact one; uncertainty is reported honestly;
- the prior is the fixed population prior, never the account's own mix;
- a message is scored against history BEFORE it is counted (no self-vouching);
- sender keys are per-account HMACs; counts only; ``forget`` erases them;
- a separate opt-in; inert without the key;
- only an UNEXPECTED high-impact ask reaches the review queue, and the
  disposition never changes.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import _mail_security_harness_helpers as H
import pytest
from sqlalchemy import select
from test_mail_security_semantic_observer import (
    SCOPE,
    Chat,
    CountingRegistry,
    MemRepo,
    _mime,
    _snapshot_runtime,
)

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.mail_security import telemetry
from backend.mail_security.expectedness import (
    MIN_HISTORY,
    POPULATION_PRIOR,
    Status,
    score,
    sender_key,
    unexpected_high_impact,
)
from backend.mail_security.observer_wiring import (
    ENV_EXPECTEDNESS_SCOPES,
    ENV_OBSERVER_SCOPES,
    sender_domain,
)
from backend.mail_security.parser import parse_mime_evidence
from backend.mail_security.review_queue import QueueCandidate, QueueReason, build_queue
from backend.persistence.models.mail_security import MailExpectednessCountRow
from backend.persistence.repositories.mail_security_expectedness_repo import (
    MailExpectednessRepository,
)

SECRET = os.urandom(32)
NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)


def test_prior_is_fixed_population_and_sums_to_one() -> None:
    assert abs(sum(POPULATION_PRIOR.values()) - 1.0) < 1e-9


def test_sparse_senders_abstain() -> None:
    s = score("PAY", {"OPEN_LINK": MIN_HISTORY - 1})
    assert s.status is Status.ABSTAIN and s.high_impact


def test_routine_vs_unusual_high_impact_ask() -> None:
    newsletter = {"OPEN_LINK": 120.0, "READ": 40.0}
    assert score("PAY", newsletter).status is Status.UNEXPECTED
    assert score("OPEN_LINK", newsletter).status is Status.EXPECTED
    biller = {"PAY": 30.0, "READ": 10.0}
    assert score("PAY", biller).status is Status.EXPECTED  # routine for a biller
    # Honest uncertainty with modest history.
    assert score("PAY", {"OPEN_LINK": 8.0}).status is Status.UNCERTAIN


def test_sender_keys_are_per_account() -> None:
    assert sender_key(SECRET, "conn:a", "bank.example") != sender_key(
        SECRET, "conn:b", "bank.example"
    )
    assert sender_key(SECRET, "conn:a", "Bank.Example ") == sender_key(
        SECRET, "conn:a", "bank.example"
    )


def test_unexpected_high_impact_only() -> None:
    assert unexpected_high_impact([{"status": "UNEXPECTED", "high_impact": True}])
    assert not unexpected_high_impact([{"status": "UNEXPECTED", "high_impact": False}])
    assert not unexpected_high_impact([{"status": "EXPECTED", "high_impact": True}])


def test_queue_reason_never_changes_disposition() -> None:
    c = QueueCandidate(
        account_scope="conn:a",
        assessment_id="a1",
        disposition="LOW_PRIORITY",
        created_at=NOW,
        unexpected_ask=True,
    )
    (item,) = build_queue([c], now=NOW).items
    assert QueueReason.UNEXPECTED_HIGH_IMPACT_ASK in item.reasons
    assert item.candidate.disposition == "LOW_PRIORITY"


# -- runtime -----------------------------------------------------------------------------
class MemCounts:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict[str, float]] = {}
        self.order: list[str] = []

    async def counts(self, scope, key):
        self.order.append("read")
        return dict(self.data.get((scope, key), {}))

    async def add(self, scope, key, types):
        self.order.append("add")
        bucket = self.data.setdefault((scope, key), {})
        for t in types:
            bucket[t] = bucket.get(t, 0.0) + 1


def _rt(counts, *, secret=SECRET):
    rt = _snapshot_runtime(Chat(), MemRepo(), CountingRegistry())
    rt._expect_repo, rt._expect_secret = counts, secret
    return rt


async def test_scored_before_counted_and_separately_opted_in(monkeypatch) -> None:
    monkeypatch.setenv(ENV_OBSERVER_SCOPES, "*")
    monkeypatch.setenv(ENV_EXPECTEDNESS_SCOPES, SCOPE)
    ev = parse_mime_evidence(_mime())
    assert sender_domain(ev) == "vendor.example"
    counts = MemCounts()
    key = sender_key(SECRET, SCOPE, "vendor.example")
    counts.data[(SCOPE, key)] = {"OPEN_LINK": 60.0}
    rt = _rt(counts)
    assert (
        await rt.observe(SCOPE, source_key="s1", revision=1, evidence=ev, finding_codes=[])
        == "COMPLETED"
    )
    assert counts.order == ["read", "add"]  # no self-vouching
    (row,) = rt._repo.rows
    scores = row["observation"]["expectedness"]
    assert {"request_type": "PAY", "status": "UNEXPECTED", "high_impact": True}.items() <= scores[
        0
    ].items()
    assert counts.data[(SCOPE, key)]["PAY"] == 1


async def test_not_opted_in_or_no_key_records_no_expectedness(monkeypatch) -> None:
    monkeypatch.setenv(ENV_OBSERVER_SCOPES, "*")
    monkeypatch.setenv(ENV_EXPECTEDNESS_SCOPES, "")
    counts = MemCounts()
    rt = _rt(counts)
    await rt.observe(
        SCOPE, source_key="s1", revision=1, evidence=parse_mime_evidence(_mime()), finding_codes=[]
    )
    assert "expectedness" not in rt._repo.rows[0]["observation"] and counts.order == []
    monkeypatch.setenv(ENV_EXPECTEDNESS_SCOPES, "*")
    rt2 = _rt(counts, secret=None)
    await rt2.observe(
        SCOPE, source_key="s2", revision=1, evidence=parse_mime_evidence(_mime()), finding_codes=[]
    )
    assert "expectedness" not in rt2._repo.rows[0]["observation"] and counts.order == []


# -- durable repo --------------------------------------------------------------------------
@pytest.fixture
async def harness(tmp_path):
    h = await H.make_harness(tmp_path / "expect.sqlite")
    telemetry.reset_sweeper_heartbeat()
    yield h
    telemetry.reset_sweeper_heartbeat()
    await H.teardown_harness(h)


async def test_repo_counts_are_scoped_and_forgettable(harness) -> None:
    repo = MailExpectednessRepository(harness.registry.session_factory)
    await repo.add("conn:a", "k1", ["PAY", "READ"])
    await repo.add("conn:a", "k1", ["PAY"])
    await repo.add("conn:b", "k1", ["READ"])
    assert await repo.counts("conn:a", "k1") == {"PAY": 2.0, "READ": 1.0}
    assert await repo.counts("conn:b", "k1") == {"READ": 1.0}
    assert await repo.forget("conn:a") == 2
    assert await repo.counts("conn:a", "k1") == {}
    async with harness.registry.session_factory() as s:
        rows = (await s.execute(select(MailExpectednessCountRow))).scalars().all()
    assert [(r.account_scope, r.request_type, r.count) for r in rows] == [("conn:b", "READ", 1)]
