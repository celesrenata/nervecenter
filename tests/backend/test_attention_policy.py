"""AC-R15 (R108) — local, user-controlled attention policy tests.

Covers the policy layer added around the existing attention service
(spec §28/§29/§58/§59, AC-R15):

- **Budget (§58):** a per-day cap on FYI/low-priority items with bundling
  into the briefing (9 low-priority items → exactly one bundled entry);
  urgent items bypass batching; NOW items are never suppressed.
- **Quiet periods (§59):** sleep/meeting/focus/driving/manual-DND windows
  suppress notification flags only — the item stays in
  ``/api/attention/now`` with its status unchanged.
- **Deadline escalation (§28):** TODAY items whose deadline proximity
  crosses the threshold are promoted to NOW with a deterministic reason;
  score inputs and existing ``why`` are preserved.
- **Adaptive escalation (§29):** local per-person/per-channel
  preferences are applied to the notification mode (incl. silent).
- **Audit:** action history (create/snooze/resolve/… + escalation) and
  the audit response (notification state, escalation reason, adaptive
  decision, budget outcome) survive mutations; ``reset`` wipes it.
- **Briefing hook:** the bundled entry appears as a single briefing
  action; urgent items keep their own rows.

The attention plane is an in-process singleton (no DB, no NATS); the
policy layer is a second singleton reset per test, so every test gets a
clean, deterministic world. Clocks are pinned where time matters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.models import AttentionState
from backend.attention import (
    AttentionPolicyService,
    AttentionService,
    QuietPeriodKind,
    get_attention_policy,
    get_attention_service,
    reset_attention_policy,
    reset_attention_service,
)

#: Pinned reference clock for deterministic time-relative decisions.
PINNED_NOW = datetime(2026, 10, 20, 7, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_singletons():
    reset_attention_service()
    reset_attention_policy()
    yield
    reset_attention_service()
    reset_attention_policy()


def _svc() -> AttentionService:
    """The process singleton (fresh per test via the autouse fixture)."""
    return get_attention_service()


def _fyi_items(svc: AttentionService, n: int, urgency: float = 0.2, importance: float = 0.2):
    return [
        svc.create(
            {
                "reason": f"fyi {i}",
                "urgency": urgency,
                "importance": importance,
                "status": "FYI",
            }
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Budget (§58)
# ---------------------------------------------------------------------------


def test_budget_bundles_low_priority_items_into_one_briefing_entry():
    svc = _svc()
    _fyi_items(svc, 9)
    policy = svc.policy

    decision = policy.budget_decision(svc.list(), now=PINNED_NOW)
    assert len(decision.individual) == policy.budget().daily_fyi_cap
    assert len(decision.bundled) == 6
    assert decision.urgent == []
    assert decision.bundle is not None
    assert decision.bundle.count == 6
    assert len(decision.bundle.attention_ids) == 6
    assert "bundled" in decision.bundle.label
    assert decision.bundle.bundle_id == "fyi-bundle-2026-10-20"  # deterministic
    assert any("daily FYI cap" in r for r in decision.reasons)

    # The bundled items keep their status — nothing is deleted or suppressed.
    for item in svc.list():
        assert item.status is AttentionState.FYI


def test_budget_urgent_items_bypass_batching():
    svc = _svc()
    _fyi_items(svc, 9)
    urgent = svc.create(
        {"reason": "call the plumber", "urgency": 0.9, "importance": 0.4, "status": "FYI"}
    )
    decision = svc.policy.budget_decision(svc.list(), now=PINNED_NOW)

    assert decision.urgent == [urgent.attention_id]
    assert urgent.attention_id not in decision.bundled
    assert urgent.attention_id not in decision.individual
    # Urgent flags are never suppressed by the budget:
    flags = svc.policy.notification_flags(svc.list(), now=PINNED_NOW)
    assert flags[urgent.attention_id].notify is True
    assert any("bypasses budget batching" in r for r in flags[urgent.attention_id].reasons)


def test_budget_never_suppresses_now_items():
    svc = _svc()
    now_item = svc.create(
        {"reason": "hot NOW thing", "urgency": 0.1, "importance": 0.1, "status": "NOW"}
    )
    _fyi_items(svc, 10)
    flags = svc.policy.notification_flags(svc.list(), now=PINNED_NOW)

    assert flags[now_item.attention_id].notify is True
    assert any("NOW item" in r for r in flags[now_item.attention_id].reasons)
    # The over-cap FYI items are the ones flagged off (bundled):
    fyi_ids = [i.attention_id for i in svc.list(status=AttentionState.FYI)]
    assert sum(1 for aid in fyi_ids if not flags[aid].notify) == 7


def test_budget_cap_is_user_editable():
    svc = _svc()
    _fyi_items(svc, 5)
    policy = svc.policy
    assert len(policy.budget_decision(svc.list(), now=PINNED_NOW).bundled) == 2

    policy.set_budget(daily_fyi_cap=5)
    assert policy.budget().daily_fyi_cap == 5
    decision = policy.budget_decision(svc.list(), now=PINNED_NOW)
    assert decision.bundled == []
    assert decision.bundle is None
    assert len(decision.individual) == 5

    # Urgent threshold is editable too.
    policy.set_budget(urgent_threshold=0.9)
    assert policy.budget().urgent_threshold == 0.9


# ---------------------------------------------------------------------------
# Quiet periods (§59)
# ---------------------------------------------------------------------------


def test_quiet_period_suppresses_flags_but_not_now_results(client):
    now_item = _svc().create({"reason": "urgent thing", "urgency": 0.9, "importance": 0.8})
    policy = _svc().policy

    # 1) No quiet window → notified.
    flags = policy.notification_flags([now_item], now=PINNED_NOW)
    assert flags[now_item.attention_id].notify is True

    # 2) Focus window covering the pinned clock → flag suppressed.
    policy.add_quiet_period(QuietPeriodKind.FOCUS, PINNED_NOW, PINNED_NOW + timedelta(hours=1))
    flag = policy.notification_flags([now_item], now=PINNED_NOW)[now_item.attention_id]
    assert flag.notify is False
    assert flag.quiet_period is not None
    assert flag.quiet_period.kind is QuietPeriodKind.FOCUS
    assert any("quiet period" in r for r in flag.reasons)

    # 3) The item is STILL in the NOW feed — nothing was removed.
    res = client.get("/api/attention/now")
    assert res.status_code == 200
    data = res.json()["data"]
    assert [d["attention_id"] for d in data] == [now_item.attention_id]
    assert data[0]["status"] == "NOW"
    assert data[0]["urgency"] == 0.9  # urgent state returned, untouched
    assert _svc().get(now_item.attention_id).status is AttentionState.NOW


def test_quiet_period_all_kinds_and_window_edges():
    policy = AttentionPolicyService()
    item = AttentionService().create({"reason": "x", "urgency": 0.5, "importance": 0.5})

    for kind in QuietPeriodKind:
        policy.add_quiet_period(kind, PINNED_NOW, PINNED_NOW + timedelta(hours=1))
    active = policy.active_quiet_period(PINNED_NOW)
    assert active is not None and active.kind is QuietPeriodKind.SLEEP  # first added

    # Outside the window → not active → flag on.
    flag = policy.notification_flags([item], now=PINNED_NOW - timedelta(hours=2))[item.attention_id]
    assert flag.notify is True
    assert flag.quiet_period is None
    flags_late = policy.notification_flags(
        [item], now=PINNED_NOW + timedelta(hours=2)
    )
    assert flags_late[item.attention_id].notify is True


def test_quiet_period_suppresses_urgent_flag_without_changing_state():
    svc = _svc()
    urgent = svc.create({"reason": "fire", "urgency": 1.0, "importance": 1.0, "status": "NOW"})
    svc.policy.add_quiet_period(
        QuietPeriodKind.DRIVING, PINNED_NOW, PINNED_NOW + timedelta(hours=1)
    )

    flag = svc.policy.notification_flags([urgent], now=PINNED_NOW)[urgent.attention_id]
    assert flag.notify is False
    assert any("urgent item" in r for r in flag.reasons)  # honest about what was suppressed
    assert urgent.status is AttentionState.NOW  # state unchanged


# ---------------------------------------------------------------------------
# Deadline escalation (§28)
# ---------------------------------------------------------------------------


def test_deadline_promotion_todays_item_promoted_to_now_with_reason():
    svc = _svc()
    original_why = ["original reason kept"]
    item = svc.create(
        {
            "reason": "pay the invoice",
            "urgency": 0.6,
            "importance": 0.7,
            "due": PINNED_NOW + timedelta(hours=3),
            "status": "TODAY",
            "why": original_why,
        }
    )
    original_priority = item.priority
    assert item.priority > 0.4  # a real TODAY task, not a low-priority FYI

    records = svc.policy.apply_deadline_escalation(svc.list(), now=PINNED_NOW)
    assert len(records) == 1
    rec = records[0]
    assert rec.from_status is AttentionState.TODAY
    assert rec.to_status is AttentionState.NOW
    assert "deadline escalation" in rec.reason and "3.0h away" in rec.reason

    # Score inputs preserved; deterministic reason appended; original why kept.
    assert item.status is AttentionState.NOW
    assert item.urgency == 0.6
    assert item.importance == 0.7
    assert item.priority == original_priority
    assert original_why[0] in item.why
    assert rec.reason in item.why
    # Recorded in the escalation history and the item's action history.
    assert svc.policy.escalation_history(item.attention_id) == [rec]
    actions = svc.policy.action_history(item.attention_id)
    assert [a.action for a in actions] == ["create", "escalate"]


def test_deadline_promotion_respects_threshold_and_only_todays_bucket():
    svc = _svc()
    near = svc.create({"reason": "near", "urgency": 0.5, "importance": 0.5,
                       "due": PINNED_NOW + timedelta(hours=2), "status": "TODAY"})
    far = svc.create({"reason": "far", "urgency": 0.5, "importance": 0.5,
                      "due": PINNED_NOW + timedelta(days=3), "status": "TODAY"})
    overdue_now = svc.create({"reason": "already NOW", "urgency": 0.5, "importance": 0.5,
                              "due": PINNED_NOW - timedelta(hours=1), "status": "NOW"})

    records = svc.policy.apply_deadline_escalation(svc.list(), now=PINNED_NOW)
    assert [r.attention_id for r in records] == [near.attention_id]
    assert near.status is AttentionState.NOW
    assert far.status is AttentionState.TODAY
    assert overdue_now.status is AttentionState.NOW  # already NOW — untouched, no record

    # User-editable threshold: lower it so even the near item stays TODAY.
    svc.policy.set_deadline_promotion_threshold(1.0)
    assert svc.policy.deadline_promotion_threshold() == 1.0
    records2 = svc.policy.apply_deadline_escalation(svc.list(), now=PINNED_NOW)
    assert records2 == []


# ---------------------------------------------------------------------------
# Adaptive escalation (§29)
# ---------------------------------------------------------------------------


def test_adaptive_escalation_preference_applied_per_person_and_channel():
    svc = _svc()
    policy = svc.policy
    item = svc.create(
        {"reason": "reply to Alice", "urgency": 0.5, "importance": 0.6,
         "entities": ["Alice"], "sources": ["email"]}
    )
    other = svc.create(
        {"reason": "note from Bob", "urgency": 0.3, "importance": 0.3,
         "entities": ["Bob"], "sources": ["messages"]}
    )

    assert policy.adaptive_escalation(item).mode.value == "notification"  # no preference yet
    policy.set_escalation_preference(
        "watch_prompt", person="Alice", channel="email", reason="only responds to Watch prompts"
    )
    decision = policy.adaptive_escalation(item)
    assert decision.mode.value == "watch_prompt"
    assert decision.preference_id == "Alice:email"
    assert "Watch prompts" in decision.reason
    # The preference is user-editable and scoped: Bob's item is unaffected.
    assert policy.adaptive_escalation(other).mode.value == "notification"

    # A silent preference suppresses the notification flag (deterministic reason).
    policy.set_escalation_preference("silent", person="Alice", channel="email")
    flag = policy.notification_flags([item], now=PINNED_NOW)[item.attention_id]
    assert flag.notify is False
    assert any("silent" in r for r in flag.reasons)

    # Removing the preference restores the default.
    assert policy.remove_escalation_preference(person="Alice", channel="email") is True
    assert policy.adaptive_escalation(item).mode.value == "notification"


def test_adaptive_escalation_specificity_person_beats_channel():
    policy = AttentionPolicyService()
    item = AttentionService().create({"reason": "x", "urgency": 0.4, "importance": 0.4,
                                       "entities": ["Alice"], "sources": ["email"]})
    policy.set_escalation_preference("escalate", channel="email")
    assert policy.adaptive_escalation(item).mode.value == "escalate"  # channel match
    policy.set_escalation_preference("watch_prompt", person="Alice")
    assert policy.adaptive_escalation(item).mode.value == "watch_prompt"  # person wins
    policy.set_escalation_preference("silent", person="Alice", channel="email")
    assert policy.adaptive_escalation(item).mode.value == "silent"  # most specific wins


# ---------------------------------------------------------------------------
# Action history, audit, reset
# ---------------------------------------------------------------------------


def test_action_history_preserved_across_snooze_and_resolve():
    svc = _svc()
    item = svc.create({"reason": "call pharmacy", "urgency": 0.8, "importance": 0.7})
    svc.snooze(item.attention_id, until=PINNED_NOW + timedelta(days=1))
    assert item.status is AttentionState.TODAY
    svc.resolve(item.attention_id)
    assert item.status is AttentionState.RESOLVED

    actions = svc.policy.action_history(item.attention_id)
    assert [a.action for a in actions] == ["create", "snooze", "resolve"]
    snooze = actions[1]
    assert snooze.reason == "user snoozed the item"
    assert snooze.details["until"] is not None
    assert actions[2].reason == "user resolved the item"
    # History survives the item leaving the live queue.
    assert len(svc.policy.action_history(item.attention_id)) == 3


def test_audit_response_exposes_state_and_reset_wipes_it():
    svc = _svc()
    item = svc.create({"reason": "pay invoice", "urgency": 0.5, "importance": 0.5,
                       "due": PINNED_NOW + timedelta(hours=2), "status": "TODAY",
                       "entities": ["Alice"], "sources": ["email"]})
    svc.policy.set_escalation_preference("watch_prompt", person="Alice")
    svc.policy.apply_deadline_escalation(svc.list(), now=PINNED_NOW)

    audit = svc.policy.audit_response(item, now=PINNED_NOW)
    assert audit.attention_id == item.attention_id
    assert [a.action for a in audit.actions] == ["create", "escalate"]
    assert audit.escalation is not None
    assert audit.escalation.count == 1
    assert "deadline escalation" in audit.escalation.reason
    assert audit.escalation.promoted_to is AttentionState.NOW
    assert audit.adaptive.mode.value == "watch_prompt"
    assert audit.notification is not None
    assert audit.budget is not None

    # A quiet window now suppresses the (promoted) item's flag.
    svc.policy.add_quiet_period(QuietPeriodKind.SLEEP, PINNED_NOW, PINNED_NOW + timedelta(hours=1))
    audit_quiet = svc.policy.audit_response(item, now=PINNED_NOW)
    assert audit_quiet.notification.notify is False
    assert audit_quiet.notification.quiet_period is not None

    # Reset wipes every local preference and both histories.
    svc.policy.reset()
    assert svc.policy.quiet_periods() == []
    assert svc.policy.escalation_preferences() == []
    assert svc.policy.action_history(item.attention_id) == []
    assert svc.policy.escalation_history(item.attention_id) == []
    assert svc.policy.budget().daily_fyi_cap == 3  # defaults restored
    assert item.status is AttentionState.NOW  # reset does not touch the queue


# ---------------------------------------------------------------------------
# Briefing bundling hook (§58)
# ---------------------------------------------------------------------------


def test_briefing_bundles_low_priority_items_into_one_entry_and_keeps_urgent():
    from backend.briefing import get_briefing_service

    svc = _svc()
    _fyi_items(svc, 9)  # 3 individual + 6 bundled
    urgent = svc.create(
        {"reason": "call the plumber", "urgency": 0.9, "importance": 0.5,
         "suggested_action": "Call the plumber now"}
    )
    assert urgent.status is AttentionState.NOW

    brief = get_briefing_service().compose(kind="morning", now=PINNED_NOW)
    # Exactly one bundled entry, appended after the Now actions.
    bundled = [a for a in brief.actions if a.attention_id.startswith("fyi-bundle-")]
    assert len(bundled) == 1
    assert bundled[0].label == "6 low-priority items bundled — review in the briefing"
    # The urgent item keeps its own row (bypasses batching) and ranks first.
    assert brief.actions[0].attention_id == urgent.attention_id
    assert brief.actions[0].label == "Call the plumber now"
    assert len(brief.actions) == 2  # 1 urgent + 1 bundled; the 9 FYI collapsed
    assert brief.actions[1] is bundled[0]


# ---------------------------------------------------------------------------
# Router (AC-R15 verification: item present in /api/attention/now,
# notification flag suppressed by a quiet period)
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    reset_attention_service()
    reset_attention_policy()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_attention_service()
    reset_attention_policy()


def test_now_endpoint_unaffected_by_manual_dnd(client):
    res = client.post(
        "/api/attention",
        json={"reason": "manual DND check", "urgency": 0.9, "importance": 0.9},
    )
    assert res.status_code == 201
    item_id = res.json()["attention_id"]

    # A manual DND window around the real clock → active.
    real_now = datetime.now(UTC)
    get_attention_policy().add_quiet_period(
        QuietPeriodKind.MANUAL_DND,
        real_now - timedelta(minutes=5),
        real_now + timedelta(minutes=5),
    )

    res_now = client.get("/api/attention/now")
    assert res_now.status_code == 200
    assert [d["attention_id"] for d in res_now.json()["data"]] == [item_id]

    flag = get_attention_policy().notification_flags(get_attention_service().list())[item_id]
    assert flag.notify is False
    assert flag.quiet_period is not None
    assert flag.quiet_period.kind is QuietPeriodKind.MANUAL_DND
