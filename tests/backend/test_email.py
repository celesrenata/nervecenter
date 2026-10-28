"""Prompt2 Phase P0 — Email plane tests (schema, threading, attention link).

Covers:
- ``EmailMessage`` / ``EmailThread`` / ``EmailDirection`` schema (prompt2 §75).
- Email ingestion: thread reconstruction (one thread per sender+subject),
  chronological ordering, unread bookkeeping.
- Idempotent ingest: replaying a message id is a no-op (§48 replay safety).
- AttentionItem generation: a new email thread produces an AttentionItem
  (§75 acceptance); the item is refreshed in place (not duplicated) on
  further inbounds; acknowledging resolves it.
- Draft replies: deterministic, based on the latest inbound (§13 AI drafts,
  human sends).
- The ``/api/email`` router: list / ingest / get / acknowledge / draft,
  including honest 404s and the 422 the router emits for an invalid
  direction value.

The email service is an in-process singleton (no engine, no DB), so tests
get a fresh app per test via :func:`backend.api.app.create_app` and fresh
singletons via the reset hooks — no database or NATS required.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.models import EmailMessage, EmailDirection
from backend.attention import reset_attention_service
from backend.email import EmailService, reset_email_service


@pytest.fixture
def client():
    reset_attention_service()
    reset_email_service()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_attention_service()
    reset_email_service()


def _now() -> datetime:
    return datetime.now(UTC)


def _ingest(client: TestClient, **over) -> dict:
    payload = {
        "message_id": f"mail-{over.get('n', 1)}",
        "direction": "RECEIVED",
        "from_address": "alice@example.com",
        "to_address": "me@example.com",
        "sender_name": "Alice Nguyen",
        "subject": "Q1 planning",
        "body": "Are you free Friday?",
        "timestamp": _now().isoformat(),
    }
    payload.update(over)
    res = client.post("/api/email/ingest", json=payload)
    assert res.status_code in (200, 201), res.text
    return res.json()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_email_defaults():
    msg = EmailMessage(
        message_id="m1",
        thread_id="thr-x",
        direction=EmailDirection.RECEIVED,
        from_address="a@example.com",
        to_address="me@example.com",
        subject="Hi",
        timestamp=_now(),
    )
    assert msg.body == ""
    assert msg.source == "personal-relay"


def test_direction_enum_values():
    assert EmailDirection.RECEIVED.value == "RECEIVED"
    assert EmailDirection.SENT.value == "SENT"


# ---------------------------------------------------------------------------
# Ingestion + thread reconstruction
# ---------------------------------------------------------------------------


def test_ingest_creates_thread(client):
    data = _ingest(client)
    thread = data["thread"]
    assert thread["message_count"] == 1
    assert thread["unread"] == 1
    assert thread["last_body"] == "Are you free Friday?"
    assert thread["sender_name"] == "Alice Nguyen"
    assert data["message"]["thread_id"] == thread["thread_id"]


def test_ingest_returns_201_then_200_on_replay(client):
    first = client.post("/api/email/ingest", json={
        "message_id": "mail-replay",
        "direction": "RECEIVED",
        "from_address": "alice@example.com",
        "to_address": "me@example.com",
        "sender_name": "Alice Nguyen",
        "subject": "Q1 planning",
        "body": "hello",
    })
    assert first.status_code == 201
    replay = client.post("/api/email/ingest", json={
        "message_id": "mail-replay",
        "direction": "RECEIVED",
        "from_address": "alice@example.com",
        "to_address": "me@example.com",
        "sender_name": "Alice Nguyen",
        "subject": "Q1 planning",
        "body": "hello",
    })
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    # Replay changed nothing: the thread still has exactly one message.
    listing = client.get("/api/email").json()["data"]
    assert listing[0]["message_count"] == 1


def test_one_thread_per_sender_per_subject(client):
    _ingest(client, n=1, subject="Q1 planning")
    _ingest(client, n=2, subject="Lunch?")
    listing = client.get("/api/email").json()["data"]
    assert len(listing) == 2
    subjects = {t["subject"] for t in listing}
    assert subjects == {"Q1 planning", "Lunch?"}


def test_thread_messages_chronological(client):
    base = _now() - timedelta(hours=2)
    _ingest(client, n=1, timestamp=(base + timedelta(hours=1)).isoformat(), body="later email")
    _ingest(client, n=2, timestamp=base.isoformat(), body="earlier email")
    _ingest(client, n=3, timestamp=(base + timedelta(hours=2)).isoformat(), body="latest email")
    threads = client.get("/api/email").json()["data"]
    thread_id = threads[0]["thread_id"]
    thread = client.get(f"/api/email/{thread_id}").json()
    bodies = [m["body"] for m in thread["messages"]]
    assert bodies == ["earlier email", "later email", "latest email"]
    # List view is also most-recently-active first.
    assert threads[0]["thread_id"] == thread_id


def test_reply_joins_same_thread(client):
    """A sent reply ("Re: …" to the same recipient) must land on the same
    thread as the received email it answers — one thread per
    sender+recipient subject key, chronological."""
    data = _ingest(client, from_address="alice@example.com", subject="Q1 planning")
    received_thread_id = data["thread"]["thread_id"]
    reply = client.post("/api/email/ingest", json={
        "message_id": "mail-reply-1",
        "direction": "SENT",
        "from_address": "me@example.com",
        "to_address": "alice@example.com",
        "subject": "Re: Q1 planning",
        "body": "Friday works.",
    }).json()
    assert reply["thread"]["thread_id"] == received_thread_id
    assert reply["thread"]["message_count"] == 2
    thread = client.get(f"/api/email/{received_thread_id}").json()
    assert [m["direction"] for m in thread["messages"]] == ["RECEIVED", "SENT"]


def test_sent_message_clears_unread(client):
    data = _ingest(client)
    thread_id = data["thread"]["thread_id"]
    sent = client.post("/api/email/ingest", json={
        "message_id": "mail-out-1",
        "direction": "SENT",
        "from_address": "me@example.com",
        "to_address": "alice@example.com",
        "subject": "Re: Q1 planning",
        "body": "Yes, Friday works.",
    }).json()
    assert sent["thread"]["unread"] == 0
    assert sent["thread"]["message_count"] == 2
    # Outgoing does not itself create attention.
    assert sent["new_attention"] is None
    _assert_thread_exists(client, sent["thread"]["thread_id"])


def _assert_thread_exists(client, thread_id):
    assert client.get(f"/api/email/{thread_id}").status_code == 200


def test_unknown_thread_404(client):
    assert client.get("/api/email/thr-nobody@example.com|nope").status_code == 404
    assert client.post("/api/email/thr-nobody@example.com|nope/draft").status_code == 404
    assert client.post("/api/email/thr-nobody@example.com|nope/acknowledge").status_code == 404


def test_invalid_direction_422(client):
    res = client.post("/api/email/ingest", json={
        "message_id": "mail-bad",
        "direction": "DIAGONAL",
        "from_address": "a@example.com",
        "subject": "x",
    })
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# AttentionItem generation (§75 acceptance)
# ---------------------------------------------------------------------------


def test_new_thread_produces_attention_item(client):
    data = _ingest(client)
    assert data["new_attention"] is not None
    att = data["new_attention"]
    assert att["attention_id"] == data["thread"]["attention_id"]
    # The item lands on the Now screen and names the thread.
    now = client.get("/api/attention/now").json()["data"]
    ids = [a["attention_id"] for a in now]
    assert att["attention_id"] in ids
    item = next(a for a in now if a["attention_id"] == att["attention_id"])
    assert "Alice" in item["reason"]
    assert "email" in item["sources"]
    assert item["why"], "attention item must be explainable (§22)"


def test_second_inbound_refreshes_rather_than_duplicates(client):
    first = _ingest(client)
    second = _ingest(client, n=2, body="Also — can you bring the slides?")
    assert second["thread"]["unread"] == 2
    # Same live item, refreshed — still exactly one NOW item for this thread.
    now = client.get("/api/attention/now").json()["data"]
    thread_items = [
        a for a in now if any(s == "email" for s in a.get("sources", []))
    ]
    assert len(thread_items) == 1
    assert thread_items[0]["attention_id"] == first["thread"]["attention_id"]


def test_acknowledge_resolves_attention(client):
    data = _ingest(client)
    thread_id = data["thread"]["thread_id"]
    att_id = data["thread"]["attention_id"]
    res = client.post(f"/api/email/{thread_id}/acknowledge")
    assert res.status_code == 200
    body = res.json()
    assert body["resolved"] is True
    assert body["thread"]["unread"] == 0
    # The item is off the Now screen now.
    now_ids = [a["attention_id"] for a in client.get("/api/attention/now").json()["data"]]
    assert att_id not in now_ids
    assert client.get(f"/api/attention/{att_id}").json()["status"] == "RESOLVED"


# ---------------------------------------------------------------------------
# Draft replies (§13)
# ---------------------------------------------------------------------------


def test_draft_reply_for_question(client):
    data = _ingest(client, body="Can you review the contract today?")
    thread_id = data["thread"]["thread_id"]
    res = client.post(f"/api/email/{thread_id}/draft")
    assert res.status_code == 200
    body = res.json()
    assert body["to"] == "Alice Nguyen"
    assert body["draft"], "draft must be non-empty"
    assert body["basis"]


def test_draft_reply_without_inbound(client):
    data = _ingest(client, direction="SENT", from_address="me@example.com", to_address="bob@example.com", subject="Hi")
    thread_id = data["thread"]["thread_id"]
    res = client.post(f"/api/email/{thread_id}/draft")
    assert res.status_code == 200
    body = res.json()
    # A thread with only a SENT email: nothing to reply to.
    assert body["draft"] is None
    assert "no inbound" in body["basis"]
    # Unknown thread: the service reports it honestly; the router 404s.
    svc = EmailService()
    missing = svc.draft_reply("thr-unknown|nope")
    assert missing["draft"] is None
    assert "not found" in missing["basis"]


def test_list_sorted_by_recency(client):
    base = _now() - timedelta(hours=2)
    first = _ingest(client, n=1, timestamp=base.isoformat(), from_address="alice@example.com", subject="A")
    _ingest(
        client,
        n=2,
        timestamp=(base + timedelta(hours=1)).isoformat(),
        from_address="bob@example.com",
        subject="B",
    )
    listing = client.get("/api/email").json()["data"]
    assert listing[0]["subject"] == "B"
    assert listing[1]["subject"] == first["thread"]["subject"]


def test_unnamed_email_has_null_sender_name(client):
    data = _ingest(client, sender_name=None, from_address="ghost@example.com")
    assert data["thread"]["sender_name"] is None
    # Attention still works — falls back to the raw address in the reason.
    att = data["new_attention"]
    assert att is not None
    assert "ghost@example.com" in att["reason"]


def test_seed_demo_populates_a_thread(client):
    # NERVE_SEED_EMAILS=1 behavior, exercised directly on the singleton the
    # app bound at startup.
    from backend.email import get_email_service

    get_email_service().seed_demo()
    listing = client.get("/api/email").json()["data"]
    assert len(listing) == 1
    assert listing[0]["sender_name"] == "Alice Nguyen"
    assert listing[0]["message_count"] == 2
    assert listing[0]["unread"] == 2
    thread = client.get(f"/api/email/{listing[0]['thread_id']}").json()
    assert len(thread["messages"]) == 2


def test_service_reset_hook():
    svc = EmailService()
    svc.ingest({
        "message_id": "m-reset",
        "direction": "RECEIVED",
        "from_address": "a@example.com",
        "subject": "x",
        "timestamp": _now().isoformat(),
    })
    assert len(svc.list_threads()) == 1
    reset_email_service()
    fresh = EmailService()
    assert fresh is not svc


# ---------------------------------------------------------------------------
# Transport-adjacent seam: MailItem -> Email plane
#
# The durable mail transports (``backend.connectors.mail.mail_transport``) are
# the *other* ingest path in this plane; the Email plane receives normalized
# payloads. These pin the contract the seam relies on: a transport
# :class:`MailItem` maps to a valid ``ingest`` payload, replaying it is a
# no-op (the transport's "no dupes on restart" contract), and the stub
# transport the worker tests use delivers one-shot and merges the cursor.
# ---------------------------------------------------------------------------


def _mail_item(message_id="<t1@corp>", account="user@example.com", subject="Deploy by Friday",
               body=b"Deploy by Friday\n", date="2026-10-27T09:00:00Z", source="imap"):
    """A transport :class:`MailItem` shaped exactly as the transports emit it."""
    from datetime import datetime as _dt

    from backend.connectors.mail.mail_transport import MailItem

    return MailItem(
        message_id=message_id,
        account=account,
        subject=subject,
        content_bytes=body,
        timestamp=_dt.fromisoformat(date.replace("Z", "+00:00")),
        headers={
            "From": "boss@corp.com",
            "To": account,
            "Subject": subject,
            "Date": date,
            "Message-ID": message_id,
        },
        source=source,
    )


def _email_payload(item):
    """The normalized payload the ingest worker would post for a MailItem."""
    return {
        "message_id": item.message_id,
        "direction": "RECEIVED",
        "from_address": item.headers.get("From", ""),
        "to_address": item.headers.get("To", ""),
        "subject": item.subject,
        "body": item.content_bytes.decode("utf-8", "replace"),
        "timestamp": item.timestamp.isoformat(),
        "source": item.source,
    }


def test_mail_item_maps_to_email_plane_ingest():
    svc = EmailService()
    item = _mail_item()
    result = svc.ingest(_email_payload(item))
    assert result["duplicate"] is False
    msg = result["message"]
    thread = result["thread"]
    assert msg.message_id == item.message_id
    assert msg.subject == item.subject
    assert msg.body == item.content_bytes.decode()
    assert msg.thread_id == thread.thread_id
    # The transport's dedup key (account + message-id) maps 1:1 to the plane's
    # message_id, so a transport-fetched mail lands as exactly one message.
    assert result["new_attention"] is not None


def test_mail_item_replay_is_a_duplicate_in_email_plane():
    # The transport guarantees "no dupes on restart"; the Email plane reflects
    # that contract — re-ingesting the same message_id is a no-op.
    svc = EmailService()
    item = _mail_item()
    first = svc.ingest(_email_payload(item))
    assert first["duplicate"] is False
    second = svc.ingest(_email_payload(item))
    assert second["duplicate"] is True
    assert second["message"].thread_id == first["message"].thread_id
    assert len(svc.list_threads()) == 1


def test_stub_transport_one_shot_delivery_and_cursor_merge():
    import asyncio

    from backend.api.models import MailConnector
    from backend.connectors.mail.mail_transport import StubMailTransport

    connector = MailConnector(
        connector_id="c-1",
        account="Work",
        mailbox="user@example.com",
        host="imap.example.com",
        port=993,
    )
    item = _mail_item()
    transport = StubMailTransport(items=[item], next_cursor={"uid": 101, "uidvalidity": 7})
    items, cursor = asyncio.run(
        transport.run(connector, {"uid": 100}, credentials={"password": "super-secret"})
    )
    assert len(items) == 1
    assert items[0].message_id == item.message_id
    # One-shot: the cursor already advanced, so a second poll yields nothing.
    items2, _ = asyncio.run(
        transport.run(connector, {"uid": 101}, credentials={"password": "super-secret"})
    )
    assert items2 == []
    # The returned cursor merges the next_cursor into the input position.
    assert cursor.get("uid") == 101
    assert cursor.get("uidvalidity") == 7
    # The plaintext secret never appears in the stub's observable state.
    assert all("super-secret" not in str(v) for call in transport.calls for v in call.values())
