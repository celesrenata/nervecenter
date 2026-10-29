"""R104 — Follow-up engine tests (AC-R07, spec §57).

Covers:

- **Schema + state machine (AC-R07).** ``FollowUp`` model defaults; the
  transition matrix OPEN ↔ SNOOZED, OPEN|SNOOZED → COMPLETED / FORGOTTEN;
  terminal states immutable (every transition out of them raises
  ``FollowUpTransitionError``).
- **Lifecycle.** create / list / get / update (reason, person, due) and the
  ``followup.created`` / ``followup.updated`` events with stable
  idempotency keys.
- **Deterministic due evaluation.** ``evaluate_due(now)`` returns the due
  OPEN/SNOOZED follow-ups ordered by ``due_at``; a due follow-up creates one
  live ``AttentionItem`` sourced ``followups`` with a reference to the
  follow-up and emits ``followup.triggered``; re-running the evaluation at
  the same ``now`` refreshes the same single item — no stacked duplicates.
- **Action propagation.** snooze suppresses the live AttentionItem (and the
  due time moves to the snooze-until); reopen re-enters it once due;
  complete / forget resolve the item and emit
  ``followup.completed`` / ``followup.forgotten``.
- **API contract.** 201 create, list filter/order, honest 404s, 409 on
  terminal transitions and on double-snooze, 422 on an empty PATCH body and a
  bad status filter.
- **Waiting-on integration (R104 step 3).** ``from_waiting_on`` derives the
  normalized create-payload (due = requested_at + configurable default
  delay). The existing waiting-on ``follow-up`` helper is untouched (see
  ``tests/backend/test_waiting_on.py`` regression run).

The service is an in-process singleton (no engine, no DB), so tests get a
fresh app per test via :func:`backend.api.app.create_app` and fresh
singletons via the reset hooks — no database or NATS required.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.models import WaitingOnItem, WaitingOnSource, WaitingOnStatus
from backend.attention import reset_attention_service
from backend.followups import (
    FollowUp,
    FollowUpService,
    FollowUpStatus,
    FollowUpTransitionError,
    can_transition,
    get_followup_service,
    reset_followup_service,
)
from backend.followups.service import default_follow_up_delay
from backend.waiting_on import reset_waiting_on_service

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


@pytest.fixture
def client():
    reset_attention_service()
    reset_followup_service()
    reset_waiting_on_service()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_attention_service()
    reset_followup_service()
    reset_waiting_on_service()


@pytest.fixture
def svc() -> FollowUpService:
    """A standalone service (no app) with a pinned clock where needed."""
    return FollowUpService()


def _now() -> datetime:
    return datetime.now(UTC)


def _create(client: TestClient, **over) -> dict:
    payload = {
        "target_kind": "waiting_on",
        "target_id": "wo-1",
        "reason": "Follow up with Sam — deployment credentials",
        "person_id": "p-sam",
        # Real-clock-relative so the API (which runs the hook on the wall
        # clock) reports a stable "2 days overdue" regardless of run date.
        "due_at": (_now() - timedelta(days=2)).isoformat(),
        "created_from": "waiting_on:wo-1",
    }
    payload.update(over)
    res = client.post("/api/followups", json=payload)
    assert res.status_code == 201, res.text
    return res.json()


# ---------------------------------------------------------------------------
# Schema + state machine
# ---------------------------------------------------------------------------


def test_followup_schema_defaults():
    fu = FollowUp(
        id="fu-x", target_kind="commitment", target_id="fnd-9",
        reason="Send Q4 review", due_at=NOW,
    )
    assert fu.status == FollowUpStatus.OPEN
    assert fu.person_id is None
    assert fu.created_from is None
    assert fu.attention_id is None
    assert fu.audit_history == []
    assert fu.is_terminal is False
    assert fu.created_at.tzinfo is not None


def test_followup_schema_requires_core_fields():
    with pytest.raises(ValueError):
        FollowUp(id="fu-x", target_kind="", target_id="t", reason="r",
                 due_at=NOW)
    with pytest.raises(ValueError):
        FollowUp(id="fu-x", target_kind="k", target_id="  ", reason="r",
                 due_at=NOW)
    with pytest.raises(ValueError):
        FollowUp(id="fu-x", target_kind="k", target_id="t", reason="   ",
                 due_at=NOW)


def test_status_parse_and_transition_matrix():
    assert FollowUpStatus.parse("open") == "OPEN"
    assert FollowUpStatus.parse(" SNOOZED ") == "SNOOZED"
    with pytest.raises(ValueError):
        FollowUpStatus.parse("done")
    # Legal moves
    assert can_transition("OPEN", "SNOOZED")
    assert can_transition("OPEN", "COMPLETED")
    assert can_transition("OPEN", "FORGOTTEN")
    assert can_transition("SNOOZED", "OPEN")
    assert can_transition("SNOOZED", "COMPLETED")
    assert can_transition("SNOOZED", "FORGOTTEN")
    # Illegal moves
    assert not can_transition("OPEN", "OPEN")
    assert not can_transition("SNOOZED", "SNOOZED")
    # Terminal states are immutable
    for term in ("COMPLETED", "FORGOTTEN"):
        for target in ("OPEN", "SNOOZED", "COMPLETED", "FORGOTTEN"):
            assert not can_transition(term, target)
    # Unknown states are never legal
    assert not can_transition("COMPLETED", "NOPE")


# ---------------------------------------------------------------------------
# Service: create / list / get
# ---------------------------------------------------------------------------


def test_create_defaults_due_to_configurable_default_delay(svc):
    before = _now()
    r = svc.create(target_kind="commitment", target_id="fnd-1",
                   reason="Review PR")
    fu = r["followup"]
    default = default_follow_up_delay()
    assert fu.due_at >= before + default
    assert fu.due_at <= _now() + default + timedelta(seconds=5)
    assert fu.created_from == "commitment:fnd-1"
    assert r["event"].event_type == "followup.created"
    assert r["event"].subject == fu.id
    assert r["event"].idempotency_key == f"followup.created:{fu.id}"
    assert fu.audit_history[0].action == "created"


def test_default_delay_is_configurable(svc, monkeypatch):
    monkeypatch.setenv("NERVE_FOLLOWUP_DELAY_HOURS", "12")
    assert default_follow_up_delay() == timedelta(hours=12)
    r = svc.create(target_kind="commitment", target_id="fnd-2",
                   reason="Ship it")
    # due_at = service-internal now + 12h; compare against the real clock
    # with a small tolerance (the two clocks differ by microseconds).
    assert abs(
        (r["followup"].due_at - _now()) - timedelta(hours=12)
    ) < timedelta(seconds=1)
    monkeypatch.setenv("NERVE_FOLLOWUP_DELAY_HOURS", "garbage")
    assert default_follow_up_delay() == timedelta(hours=72)


def test_list_ordering_and_filter(svc):
    early = svc.create(target_kind="waiting_on", target_id="a",
                       reason="a", due_at="2026-10-21T00:00:00Z")["followup"]
    late = svc.create(target_kind="waiting_on", target_id="b",
                      reason="b", due_at="2026-10-22T00:00:00Z")["followup"]
    done = svc.create(target_kind="waiting_on", target_id="c",
                     reason="c", due_at="2026-10-19T00:00:00Z")["followup"]
    svc.complete(done.id)
    all_items = svc.list()
    assert [f.id for f in all_items] == [done.id, early.id, late.id]
    assert [f.id for f in svc.list(status="OPEN")] == [early.id, late.id]
    assert [f.id for f in svc.list(status="COMPLETED")] == [done.id]
    with pytest.raises(ValueError):
        svc.list(status="bogus")


def test_get_unknown_returns_none(svc):
    assert svc.get("fu-nope") is None


def test_update_edits_live_followup(svc):
    fu = svc.create(target_kind="waiting_on", target_id="w", reason="old",
                    due_at="2026-10-30T00:00:00Z")["followup"]
    updated = svc.update(fu.id, reason="new", person_id="p-x",
                         due_at="2026-10-31T00:00:00Z", now=NOW)
    assert updated.reason == "new"
    assert updated.person_id == "p-x"
    assert updated.due_at == datetime(2026, 10, 31, tzinfo=UTC)
    assert [a.action for a in updated.audit_history] == ["created", "updated"]
    ev = svc.events("followup.updated")
    assert len(ev) == 1
    assert ev[0].subject == fu.id
    assert "reason, person_id, due_at" in ev[0].payload["detail"]


def test_update_empty_reason_rejected(svc):
    fu = svc.create(target_kind="waiting_on", target_id="w", reason="r",
                    due_at="2026-10-30T00:00:00Z")["followup"]
    with pytest.raises(ValueError):
        svc.update(fu.id, reason="   ")
    with pytest.raises(KeyError):
        svc.update("fu-missing", reason="x", now=NOW)


# ---------------------------------------------------------------------------
# Service: due evaluation + attention promotion
# ---------------------------------------------------------------------------


def test_evaluate_due_promotes_to_attention_with_source_and_reference(svc):
    fu = svc.create(target_kind="waiting_on", target_id="wo-1",
                   reason="Follow up with Sam — deployment credentials",
                   person_id="p-sam",
                   due_at="2026-10-18T09:00:00Z")["followup"]
    results = svc.evaluate_due(now=NOW)
    assert len(results) == 1
    assert results[0]["followup"].id == fu.id
    assert results[0]["days_overdue"] == 2
    item = results[0]["attention"]
    assert item.sources == ["followups"]
    assert item.reason == "Follow through: Follow up with Sam — deployment credentials"
    assert fu.attention_id == item.attention_id
    assert item.suggested_action == "Follow through: Follow up with Sam — deployment credentials"
    # The due-item reason text is explainable: the overdue factor is in `why`.
    assert any("2 days overdue" in w for w in item.why)
    assert any("target: waiting_on/wo-1" in w for w in item.why)
    ev = results[0]["event"]
    assert ev.event_type == "followup.triggered"
    assert ev.idempotency_key == f"followup.triggered:{fu.id}"
    assert ev.payload["attention_id"] == fu.attention_id
    assert ev.payload["days_overdue"] == 2


def test_evaluate_due_ignores_not_due_and_terminal(svc):
    due = svc.create(target_kind="waiting_on", target_id="d", reason="due",
                     due_at="2026-10-18T00:00:00Z")["followup"]
    not_due = svc.create(target_kind="waiting_on", target_id="n", reason="n",
                         due_at="2026-10-25T00:00:00Z")["followup"]
    done = svc.create(target_kind="waiting_on", target_id="x", reason="x",
                      due_at="2026-10-18T00:00:00Z")["followup"]
    svc.complete(done.id)
    results = svc.evaluate_due(now=NOW)
    assert [r["followup"].id for r in results] == [due.id]
    assert not_due.attention_id is None and done.attention_id is None
    assert svc.events("followup.triggered") == [] or all(
        e.subject == due.id for e in svc.events("followup.triggered")
    )


def test_evaluate_due_is_idempotent_single_attention_item(svc):
    fu = svc.create(target_kind="waiting_on", target_id="wo-1", reason="r",
                    due_at="2026-10-18T09:00:00Z")["followup"]
    first = svc.evaluate_due(now=NOW)[0]["attention"]
    second = svc.evaluate_due(now=NOW)[0]["attention"]
    assert first.attention_id == second.attention_id
    # The Now screen shows exactly one live item for this follow-up.
    now_items = svc._attention.now()
    assert [i for i in now_items if i.attention_id == first.attention_id]
    assert len([i for i in now_items if i.attention_id == first.attention_id]) == 1
    # One triggered event per evaluation call, all referencing the same fu.
    assert len(svc.events("followup.triggered")) == 2
    assert all(e.subject == fu.id for e in svc.events("followup.triggered"))


def test_evaluate_due_orders_by_due_at(svc):
    a = svc.create(target_kind="waiting_on", target_id="a", reason="a",
                   due_at="2026-10-19T00:00:00Z")["followup"]
    b = svc.create(target_kind="waiting_on", target_id="b", reason="b",
                   due_at="2026-10-17T00:00:00Z")["followup"]
    results = svc.evaluate_due(now=NOW)
    assert [r["followup"].id for r in results] == [b.id, a.id]
    assert results[0]["days_overdue"] == 3 and results[1]["days_overdue"] == 1


# ---------------------------------------------------------------------------
# Service: actions + propagation
# ---------------------------------------------------------------------------


def test_snooze_suppresses_live_item_and_moves_due(svc):
    fu = svc.create(target_kind="waiting_on", target_id="w", reason="r",
                    due_at="2026-10-18T09:00:00Z")["followup"]
    svc.evaluate_due(now=NOW)
    item = svc._attention.get(fu.attention_id)
    assert item.status.value == "NOW"
    svc.snooze(fu.id, until="2026-10-25T09:00:00Z", now=NOW)
    assert fu.status == FollowUpStatus.SNOOZED
    assert fu.due_at == datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    assert svc._attention.get(fu.attention_id).status.value == "SUPPRESSED"
    # Snoozed + not yet due → the due sweep no longer touches it.
    assert svc.evaluate_due(now=NOW) == []
    assert [a.action for a in fu.audit_history] == ["created", "snoozed"]


def test_snooze_relative_for_delta(svc):
    fu = svc.create(target_kind="waiting_on", target_id="w", reason="r",
                    due_at="2026-10-18T09:00:00Z")["followup"]
    svc.snooze(fu.id, for_delta=timedelta(days=2), now=NOW)
    assert fu.due_at == NOW + timedelta(days=2)


def test_reopen_reactivates_item_once_due(svc):
    fu = svc.create(target_kind="waiting_on", target_id="w", reason="r",
                    due_at="2026-10-18T09:00:00Z")["followup"]
    svc.evaluate_due(now=NOW)
    svc.snooze(fu.id, until="2026-10-21T09:00:00Z", now=NOW)
    # Re-open *before* the snooze-until: not due, item stays suppressed.
    svc.reopen(fu.id, now=NOW)
    assert fu.status == FollowUpStatus.OPEN
    assert svc._attention.get(fu.attention_id).status.value == "SUPPRESSED"
    # Re-open *after* the snooze-until: due again, item re-enters the NOW
    # bucket.
    svc.snooze(fu.id, until="2026-10-21T09:00:00Z", now=NOW)
    late = NOW + timedelta(days=2)
    svc.reopen(fu.id, now=late)
    assert svc._attention.get(fu.attention_id).status.value == "NOW"


def test_snooze_requires_live_state(svc):
    fu = svc.create(target_kind="waiting_on", target_id="w", reason="r",
                    due_at="2026-10-18T09:00:00Z")["followup"]
    svc.snooze(fu.id, for_delta=timedelta(hours=1), now=NOW)
    with pytest.raises(FollowUpTransitionError):
        svc.snooze(fu.id, for_delta=timedelta(hours=1), now=NOW)
    with pytest.raises(KeyError):
        svc.reopen("fu-missing", now=NOW)


def test_complete_resolves_item_and_emits_event(svc):
    fu = svc.create(target_kind="waiting_on", target_id="w", reason="r",
                    due_at="2026-10-18T09:00:00Z")["followup"]
    svc.evaluate_due(now=NOW)
    svc.complete(fu.id, note="sam replied", now=NOW)
    assert fu.status == FollowUpStatus.COMPLETED
    assert fu.is_terminal
    assert svc._attention.get(fu.attention_id).status.value == "RESOLVED"
    ev = svc.events("followup.completed")
    assert len(ev) == 1
    assert ev[0].subject == fu.id
    assert ev[0].payload["note"] == "sam replied"
    assert fu.audit_history[-1].action == "completed"


def test_forget_resolves_item_and_emits_event(svc):
    fu = svc.create(target_kind="waiting_on", target_id="w", reason="r",
                    due_at="2026-10-18T09:00:00Z")["followup"]
    svc.evaluate_due(now=NOW)
    svc.forget(fu.id, note="no longer relevant", now=NOW)
    assert fu.status == FollowUpStatus.FORGOTTEN
    assert svc._attention.get(fu.attention_id).status.value == "RESOLVED"
    assert len(svc.events("followup.forgotten")) == 1


def test_terminal_states_are_immutable(svc):
    fu = svc.create(target_kind="waiting_on", target_id="w", reason="r",
                    due_at="2026-10-18T09:00:00Z")["followup"]
    svc.evaluate_due(now=NOW)
    svc.complete(fu.id, now=NOW)
    for action in (
        lambda: svc.complete(fu.id, now=NOW),
        lambda: svc.forget(fu.id, now=NOW),
        lambda: svc.snooze(fu.id, for_delta=timedelta(hours=1), now=NOW),
        lambda: svc.reopen(fu.id, now=NOW),
        lambda: svc.update(fu.id, reason="x", now=NOW),
    ):
        with pytest.raises(FollowUpTransitionError):
            action()
    # A forgotten follow-up is equally locked.
    fu2 = svc.create(target_kind="waiting_on", target_id="w2", reason="r2",
                     due_at="2026-10-18T09:00:00Z")["followup"]
    svc.forget(fu2.id, now=NOW)
    with pytest.raises(FollowUpTransitionError):
        svc.complete(fu2.id, now=NOW)


def test_lifecycle_event_sequence(svc):
    fu = svc.create(target_kind="waiting_on", target_id="w", reason="r",
                    due_at="2026-10-18T09:00:00Z")["followup"]
    svc.evaluate_due(now=NOW)
    svc.complete(fu.id, now=NOW)
    types = [e.event_type for e in svc.events()]
    assert types == ["followup.created", "followup.triggered",
                    "followup.completed"]
    assert all(e.subject == fu.id for e in svc.events())


# ---------------------------------------------------------------------------
# Waiting-on integration (R104 step 3)
# ---------------------------------------------------------------------------


def test_from_waiting_on_derives_normalized_payload():
    item = WaitingOnItem(
        request_id="wo-sam",
        person_display_name="Sam",
        person_id="p-sam",
        description="deployment credentials",
        requested_at=NOW - timedelta(days=1),
        source=WaitingOnSource.MESSAGES,
        status=WaitingOnStatus.OPEN,
    )
    payload = FollowUpService.from_waiting_on(item, now=NOW)
    assert payload["target_kind"] == "waiting_on"
    assert payload["target_id"] == "wo-sam"
    assert payload["person_id"] == "p-sam"
    assert payload["created_from"] == "waiting_on:wo-sam"
    assert payload["reason"] == "Follow up with Sam — deployment credentials"
    assert payload["due_at"] == (NOW - timedelta(days=1)) + timedelta(hours=72)
    # Configurable default timing override.
    payload2 = FollowUpService.from_waiting_on(
        item, now=NOW, follow_up_delay=timedelta(days=3)
    )
    assert payload2["due_at"] == (NOW - timedelta(days=1)) + timedelta(days=3)


def test_waiting_on_service_can_build_and_track_followup(svc):
    item = WaitingOnItem(
        request_id="wo-sam",
        person_display_name="Sam",
        description="deployment credentials",
        requested_at=NOW - timedelta(days=3),
        source=WaitingOnSource.MESSAGES,
        status=WaitingOnStatus.OPEN,
    )
    payload = FollowUpService.from_waiting_on(item, now=NOW)
    fu = svc.create(**payload)["followup"]
    assert fu.created_from == "waiting_on:wo-sam"
    # requested_at (= NOW - 2d) + 72h default delay == NOW → due exactly at
    # NOW (deterministic: pinned clock, no wall-clock drift).
    assert fu.due_at == NOW
    results = svc.evaluate_due(now=NOW)
    assert [r["followup"].id for r in results] == [fu.id]


# ---------------------------------------------------------------------------
# API contract
# ---------------------------------------------------------------------------


def test_create_and_get_via_api(client):
    body = _create(client)
    assert body["status"] == "OPEN"
    assert body["person_id"] == "p-sam"
    got = client.get(f"/api/followups/{body['id']}")
    assert got.status_code == 200
    assert got.json() == body
    assert got.json()["audit_history"][0]["action"] == "created"


def test_list_filters_and_orders_via_api(client):
    _create(client, target_id="a", due_at="2026-10-21T00:00:00Z")
    _create(client, target_id="b", due_at="2026-10-17T00:00:00Z")
    data = client.get("/api/followups").json()
    assert len(data["data"]) == 2
    assert data["meta"]["pagination"]["total"] == 2
    # Most-due first (earliest due_at).
    assert [f["target_id"] for f in data["data"]] == ["b", "a"]
    open_data = client.get("/api/followups", params={"status": "OPEN"}).json()
    assert len(open_data["data"]) == 2
    assert client.get("/api/followups",
                      params={"status": "COMPLETED"}).json()["data"] == []
    # Case-insensitive status.
    assert len(client.get("/api/followups",
                          params={"status": "open"}).json()["data"]) == 2


def test_bad_status_filter_is_422(client):
    assert client.get("/api/followups",
                      params={"status": "bogus"}).status_code == 422


def test_get_unknown_followup_is_404(client):
    assert client.get("/api/followups/fu-nope").status_code == 404


def test_create_validation_is_422(client):
    res = client.post("/api/followups",
                      json={"target_kind": "", "target_id": "t",
                            "reason": "r"})
    assert res.status_code == 422
    res = client.post("/api/followups",
                      json={"target_kind": "k", "target_id": "t",
                            "reason": ""})
    assert res.status_code == 422


def test_update_via_api(client):
    body = _create(client, due_at="2026-10-30T00:00:00Z")
    res = client.patch(f"/api/followups/{body['id']}",
                       json={"reason": "new reason",
                             "due_at": "2026-10-31T00:00:00Z"})
    assert res.status_code == 200, res.text
    assert res.json()["reason"] == "new reason"
    assert res.json()["due_at"] == "2026-10-31T00:00:00Z"
    assert client.patch("/api/followups/fu-nope",
                        json={"reason": "x"}).status_code == 404


def test_update_empty_body_is_422(client):
    body = _create(client)
    assert client.patch(f"/api/followups/{body['id']}",
                        json={}).status_code == 422


def test_snooze_then_complete_via_api(client):
    body = _create(client)
    res = client.post(f"/api/followups/{body['id']}/snooze",
                      json={"for_hours": 48, "detail": "check friday"})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "SNOOZED"
    # Double-snooze is an illegal transition.
    assert client.post(f"/api/followups/{body['id']}/snooze",
                       json={}).status_code == 409
    res = client.post(f"/api/followups/{body['id']}/complete",
                      json={"note": "done"})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "COMPLETED"
    # Terminal: every further action is 409.
    for path, payload in (
        ("snooze", {}),
        ("complete", {}),
        ("forget", {}),
    ):
        assert client.post(
            f"/api/followups/{body['id']}/{path}", json=payload
        ).status_code == 409
    assert client.patch(f"/api/followups/{body['id']}",
                        json={"reason": "x"}).status_code == 409


def test_forget_via_action_unknown_is_404(client):
    for path in ("snooze", "complete", "forget"):
        assert client.post(f"/api/followups/fu-nope/{path}",
                           json={}).status_code == 404


def test_evaluate_endpoint_creates_attention_and_events(client):
    body = _create(client)
    res = client.post("/api/followups/evaluate")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["events_emitted"] == 1
    trig = data["triggered"][0]
    assert trig["followup"]["id"] == body["id"]
    assert trig["event_type"] == "followup.triggered"
    assert trig["days_overdue"] == 2
    assert trig["attention_id"] is not None
    assert "follow up with sam" in trig["attention_reason"].lower()
    # The Now screen shows the item, sourced from followups.
    now_items = client.get("/api/attention/now").json()["data"]
    mine = [a for a in now_items if a["attention_id"] == trig["attention_id"]]
    assert len(mine) == 1
    assert mine[0]["sources"] == ["followups"]
    # Idempotent re-run: same single item, no stacking.
    res2 = client.post("/api/followups/evaluate")
    assert res2.json()["triggered"][0]["attention_id"] == trig["attention_id"]
    now_items2 = client.get("/api/attention/now").json()["data"]
    assert len([a for a in now_items2
                if a["attention_id"] == trig["attention_id"]]) == 1
    # Events are inspectable on the singleton the app bound at startup.
    svc = get_followup_service()
    assert {e.event_type for e in svc.events()} == {"followup.created",
                                                    "followup.triggered"}
    assert all(e.subject == body["id"] for e in svc.events())


def test_evaluate_endpoint_ignores_not_due(client):
    _create(client, due_at=(_now() + timedelta(days=5)).isoformat())
    data = client.post("/api/followups/evaluate").json()
    assert data["triggered"] == []
    assert client.get("/api/attention/now").json()["data"] == []


def test_service_reset_hook():
    svc = FollowUpService()
    svc.create(target_kind="waiting_on", target_id="w", reason="r",
               due_at="2026-10-18T09:00:00Z")
    assert len(svc.list()) == 1
    reset_followup_service()
    fresh = get_followup_service()
    assert fresh is not svc
    assert len(fresh.list()) == 0
