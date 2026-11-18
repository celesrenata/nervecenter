"""Prompt2 Phase P2 — Voicemail plane tests (schema, threading, attention link).

Covers:
- ``Voicemail`` / ``VoicemailThread`` / ``VoicemailSource`` schema (prompt2 §77).
- Voicemail ingestion: thread reconstruction (one thread per caller phone
  number, non-digits stripped for the key; display format kept separately),
  chronological ordering, unread bookkeeping.
- Idempotent ingest: replaying a ``voicemail_id`` is a no-op (§48 replay safety).
- AttentionItem generation: a new voicemail thread produces an AttentionItem
  (§77 acceptance); the item is refreshed in place (not duplicated) on further
  inbounds; acknowledging resolves it.
- Callback-request detection (§15/§77): a deterministic keyword heuristic on the
  already-transcribed transcript, positive cases (multiple phrasings) and
  negative cases (ordinary message → ``needs_callback`` False,
  ``callback_reason`` None). A callback-request voicemail surfaces a stronger,
  explainable factor in the item's ``why``.
- The ``/api/voicemail`` router: list / ingest / get / acknowledge, including
  honest 404s and the 422 the router emits for an invalid source value. There is
  deliberately no draft/reply endpoint (§77 has no reply drafting for voicemail).

The voicemail service is an in-process singleton (no engine, no DB), so tests
get a fresh app per test via :func:`backend.api.app.create_app` and fresh
singletons via the reset hooks — no database or NATS required.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.models import AttentionState, VoicemailSource
from backend.attention import get_attention_service, reset_attention_service
from backend.voicemail import VoicemailService, reset_voicemail_service
from backend.voicemail.service import detect_callback


@pytest.fixture
def client():
    reset_attention_service()
    reset_voicemail_service()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_attention_service()
    reset_voicemail_service()


def _now() -> datetime:
    return datetime.now(UTC)


def _ingest(
    client: TestClient,
    voicemail_id: str,
    caller_phone: str,
    *,
    caller_name: str | None = None,
    transcript: str = "Hi, this is a voicemail.",
    duration: int = 15,
    received_at: datetime | None = None,
    source: str = "share-sheet",
):
    payload = {
        "voicemail_id": voicemail_id,
        "caller_phone": caller_phone,
        "caller_name": caller_name,
        "transcript": transcript,
        "duration_seconds": duration,
        "received_at": (received_at or _now()).isoformat(),
        "source": source,
    }
    return client.post("/api/voicemail/ingest", json=payload)


# -- schema -------------------------------------------------------------------
def test_source_enum_values():
    """The provenance enum mirrors the §14 ingestion priority (out-of-scope relay)."""
    assert VoicemailSource.SHARE_SHEET.value == "share-sheet"
    assert VoicemailSource.CARRIER_EMAIL.value == "carrier-email"
    assert VoicemailSource.IMPORTED.value == "imported"
    assert VoicemailSource.MAC_PHONE_STORE.value == "mac-phone-store"


def test_unknown_source_is_rejected_with_422(client):
    """An unrecognized source value is a Pydantic 422, not a stored error."""
    r = _ingest(client, "vm-bad-source", "+1 415-555-0134", source="bogus")
    assert r.status_code == 422


# -- threading ---------------------------------------------------------------
def test_same_number_same_thread(client):
    """One thread per caller phone number — formatting differences collapse.

    ``+1 415-555-0134`` and ``(415) 555-0134 ext. 0`` both key to the same
    caller number (non-digits stripped, the ext's trailing digits aside — we
    use two pure-format variants of the *same* full number)."""
    a = _ingest(client, "vm-a", "+1 415-555-0134", caller_name="Alice").json()
    b = _ingest(client, "vm-b", "1(415) 555-0134").json()
    assert a["thread"]["thread_id"] == b["thread"]["thread_id"] == "vthr-14155550134"

    listing = client.get("/api/voicemail").json()
    assert len(listing["data"]) == 1
    assert listing["data"][0]["voicemail_count"] == 2


def test_different_numbers_different_threads(client):
    """Different caller numbers → different threads."""
    a = _ingest(client, "vm-a", "+1 415-555-0134", caller_name="Alice").json()
    b = _ingest(client, "vm-b", "+1 415-555-0199", caller_name="Bob").json()
    assert a["thread"]["thread_id"] != b["thread"]["thread_id"]

    listing = client.get("/api/voicemail").json()
    assert len(listing["data"]) == 2


def test_display_format_kept_separately(client):
    """The thread key strips non-digits; the display format is preserved."""
    body = _ingest(client, "vm-a", "+1 415-555-0134").json()
    assert body["voicemail"]["caller_phone"] == "+1 415-555-0134"
    assert body["thread"]["caller_phone"] == "+1 415-555-0134"
    assert body["thread"]["thread_id"] == "vthr-14155550134"


def test_chronological_ordering(client):
    """Voicemails in a thread come back oldest-first regardless of ingest order."""
    late = _now()
    early = late - timedelta(minutes=10)
    _ingest(client, "vm-late", "+1 415-555-0134", transcript="second call", received_at=late)
    _ingest(client, "vm-early", "+1 415-555-0134", transcript="first call", received_at=early)

    tid = client.get("/api/voicemail").json()["data"][0]["thread_id"]
    body = client.get(f"/api/voicemail/{tid}").json()
    order = [v["voicemail_id"] for v in body["voicemails"]]
    assert order == ["vm-early", "vm-late"]


# -- idempotency -------------------------------------------------------------
def test_ingest_is_idempotent_by_voicemail_id(client):
    """Re-posting the same voicemail_id is a no-op (§48 replay safety)."""
    first = _ingest(client, "vm-x", "+1 415-555-0134")
    assert first.status_code == 201
    first_thread = first.json()["thread"]["thread_id"]

    second = _ingest(client, "vm-x", "+1 415-555-0134", transcript="replayed")
    assert second.status_code == 200
    assert second.json()["duplicate"] is True

    body = client.get(f"/api/voicemail/{first_thread}").json()
    assert len(body["voicemails"]) == 1
    assert body["voicemails"][0]["transcript"] != "replayed"

    assert client.get("/api/voicemail").json()["meta"]["pagination"]["total"] == 1


# -- callback detection (deterministic keyword heuristic) ---------------------
def test_callback_detected_multiple_phrasings(client):
    """Each §15-style phrasing is flagged and the matched phrase is recorded."""
    cases = [
        ("vm-c1", "Please call us regarding your appointment.", "please call us"),
        ("vm-c2", "Can you call me back when you get a chance?", "call me back"),
        ("vm-c3", "Please have your team ring us back about the invoice.", "ring us back"),
        ("vm-c4", "This is an automated message; call back to speak with the operator.", "call back"),
        ("vm-c5", "Your callback has been recorded.", "callback"),
        ("vm-c6", "Give me a call before Friday.", "give me a call"),
    ]
    for vid, transcript, expected_reason in cases:
        body = _ingest(client, vid, "+1 202-555-0100", transcript=transcript).json()
        vm = body["voicemail"]
        assert vm["needs_callback"] is True, transcript
        assert vm["callback_reason"] == expected_reason, (transcript, vm["callback_reason"])
        assert expected_reason in vm["callback_reason"].lower()


def test_callback_reason_is_explainable(client):
    """The matched keyword/phrase lands in callback_reason and the AttentionItem why."""
    body = _ingest(
        client, "vm-cb", "+1 202-555-0100", caller_name="Dr. Smith",
        transcript="Please call us regarding your appointment.",
    ).json()
    assert body["voicemail"]["callback_reason"] == "please call us"
    item = body["new_attention"]
    assert item is not None
    assert any("callback-request" in f for f in item["why"])
    # The callback ask is surfaced with a stronger urgency than an ordinary message.
    assert item["urgency"] >= 0.9


def test_callback_negative_ordinary_message(client):
    """An ordinary message is not flagged: needs_callback False, reason None."""
    body = _ingest(
        client, "vm-ord", "+1 202-555-0100", caller_name="Alice",
        transcript="Just confirming our meeting is still on for Thursday. No rush.",
    ).json()
    vm = body["voicemail"]
    assert vm["needs_callback"] is False
    assert vm["callback_reason"] is None


def test_callback_detection_is_deterministic_pure_function():
    """detect_callback is a pure, deterministic keyword heuristic (no model)."""
    assert detect_callback("please CALL US about the order") == "please call us"
    assert detect_callback("nothing to say") is None
    assert detect_callback("") is None
    # Case-insensitive + word-boundary: "callback" must not fire on "call-back"
    # as a different token boundary, nor on a substring that isn't a word.
    assert detect_callback("Call Us. Now.") == "call us"
    assert detect_callback("the callcenter called about call-usage fees") is None


# -- unread + acknowledge + attention ---------------------------------------
def test_unread_badge_and_acknowledge_resolves_attention(client):
    """Unread count → acknowledge clears it AND resolves the AttentionItem."""
    body = _ingest(client, "vm-a", "+1 415-555-0134", caller_name="Alice").json()
    tid = body["thread"]["thread_id"]
    aid = body["thread"]["attention_id"]

    # New inbound → unread 1, live AttentionItem.
    assert client.get(f"/api/voicemail/{tid}").json()["thread"]["unread"] == 1
    assert aid is not None
    assert get_attention_service().get(aid).status.value == "NOW"

    ack = client.post(f"/api/voicemail/{tid}/acknowledge").json()
    assert ack["resolved"] is True
    assert client.get(f"/api/voicemail/{tid}").json()["thread"]["unread"] == 0
    assert get_attention_service().get(aid).status.value == "RESOLVED"


def test_attention_item_refreshed_not_duplicated(client):
    """A source keeps exactly ONE live AttentionItem; inbounds refresh it in place."""
    first = _ingest(client, "vm-a", "+1 415-555-0134").json()
    second = _ingest(client, "vm-b", "+1 415-555-0134").json()
    assert second["thread"]["attention_id"] == first["thread"]["attention_id"]

    # The second inbound (unread now 2, not a callback) refreshes the same item
    # rather than stacking a second one.
    live = [i for i in get_attention_service().list() if i.sources == ["voicemail"]]
    assert len(live) == 1


def test_acknowledge_clears_all_voicemails_read(client):
    body = _ingest(client, "vm-a", "+1 415-555-0134").json()
    tid = body["thread"]["thread_id"]
    client.post(f"/api/voicemail/{tid}/acknowledge")
    body = client.get(f"/api/voicemail/{tid}").json()
    assert all(v["read"] for v in body["voicemails"])


# -- list ordering -----------------------------------------------------------
def test_list_sorted_by_recency(client):
    late = _now()
    early = late - timedelta(hours=1)
    _ingest(client, "vm-late", "+1 415-555-0134", received_at=late)
    _ingest(client, "vm-early", "+1 415-555-0199", received_at=early)

    listing = client.get("/api/voicemail").json()["data"]
    assert len(listing) == 2
    late_id = [t["thread_id"] for t in listing if t["caller_phone"] == "+1 415-555-0134"][0]
    assert listing[0]["thread_id"] == late_id


# -- null caller fallback ----------------------------------------------------
def test_null_caller_fallback(client):
    """Unknown caller: caller_name stays null; attention falls back to the number."""
    body = _ingest(client, "vm-unknown", "+1 415-555-0134", caller_name=None).json()
    assert body["voicemail"]["caller_name"] is None
    assert body["thread"]["caller_name"] is None
    # A later ingestion from the same number can fill in the name.
    _ingest(client, "vm-unknown2", "+1 415-555-0134", caller_name="Alice")
    thread = client.get("/api/voicemail").json()["data"][0]
    assert thread["caller_name"] == "Alice"


def test_no_caller_name_in_attention_reason(client):
    body = _ingest(client, "vm-anon", "+1 415-555-0134", transcript="hello there").json()
    item = body["new_attention"]
    assert item is not None
    assert "+1 415-555-0134" in item["reason"]


# -- honest failures ----------------------------------------------------------
def test_get_unknown_thread_404(client):
    r = client.get("/api/voicemail/vthr-999")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_acknowledge_unknown_thread_404(client):
    r = client.post("/api/voicemail/vthr-999/acknowledge")
    assert r.status_code == 404


def test_ingest_missing_required_field_422(client):
    """Missing caller_phone (required) → 422."""
    r = client.post(
        "/api/voicemail/ingest",
        json={"voicemail_id": "vm-1", "transcript": "hello"},
    )
    assert r.status_code == 422


def test_empty_inbox_is_honest(client):
    """No voicemails → an empty list, not an error."""
    r = client.get("/api/voicemail")
    assert r.status_code == 200
    assert r.json()["data"] == []


# -- seeding / reset ----------------------------------------------------------
def test_seed_demo_populates_a_thread_with_callback(client):
    """NERVE_SEED_VOICEMAILS=1 behavior, exercised directly on the singleton the
    app bound at startup — includes one callback-request voicemail (§15 example)."""
    from backend.voicemail import get_voicemail_service

    get_voicemail_service().seed_demo()
    listing = client.get("/api/voicemail").json()
    assert listing["meta"]["pagination"]["total"] == 1
    thread = listing["data"][0]
    assert thread["voicemail_count"] == 2
    assert thread["needs_callback"] is True  # second seeded voicemail is a callback ask
    assert thread["callback_reason"] == "please call us"


def test_service_reset_hook():
    svc = VoicemailService()
    svc.ingest({
        "voicemail_id": "vm-reset",
        "caller_phone": "+1 415-555-0134",
        "transcript": "x",
        "received_at": _now().isoformat(),
    })
    assert len(svc.list_threads()) == 1
    reset_voicemail_service()
    fresh = VoicemailService()
    assert fresh is not svc


# -- mac-phone-store source: read state and retraction -------------------------
def _post(client, vid, phone="+1 415-555-0134", **extra):
    payload = {
        "voicemail_id": vid,
        "caller_phone": phone,
        "transcript": extra.pop("transcript", "hello"),
        "duration_seconds": 10,
        "received_at": extra.pop("received_at", _now().isoformat()),
        "source": "mac-phone-store",
        **extra,
    }
    return client.post("/api/voicemail/ingest", json=payload)


def test_mac_phone_store_source_accepted(client):
    r = _post(client, "vm-s1")
    assert r.status_code == 201
    assert r.json()["voicemail"]["source"] == "mac-phone-store"


def test_read_voicemail_adds_no_unread_and_no_attention(client):
    body = _post(client, "vm-r1", read=True).json()
    assert body["voicemail"]["read"] is True
    assert body["thread"]["unread"] == 0
    assert body["new_attention"] is None
    assert body["thread"]["attention_id"] is None


def test_delete_one_of_two_recomputes_thread(client):
    t0 = _now() - timedelta(hours=2)
    _post(client, "vm-d1", transcript="first", received_at=t0.isoformat())
    _post(client, "vm-d2", transcript="please call me", received_at=_now().isoformat())
    r = _post(client, "vm-d2", deleted=True)
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is True and body["found"] is True
    thread = body["thread"]
    assert thread["voicemail_count"] == 1
    assert thread["unread"] == 1
    assert thread["last_transcript"] == "first"
    assert thread["needs_callback"] is False
    assert thread["callback_reason"] is None


def test_delete_last_removes_thread_and_resolves_attention(client):
    created = _post(client, "vm-l1").json()
    attention_id = created["new_attention"]["attention_id"]
    r = _post(client, "vm-l1", deleted=True)
    assert r.status_code == 200
    assert r.json()["thread"] is None
    assert client.get("/api/voicemail").json()["meta"]["pagination"]["total"] == 0
    item = get_attention_service().get(attention_id)
    assert item.status not in (
        AttentionState.NOW, AttentionState.TODAY, AttentionState.SOON, AttentionState.WAITING_ON
    )


def test_delete_unknown_id_is_noop_and_replay_safe(client):
    r = _post(client, "vm-nope", deleted=True)
    assert r.status_code == 200
    assert r.json()["found"] is False
    _post(client, "vm-k1")
    assert _post(client, "vm-k1", deleted=True).json()["found"] is True
    again = _post(client, "vm-k1", deleted=True)
    assert again.status_code == 200 and again.json()["found"] is False
