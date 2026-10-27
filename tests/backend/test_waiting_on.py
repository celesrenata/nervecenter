"""Prompt2 §19/§31 — Waiting-On plane tests (outgoing requests, no response).

Covers:
- ``WaitingOnItem`` / ``WaitingOnSource`` / ``WaitingOnStatus`` schema
  (prompt2 §19 — Waiting-On Detection / §31 "What am I waiting for?").
- Idempotent ingest by ``request_id``: 201-new / 200-replay no-op (§48
  replay safety) — a replay never stacks a second AttentionItem or bumps a
  follow-up.
- The deterministic elapsed-time helper with an *injected* ``now`` (0 / 1 /
  3+ days) — no wall-clock jitter.
- AttentionItem lifecycle: exactly one live item is created on ingest while
  OPEN and it is resolved on resolve (so it leaves the Now screen).
- Resolve sets ``status=resolved`` + ``resolved_at``.
- Follow-up bumps ``follow_up_count``, sets ``last_follow_up_at``, returns a
  rule-based draft containing the person name + description (labelled
  draft-only, §13 "AI drafts, human sends"), and keeps the item OPEN.
- List ordering by elapsed desc; the status filter (open/resolved/cancelled/
  all); honest 404s; the 422s for an empty name / description and a bad
  source; ``seed_demo``; and the reset hook.

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
from backend.waiting_on import (
    WaitingOnService,
    get_waiting_on_service,
    reset_waiting_on_service,
)
from backend.waiting_on.service import _days_since


@pytest.fixture
def client():
    reset_attention_service()
    reset_waiting_on_service()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_attention_service()
    reset_waiting_on_service()


def _now() -> datetime:
    return datetime.now(UTC)


def _ingest(client: TestClient, **over) -> dict:
    payload = {
        "request_id": f"wo-{over.get('n', 1)}",
        "person_display_name": "Sam",
        "description": "deployment credentials",
        "requested_at": _now().isoformat(),
        "source": "messages",
    }
    payload.update(over)
    res = client.post("/api/waiting-on/ingest", json=payload)
    assert res.status_code in (200, 201), res.text
    return res.json()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_waiting_on_defaults():
    item = WaitingOnItem(
        request_id="wo-x",
        person_display_name="Sam",
        description="deployment credentials",
        requested_at=_now(),
    )
    # Defaults: OPEN, messages source, no follow-ups, no live attention yet.
    assert item.status is WaitingOnStatus.OPEN
    assert item.source is WaitingOnSource.MESSAGES
    assert item.person_id is None
    assert item.source_thread_id is None
    assert item.follow_up_count == 0
    assert item.last_follow_up_at is None
    assert item.attention_id is None
    # No escalation policy unless the relay posts one (§22/§45).
    assert item.escalation_policy is None


def test_source_and_status_enum_values():
    assert WaitingOnSource.MESSAGES.value == "messages"
    assert WaitingOnSource.EMAIL.value == "email"
    assert WaitingOnStatus.OPEN.value == "open"
    assert WaitingOnStatus.RESOLVED.value == "resolved"
    assert WaitingOnStatus.CANCELLED.value == "cancelled"


# ---------------------------------------------------------------------------
# Ingestion + idempotency
# ---------------------------------------------------------------------------


def test_ingest_creates_open_item_and_attention(client):
    data = _ingest(client)
    item = data["item"]
    assert item["status"] == "open"
    assert item["person_display_name"] == "Sam"
    assert item["description"] == "deployment credentials"
    assert item["follow_up_count"] == 0
    # No escalation policy was posted, so the field is null (not omitted).
    assert item["escalation_policy"] is None
    # A fresh OPEN request produced exactly one live AttentionItem.
    att = data["new_attention"]
    assert att is not None
    assert att["status"] == "NOW"
    assert item["attention_id"] == att["attention_id"]


def test_ingest_stores_escalation_policy_verbatim(client):
    """The relay's escalation policy is stored as posted and surfaces on the
    read endpoints (§22/§45 — the UI renders an "Urgent" marker for
    ``{"priority": "urgent"}``)."""
    res = client.post("/api/waiting-on/ingest", json={
        "request_id": "wo-urgent",
        "person_display_name": "Sam",
        "description": "deployment credentials",
        "source": "messages",
        "escalation_policy": {"priority": "urgent", "window_hours": 24},
    })
    assert res.status_code == 201, res.text
    item = res.json()["item"]
    assert item["escalation_policy"] == {"priority": "urgent", "window_hours": 24}
    # It survives the round trip on GET and on the list projection.
    assert client.get("/api/waiting-on/wo-urgent").json()["escalation_policy"] == {
        "priority": "urgent", "window_hours": 24
    }
    listed = client.get("/api/waiting-on").json()["data"]
    assert next(i for i in listed if i["request_id"] == "wo-urgent")["escalation_policy"] == {
        "priority": "urgent", "window_hours": 24
    }


def test_escalation_policy_is_optional_and_typed(client):
    # An empty object is normalised to null on ingest (``or None``) so the
    # read contract is a clean null, matching the model default.
    res = client.post("/api/waiting-on/ingest", json={
        "request_id": "wo-empty-policy",
        "person_display_name": "Sam",
        "description": "deployment credentials",
        "escalation_policy": {},
    })
    assert res.status_code == 201, res.text
    assert res.json()["item"]["escalation_policy"] is None
    # A malformed policy (e.g. a bare string) is rejected with 422 at the
    # boundary — the field is typed ``dict | null``, not ``Any``.
    bad = client.post("/api/waiting-on/ingest", json={
        "request_id": "wo-bad-policy",
        "person_display_name": "Sam",
        "description": "deployment credentials",
        "escalation_policy": "urgent",
    })
    assert bad.status_code == 422


def test_ingest_is_idempotent_by_request_id(client):
    first = client.post("/api/waiting-on/ingest", json={
        "request_id": "wo-replay",
        "person_display_name": "Sam",
        "description": "deployment credentials",
        "source": "messages",
    })
    assert first.status_code == 201
    replay = client.post("/api/waiting-on/ingest", json={
        "request_id": "wo-replay",
        "person_display_name": "Sam",
        "description": "deployment credentials",
        "source": "messages",
    })
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    # Replay changed nothing: exactly one request, still open, no follow-ups.
    listing = client.get("/api/waiting-on").json()["data"]
    assert len(listing) == 1
    assert listing[0]["follow_up_count"] == 0


def test_replay_does_not_stack_second_attention_item(client):
    first = _ingest(client)  # request_id "wo-1"
    _ingest(client, n=2)  # request_id "wo-2" — a distinct request
    # Replay the first request: no second AttentionItem may be created.
    client.post("/api/waiting-on/ingest", json={
        "request_id": "wo-1",
        "person_display_name": "Sam",
        "description": "deployment credentials",
    })
    now = client.get("/api/attention/now").json()["data"]
    assert len(now) == 2
    # The replayed request still points at the same live item.
    assert client.get("/api/waiting-on/wo-1").json()["attention_id"] == first["item"]["attention_id"]


def test_attention_factor_mentions_person_and_description(client):
    data = _ingest(client)
    att = client.get(f"/api/attention/{data['item']['attention_id']}").json()
    # §22 explainability: the why-list carries the waiting-on factor.
    assert any(
        "waiting on Sam for deployment credentials" in w for w in att["why"]
    ), att["why"]


# ---------------------------------------------------------------------------
# Elapsed-time helper (deterministic, injected now)
# ---------------------------------------------------------------------------


def test_elapsed_days_zero(client):
    base = _now()
    assert _days_since(base, base) == 0


def test_elapsed_days_one(client):
    base = _now()
    assert _days_since(base - timedelta(days=1), base) == 1


def test_elapsed_days_three_or_more(client):
    base = _now()
    assert _days_since(base - timedelta(days=3), base) == 3
    assert _days_since(base - timedelta(days=30), base) == 30


def test_elapsed_days_clamps_to_zero_for_future(client):
    base = _now()
    # A request "in the future" can't have been waiting for negative days.
    assert _days_since(base + timedelta(hours=6), base) == 0


def test_service_days_elapsed_uses_injected_now(client):
    svc = get_waiting_on_service()
    svc.ingest({
        "request_id": "wo-d",
        "person_display_name": "Sam",
        "description": "creds",
        "requested_at": _now() - timedelta(days=4),
    })
    # Injected now → exact, deterministic day count (no wall-clock drift).
    assert svc.days_elapsed("wo-d", now=_now()) == 4
    # A pinned "now" 3 days after requested_at yields exactly 3, not 4.
    assert svc.days_elapsed("wo-d", now=_now() - timedelta(days=1)) == 3
    assert svc.days_elapsed("wo-missing") is None


# ---------------------------------------------------------------------------
# Listing + ordering + status filter
# ---------------------------------------------------------------------------


def test_list_open_ordered_by_elapsed_desc(client):
    base = _now()
    # Oldest (5d) first, then 1d, then today.
    _ingest(client, request_id="wo-new", requested_at=base.isoformat())
    _ingest(client, request_id="wo-mid", requested_at=(base - timedelta(days=1)).isoformat())
    _ingest(client, request_id="wo-old", requested_at=(base - timedelta(days=5)).isoformat())
    listing = client.get("/api/waiting-on").json()["data"]
    assert [i["request_id"] for i in listing] == ["wo-old", "wo-mid", "wo-new"]


def test_default_list_is_open_only(client):
    _ingest(client, request_id="wo-a")
    client.post("/api/waiting-on/wo-a/resolve")
    _ingest(client, request_id="wo-b")
    # Default GET / returns only the OPEN request.
    listing = client.get("/api/waiting-on").json()["data"]
    assert [i["request_id"] for i in listing] == ["wo-b"]


def test_status_filter_all(client):
    _ingest(client, request_id="wo-a")
    _ingest(client, request_id="wo-b")
    client.post("/api/waiting-on/wo-a/resolve")
    # "all" returns every lifecycle state.
    listing = client.get("/api/waiting-on", params={"status": "all"}).json()["data"]
    assert {i["request_id"] for i in listing} == {"wo-a", "wo-b"}
    # "resolved" returns only the closed one.
    resolved = client.get("/api/waiting-on", params={"status": "resolved"}).json()["data"]
    assert [i["request_id"] for i in resolved] == ["wo-a"]
    assert resolved[0]["status"] == "resolved"


def test_status_filter_invalid_is_422(client):
    res = client.get("/api/waiting-on", params={"status": "bogus"})
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# AttentionItem lifecycle
# ---------------------------------------------------------------------------


def test_ingest_open_item_surfaces_on_now(client):
    data = _ingest(client)
    att_id = data["item"]["attention_id"]
    now_ids = [a["attention_id"] for a in client.get("/api/attention/now").json()["data"]]
    assert att_id in now_ids


def test_resolve_resolves_attention_and_leaves_now(client):
    data = _ingest(client)
    att_id = data["item"]["attention_id"]
    res = client.post(f"/api/waiting-on/{data['item']['request_id']}/resolve", json={"note": "got them"})
    assert res.status_code == 200
    body = res.json()
    assert body["item"]["status"] == "resolved"
    assert body["resolved"] is True
    assert body["item"]["resolved_at"] is not None
    assert body["item"]["note"] == "got them"
    # The live item is off the Now screen now and is RESOLVED.
    now_ids = [a["attention_id"] for a in client.get("/api/attention/now").json()["data"]]
    assert att_id not in now_ids
    assert client.get(f"/api/attention/{att_id}").json()["status"] == "RESOLVED"


# ---------------------------------------------------------------------------
# Follow-up (§13 — AI drafts, human sends)
# ---------------------------------------------------------------------------


def test_follow_up_bumps_count_and_returns_draft(client):
    data = _ingest(client, requested_at=(_now() - timedelta(days=3)).isoformat())
    rid = data["item"]["request_id"]
    res = client.post(f"/api/waiting-on/{rid}/follow-up")
    assert res.status_code == 200
    body = res.json()
    # Rule-based draft names the person and the thing requested.
    assert "Sam" in body["draft"]
    assert "deployment credentials" in body["draft"]
    # Draft-only, never sent.
    assert body["draft_only"] is True
    assert "§13" in body["basis"] or "never sent" in body["basis"].lower()
    # Count bumped, timestamp set, and the item stays OPEN.
    assert body["follow_up_count"] == 1
    assert body["last_follow_up_at"] is not None
    assert body["status"] == "open"
    item = client.get(f"/api/waiting-on/{rid}").json()
    assert item["follow_up_count"] == 1
    assert item["status"] == "open"
    # The follow-up did NOT resolve the request's live AttentionItem.
    assert item["attention_id"] is not None


def test_follow_up_draft_includes_day_count_when_elapsed(client):
    data = _ingest(client, requested_at=(_now() - timedelta(days=5)).isoformat())
    rid = data["item"]["request_id"]
    body = client.post(f"/api/waiting-on/{rid}/follow-up").json()
    assert "5 days" in body["draft"]


def test_follow_up_on_replay_keeps_single_attention(client):
    data = _ingest(client)
    rid = data["item"]["request_id"]
    client.post(f"/api/waiting-on/{rid}/follow-up")
    client.post(f"/api/waiting-on/{rid}/follow-up")
    item = client.get(f"/api/waiting-on/{rid}").json()
    assert item["follow_up_count"] == 2
    # Still exactly one live item for this request on the Now screen.
    now = client.get("/api/attention/now").json()["data"]
    assert [a for a in now if a["attention_id"] == item["attention_id"]] and len(
        [a for a in now if a["attention_id"] == item["attention_id"]]
    ) == 1


# ---------------------------------------------------------------------------
# Honest failures
# ---------------------------------------------------------------------------


def test_get_unknown_request_is_404(client):
    res = client.get("/api/waiting-on/wo-does-not-exist")
    assert res.status_code == 404


def test_resolve_unknown_request_is_404(client):
    res = client.post("/api/waiting-on/wo-does-not-exist/resolve")
    assert res.status_code == 404


def test_follow_up_unknown_request_is_404(client):
    res = client.post("/api/waiting-on/wo-does-not-exist/follow-up")
    assert res.status_code == 404


def test_ingest_empty_person_name_is_422(client):
    res = client.post("/api/waiting-on/ingest", json={
        "request_id": "wo-bad",
        "person_display_name": "   ",
        "description": "creds",
    })
    assert res.status_code == 422


def test_ingest_empty_description_is_422(client):
    res = client.post("/api/waiting-on/ingest", json={
        "request_id": "wo-bad",
        "person_display_name": "Sam",
        "description": "",
    })
    assert res.status_code == 422


def test_ingest_bad_source_is_422(client):
    res = client.post("/api/waiting-on/ingest", json={
        "request_id": "wo-bad",
        "person_display_name": "Sam",
        "description": "creds",
        "source": "carrier-pigeon",
    })
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Seeding / reset
# ---------------------------------------------------------------------------


def test_seed_demo_populates_requests(client):
    # NERVE_SEED_WAITING_ON=1 behavior, exercised directly on the singleton
    # the app bound at startup.
    get_waiting_on_service().seed_demo()
    all_list = client.get("/api/waiting-on", params={"status": "all"}).json()["data"]
    assert len(all_list) == 3
    open_list = client.get("/api/waiting-on").json()["data"]
    assert {i["request_id"] for i in open_list} == {
        "wo-seed-sam-creds", "wo-seed-insurance-claim"
    }
    # The resolved request closed its loop and carries a note.
    resolved = client.get("/api/waiting-on/wo-seed-alice-review").json()
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"] is not None
    # §19 escalation demo: 3 days unanswered is urgent, the 1-day claim is not
    # — so the Waiting-On screen shows exactly one "Urgent" marker.
    by_id = {i["request_id"]: i for i in all_list}
    assert by_id["wo-seed-sam-creds"]["escalation_policy"] == {"priority": "urgent"}
    assert by_id["wo-seed-insurance-claim"]["escalation_policy"] is None


def test_service_reset_hook():
    svc = WaitingOnService()
    svc.ingest({
        "request_id": "wo-reset",
        "person_display_name": "Sam",
        "description": "creds",
        "requested_at": _now().isoformat(),
    })
    assert len(svc.list_all()) == 1
    reset_waiting_on_service()
    fresh = get_waiting_on_service()
    assert fresh is not svc
    assert len(fresh.list_all()) == 0
