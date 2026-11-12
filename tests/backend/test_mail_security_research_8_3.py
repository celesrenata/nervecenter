"""Spike 8.3: personal expectedness — opt-in, uncertainty, fairness, reversible feedback."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.mail_security.research import expectedness as ex
from backend.mail_security.research.expectedness_eval import evaluate

NOW = datetime(2026, 11, 12, tzinfo=UTC)


def _model(**kw) -> ex.ExpectednessModel:
    m = ex.ExpectednessModel(salt=b"s" * 16, opted_in=True, **kw)
    for _ in range(20):
        m.observe("power-co.example", "PAY")
    for _ in range(20):
        m.observe("friend.example", "REPLY")
    return m


def test_not_opted_in_records_and_scores_nothing():
    m = ex.ExpectednessModel(salt=b"s" * 16)
    m.observe("a.example", "PAY")
    m.feedback_expected("f1", "a.example", "PAY", now=NOW)
    assert m.state() == ((), ())
    assert m.score("a.example", "PAY", now=NOW).status is ex.Status.NOT_OPTED_IN


GLOBAL = {
    "READ": 0.4,
    "REPLY": 0.3,
    "SCHEDULE": 0.1,
    "OPEN_LINK": 0.1,
    "PAY": 0.05,
    "DOWNLOAD": 0.03,
    "SUBMIT_CREDENTIALS": 0.01,
    "CHANGE_PAYMENT_DETAILS": 0.005,
    "SHARE_PERSONAL_DATA": 0.005,
}


def test_personalization_separates_normal_from_anomalous_asks():
    m = _model(prior=GLOBAL)
    assert m.score("power-co.example", "PAY", now=NOW).status is ex.Status.EXPECTED
    # 20 observations: the interval straddles the cutoff -> UNCERTAIN, not EXPECTED.
    assert m.score("friend.example", "PAY", now=NOW).status is ex.Status.UNCERTAIN
    for _ in range(60):
        m.observe("friend.example", "REPLY")
    friend_pay = m.score("friend.example", "PAY", now=NOW)
    assert friend_pay.status is ex.Status.UNEXPECTED and friend_pay.high_impact
    assert friend_pay.interval[1] < m.unexpected_below


def test_documented_limit_account_prior_is_dominated_by_heavy_senders():
    # Without a global prior, an account that is half bill payments learns
    # P(PAY) ~ 0.5 and a friend's payment ask is no longer UNEXPECTED.
    m = _model()
    assert m.score("friend.example", "PAY", now=NOW).status is not ex.Status.UNEXPECTED


def test_sparse_senders_abstain_instead_of_being_flagged():
    m = _model()
    for _ in range(2):
        m.observe("new-contact.example", "REPLY")
    e = m.score("new-contact.example", "SUBMIT_CREDENTIALS", now=NOW)
    assert e.status is ex.Status.ABSTAIN and e.history == 2 and e.interval is None


def test_storage_is_keyed_counts_only_and_forget_erases():
    m = _model()
    blob = repr(m.state())
    assert "power-co" not in blob and "friend" not in blob
    m.forget()
    assert m.state() == ((), ())


def test_feedback_is_bounded_exactly_reversible_and_expires():
    m = _model()
    before = m.state()
    p0 = m.score("friend.example", "PAY", now=NOW).probability
    m.feedback_expected("fb-1", "friend.example", "PAY", now=NOW)
    m.feedback_expected("fb-1", "friend.example", "PAY", now=NOW)  # replay: no double count
    p1 = m.score("friend.example", "PAY", now=NOW).probability
    assert p0 < p1 < 0.2  # bounded: one feedback cannot flip 20 observations
    assert m.undo("fb-1") and m.state() == before
    assert m.score("friend.example", "PAY", now=NOW).probability == p0
    m.feedback_expected("fb-2", "friend.example", "PAY", now=NOW)
    later = NOW + ex.FEEDBACK_TTL + timedelta(seconds=1)
    assert m.score("friend.example", "PAY", now=later).probability == p0  # expired weight ignored
    assert m.expire(later) == 1 and m.state() == before


def test_expected_never_means_safe_or_trusted():
    assert "SAFE" not in {s.value for s in ex.Status}
    m = _model()
    e = m.score("power-co.example", "PAY", now=NOW)
    assert (
        e.status is ex.Status.EXPECTED and e.high_impact
    )  # still flagged as high impact  # still flagged as high impact
    src = Path(ex.__file__).read_text()
    mods = {n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)}
    assert not {m for m in mods if m and ("policy" in m or "trust_index" in m)}


def test_synthetic_baseline_comparison_targets():
    r = evaluate(seed=11, accounts=20, senders=16)
    assert r["auc_all"]["personalized"] > r["auc_all"]["global_baseline"]
    assert (
        r["auc_high_impact_types_only"]["personalized"]
        > r["auc_high_impact_types_only"]["global_baseline"] + 0.05
    )
    assert r["abstain_rate"]["sparse_senders"] == 1.0
    assert r["false_unexpected_rate_on_normal"]["sparse_senders"] == 0.0
    assert r["false_unexpected_rate_on_normal"]["rich_senders"] <= 0.02
    # Documented limit stays visible: no recall on sparse senders.
    assert r["unexpected_recall_on_anomalous"]["sparse_senders"] == 0.0


def test_evaluation_is_deterministic():
    assert evaluate(seed=5, accounts=4, senders=6) == evaluate(seed=5, accounts=4, senders=6)
