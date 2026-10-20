"""R102 — ingest-plane normalization into the unified communications registry.

Covers the §7 ``Communication`` shape adapters for the existing messages /
email / voicemail ingest payloads, plus the §62 cross-channel scenario
(AC-R03). Asserts:

- **Mapping:** message / email / voicemail payloads map to the §7 shape
  (channel, direction, sender, recipients, timestamp, thread provenance,
  deterministic ``content_ref``) without altering source contracts.
- **Privacy:** raw content (message body, email subject/body, voicemail
  transcript) never lands in the normalized spec or the stored
  ``Communication`` — only a ``content_ref`` pointer.
- **Conservative attribution (§6 / AC-R02):** resolved identities link the
  existing person; unresolved / same-name-different-value peers leave the
  communication unlinked rather than guessing.
- **Idempotent replay (§48):** re-normalizing the same source ids creates no
  duplicate person, communication, or conversation.
- **§62 cross-channel view:** email + iMessage + voicemail for one person
  yield one ``Person``, three ``Communication``s, and one cross-channel
  conversation with context available from all three channels.

The callback-attention portion of §62 is reported as an unresolved AC (see
``test_alice_scenario_callback_attention_is_an_r103_blocker``): the
``AttentionItem`` model has no structured callback/request metadata field and
``backend/api/models.py`` is not an R102-owned file, so wiring it here would
require speculative coupling. The attention service is left unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
    ContactIdentityKind,
)
from backend.communications.normalize import (
    CommunicationNormalizer,
    ContentRefError,
    NormalizedCommunication,
    content_ref,
    cross_channel_conversation,
    normalize_batch,
    normalize_email,
    normalize_message,
    normalize_voicemail,
)
from backend.communications.registry import CommunicationsService


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def service() -> CommunicationsService:
    return CommunicationsService()


@pytest.fixture
def normalizer(service: CommunicationsService) -> CommunicationNormalizer:
    return CommunicationNormalizer(service)


# §62 Alice fixture: one canonical person reachable over email, iMessage, and
# voicemail. Identities are seeded value-first so the three ingest planes
# resolve to the *same* person (the §62 "same person across channels" premise).
ALICE = {
    "person": "alice",
    "display_name": "Alice",
    "email": "alice@example.com",
    "phone": "+14155550134",
    "imessage_handle": "+14155550134",
    "voicemail_thread": "vthr-14155550134",
    "email_thread": "email-thread-alice",
    "message_conversation": "conv-+14155550134|imessage",
    "email_at": "2026-10-20T10:00:00Z",
    "imessage_at": "2026-10-20T10:12:00Z",
    "voicemail_at": "2026-10-20T13:40:00Z",
}


def _seed_alice(service: CommunicationsService) -> str:
    """Seed Alice's confirmed cross-channel identities into the registry.

    Value-first resolution links phone/imessage/voicemail (all canonicalize
    to the same E.164 number) to one person automatically. The email
    address is a *different* canonical value, so R101's conservative rule
    keeps it as a separate PROPOSED person — the fixture confirms the match
    with the explicit ``merge_people`` action (AC-R02), modelling the
    \"same resolved identity\" premise of §62.
    """
    email_person = service.ingest_identity(
        {"kind": ContactIdentityKind.EMAIL, "value": ALICE["email"], "display_name": ALICE["display_name"]}
    )["person"].person_id
    phone_person = service.ingest_identity(
        {"kind": ContactIdentityKind.PHONE, "value": ALICE["phone"], "display_name": ALICE["display_name"]}
    )["person"].person_id
    # Confirmed match: join the email identity onto the phone person.
    service.merge_people(phone_person, email_person)
    for kind, value in (
        (ContactIdentityKind.IMESSAGE, ALICE["imessage_handle"]),
        (ContactIdentityKind.VOICEMAIL, ALICE["phone"]),
    ):
        service.ingest_identity(
            {"kind": kind, "value": value, "display_name": ALICE["display_name"]}
        )
    people = service.list_people()
    assert len(people) == 1, "all four identities must resolve to one person"
    return people[0].person_id


def _alice_email() -> dict:
    return {
        "message_id": "alice-email-1",
        "thread_id": ALICE["email_thread"],
        "direction": "RECEIVED",
        "from_address": ALICE["email"],
        "to_address": "me@example.com",
        "sender_name": "Alice",
        "subject": "Details",
        "body": "Here are the details, call me when you see it.",
        "timestamp": ALICE["email_at"],
        "source": "personal-relay",
    }


def _alice_message() -> dict:
    return {
        "message_id": "alice-msg-1",
        "conversation_id": ALICE["message_conversation"],
        "direction": "INBOUND",
        "peer_identifier": ALICE["imessage_handle"],
        "peer_name": "Alice",
        "body": "Sent you the thing.",
        "channel": "imessage",
        "timestamp": ALICE["imessage_at"],
        "source": "personal-relay",
    }


def _alice_voicemail() -> dict:
    return {
        "voicemail_id": "alice-vm-1",
        "thread_id": ALICE["voicemail_thread"],
        "caller_phone": ALICE["phone"],
        "caller_name": "Alice",
        "transcript": "Call me when you see it.",
        "duration_seconds": 12,
        "received_at": ALICE["voicemail_at"],
        "needs_callback": True,
        "callback_reason": "call me",
        "source": "share-sheet",
    }


def _alice_batch() -> list[tuple[str, dict]]:
    return [
        ("email", _alice_email()),
        ("message", _alice_message()),
        ("voicemail", _alice_voicemail()),
    ]


# ---------------------------------------------------------------------------
# content_ref helper
# ---------------------------------------------------------------------------


def test_content_ref_is_deterministic_and_stable():
    assert content_ref("message", "m-1") == "message:m-1"
    assert content_ref("email", "e-2") == "email:e-2"
    assert content_ref("voicemail", "v-3") == "voicemail:v-3"
    assert content_ref("message", "m-1") == "message:m-1"


def test_content_ref_rejects_empty_inputs():
    with pytest.raises(ContentRefError):
        content_ref("", "m-1")
    with pytest.raises(ContentRefError):
        content_ref("message", "")
    with pytest.raises(ContentRefError):
        content_ref("   ", "m-1")


def test_content_ref_ignores_missing_string():
    with pytest.raises(ContentRefError):
        content_ref(None, "m-1")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# pure adapters: mapping + privacy
# ---------------------------------------------------------------------------


def test_message_inbound_maps_imessage_and_content_ref():
    spec = normalize_message(_alice_message())
    assert isinstance(spec, NormalizedCommunication)
    assert spec.communication_id == "alice-msg-1"
    assert spec.channel is CommunicationChannel.IMESSAGE
    assert spec.direction is CommunicationDirection.INCOMING
    assert spec.sender == ALICE["imessage_handle"]
    assert spec.recipients == []
    assert spec.thread_id == ALICE["message_conversation"]
    assert spec.content_ref == "message:alice-msg-1"
    assert spec.source == "personal-relay"
    assert spec.timestamp == datetime(2026, 10, 20, 10, 12, tzinfo=UTC)


def test_message_outbound_maps_recipient_and_self_sender():
    spec = normalize_message(
        {
            "message_id": "m-out",
            "conversation_id": "conv-x",
            "direction": "OUTBOUND",
            "peer_identifier": "+15551234",
            "peer_name": "Bob",
            "body": "on my way",
            "timestamp": "2026-10-20T12:00:00Z",
        }
    )
    assert spec.direction is CommunicationDirection.OUTGOING
    assert spec.sender == "self"
    assert spec.recipients == ["+15551234"]
    assert spec.content_ref == "message:m-out"


def test_message_body_is_never_stored_in_spec():
    spec = normalize_message(_alice_message())
    dumped = spec.model_dump()
    assert "body" not in dumped
    assert "Sent you the thing." not in repr(dumped)


def test_message_unknown_channel_defaults_to_imessage():
    spec = normalize_message(
        {
            "message_id": "m-ch",
            "conversation_id": "c",
            "direction": "INBOUND",
            "peer_identifier": "+15550000",
            "body": "x",
            "channel": "carrier-unknown",
            "timestamp": "2026-10-20T09:00:00Z",
        }
    )
    assert spec.channel is CommunicationChannel.IMESSAGE


def test_message_explicit_sms_channel_is_preserved():
    spec = normalize_message(
        {
            "message_id": "m-sms",
            "conversation_id": "c",
            "direction": "INBOUND",
            "peer_identifier": "+15550001",
            "body": "x",
            "channel": "SMS",
            "timestamp": "2026-10-20T09:00:00Z",
        }
    )
    assert spec.channel is CommunicationChannel.SMS


def test_message_requires_message_id():
    with pytest.raises(ContentRefError):
        normalize_message({"conversation_id": "c", "timestamp": "2026-10-20T09:00:00Z"})


def test_message_requires_timestamp():
    with pytest.raises(ValueError):
        normalize_message(
            {"message_id": "m-1", "conversation_id": "c", "direction": "INBOUND", "peer_identifier": "p"}
        )


def test_email_received_maps_incoming_from_identity_and_content_ref():
    spec = normalize_email(_alice_email())
    assert isinstance(spec, NormalizedCommunication)
    assert spec.communication_id == "alice-email-1"
    assert spec.channel is CommunicationChannel.EMAIL
    assert spec.direction is CommunicationDirection.INCOMING
    assert spec.sender == ALICE["email"]
    assert spec.recipients == ["me@example.com"]
    assert spec.thread_id == ALICE["email_thread"]
    assert spec.content_ref == "email:alice-email-1"
    assert spec.source == "personal-relay"
    assert spec.timestamp == datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


def test_email_sent_maps_outgoing_to_recipient():
    spec = normalize_email(
        {
            "message_id": "e-sent",
            "thread_id": "t",
            "direction": "SENT",
            "from_address": "me@example.com",
            "to_address": "alice@example.com",
            "subject": "Re: Details",
            "body": "done",
            "timestamp": "2026-10-20T10:30:00Z",
        }
    )
    assert spec.direction is CommunicationDirection.OUTGOING
    assert spec.sender == "self"
    assert spec.recipients == ["alice@example.com"]
    assert spec.content_ref == "email:e-sent"


def test_email_subject_and_body_are_never_stored_in_spec():
    spec = normalize_email(_alice_email())
    dumped = spec.model_dump()
    assert "subject" not in dumped
    assert "body" not in dumped
    blob = repr(dumped)
    assert "Details" not in blob
    assert "Here are the details" not in blob


def test_email_requires_message_id():
    with pytest.raises(ContentRefError):
        normalize_email({"thread_id": "t", "direction": "RECEIVED", "from_address": "a@b.c"})


def test_voicemail_maps_incoming_caller_identity_and_content_ref():
    spec = normalize_voicemail(_alice_voicemail())
    assert isinstance(spec, NormalizedCommunication)
    assert spec.communication_id == "alice-vm-1"
    assert spec.channel is CommunicationChannel.VOICEMAIL
    assert spec.direction is CommunicationDirection.INCOMING
    assert spec.sender == ALICE["phone"]
    assert spec.recipients == []
    assert spec.thread_id == ALICE["voicemail_thread"]
    assert spec.content_ref == "voicemail:alice-vm-1"
    assert spec.source == "share-sheet"
    assert spec.timestamp == datetime(2026, 10, 20, 13, 40, tzinfo=UTC)


def test_voicemail_uses_caller_name_when_phone_missing():
    spec = normalize_voicemail(
        {"voicemail_id": "v-1", "caller_name": "Bob", "transcript": "hi", "received_at": "2026-10-20T11:00:00Z"}
    )
    assert spec.sender == "Bob"


def test_voicemail_transcript_is_never_stored_in_spec():
    spec = normalize_voicemail(_alice_voicemail())
    dumped = spec.model_dump()
    assert "transcript" not in dumped
    assert "Call me when you see it." not in repr(dumped)


def test_voicemail_requires_id_and_received_at():
    with pytest.raises(ContentRefError):
        normalize_voicemail({"transcript": "hi", "received_at": "2026-10-20T11:00:00Z"})
    with pytest.raises(ValueError):
        normalize_voicemail({"voicemail_id": "v-1", "transcript": "hi"})


# ---------------------------------------------------------------------------
# register_communication: conservative attribution + idempotency
# ---------------------------------------------------------------------------


def test_register_resolves_resolved_identity_to_existing_person(service):
    person_id = _seed_alice(service)
    norm = CommunicationNormalizer(service)
    out = norm.register_communication("email", _alice_email())
    comm = out["result"]["communication"]
    assert comm.person_id == person_id
    assert comm.channel is CommunicationChannel.EMAIL
    assert comm.content_ref == "email:alice-email-1"
    assert comm.sender == ALICE["email"]
    # Raw content never reaches the stored record.
    assert "Details" not in repr(comm.model_dump())
    assert "Here are the details" not in repr(comm.model_dump())


def test_register_leaves_unresolved_identity_unlinked(service):
    norm = CommunicationNormalizer(service)
    out = norm.register_communication(
        "email",
        {
            "message_id": "e-stranger",
            "thread_id": "t",
            "direction": "RECEIVED",
            "from_address": "stranger@elsewhere.com",
            "to_address": "me@example.com",
            "subject": "hi",
            "timestamp": "2026-10-20T11:00:00Z",
        },
    )
    assert out["result"]["communication"].person_id is None


def test_register_same_name_different_value_stays_separate(service):
    # Two people, same display name, different canonical values -> two persons.
    service.ingest_identity({"kind": "email", "value": "a.n@example.com", "display_name": "Ann"})
    service.ingest_identity({"kind": "email", "value": "b.n@example.com", "display_name": "Ann"})
    norm = CommunicationNormalizer(service)
    a = norm.register_communication(
        "email",
        {"message_id": "a1", "thread_id": "ta", "direction": "RECEIVED", "from_address": "a.n@example.com",
         "to_address": "me@example.com", "subject": "s", "timestamp": "2026-10-20T09:00:00Z"},
    )
    b = norm.register_communication(
        "email",
        {"message_id": "b1", "thread_id": "tb", "direction": "RECEIVED", "from_address": "b.n@example.com",
         "to_address": "me@example.com", "subject": "s", "timestamp": "2026-10-20T09:05:00Z"},
    )
    assert a["result"]["communication"].person_id != b["result"]["communication"].person_id
    assert len(service.list_people()) == 2


def test_register_replay_is_idempotent(service):
    person_id = _seed_alice(service)
    norm = CommunicationNormalizer(service)
    first = norm.register_communication("email", _alice_email())
    assert first["result"]["created"] is True
    second = norm.register_communication("email", _alice_email())
    assert second["result"]["created"] is False
    assert second["result"]["communication"].communication_id == "alice-email-1"
    assert second["result"]["communication"].person_id == person_id
    assert len(service.list_people()) == 1
    assert len(service.list_communications()) == 1


def test_register_unknown_source_raises(service):
    with pytest.raises(ValueError):
        CommunicationNormalizer(service).register_communication("calendar", {})


def test_register_explicit_unknown_person_id_raises(service):
    with pytest.raises(KeyError):
        CommunicationNormalizer(service).register_communication(
            "email", _alice_email(), person_id="ghost"
        )


# ---------------------------------------------------------------------------
# cross_channel_conversation helper (pure fold)
# ---------------------------------------------------------------------------


def _comm(cid, channel, ts, person_id="alice", thread="t"):
    return Communication(
        communication_id=cid,
        person_id=person_id,
        channel=channel,
        direction=CommunicationDirection.INCOMING,
        sender="s",
        timestamp=ts,
        thread_id=thread,
        content_ref=f"{channel.value}:{cid}",
    )


def test_cross_channel_conversation_folds_all_channels():
    comms = [
        _comm("m", CommunicationChannel.IMESSAGE, datetime(2026, 10, 20, 10, 12, tzinfo=UTC), thread="conv-x"),
        _comm("e", CommunicationChannel.EMAIL, datetime(2026, 10, 20, 10, 0, tzinfo=UTC), thread="email-t"),
        _comm("v", CommunicationChannel.VOICEMAIL, datetime(2026, 10, 20, 13, 40, tzinfo=UTC), thread="vm-t"),
    ]
    view = cross_channel_conversation(comms, "Alice")
    assert view is not None
    assert view.person_id == "alice"
    assert view.display_name == "Alice"
    assert view.channels == [CommunicationChannel.EMAIL, CommunicationChannel.IMESSAGE, CommunicationChannel.VOICEMAIL]
    assert set(view.thread_ids) == {"email-t", "conv-x", "vm-t"}
    assert view.communication_count == 3
    # Ordered by timestamp.
    assert view.communication_ids == ["e", "m", "v"]
    assert view.started_at == datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    assert view.last_activity_at == datetime(2026, 10, 20, 13, 40, tzinfo=UTC)


def test_cross_channel_conversation_empty_is_none():
    assert cross_channel_conversation([], "Alice") is None


def test_cross_channel_conversation_dedupes_and_orders_threads():
    comms = [
        _comm("a", CommunicationChannel.EMAIL, datetime(2026, 10, 20, 9, 0, tzinfo=UTC), thread="b"),
        _comm("b", CommunicationChannel.EMAIL, datetime(2026, 10, 20, 9, 5, tzinfo=UTC), thread="a"),
    ]
    view = cross_channel_conversation(comms)
    assert view is not None
    assert view.thread_ids == ["a", "b"]
    assert view.communication_ids == ["a", "b"]


# ---------------------------------------------------------------------------
# §62 Alice cross-channel scenario (AC-R03)
# ---------------------------------------------------------------------------


def test_alice_scenario_one_person_three_comm_one_cross_channel(normalizer, service):
    person_id = _seed_alice(service)
    out = normalize_batch(_alice_batch(), service=service)

    # One person.
    people = service.list_people()
    assert len(people) == 1
    assert people[0].person_id == person_id

    # Three communications, one per channel, all attributed to the person.
    comms = service.list_communications(person_id=person_id)
    assert len(comms) == 3
    assert {c.channel for c in comms} == {
        CommunicationChannel.EMAIL,
        CommunicationChannel.IMESSAGE,
        CommunicationChannel.VOICEMAIL,
    }
    assert {c.communication_id for c in comms} == {"alice-email-1", "alice-msg-1", "alice-vm-1"}
    assert all(c.person_id == person_id for c in comms)
    # No raw content in any stored record.
    for c in comms:
        blob = repr(c.model_dump())
        assert "Sent you the thing." not in blob
        assert "Call me when you see it." not in blob
        assert "Here are the details" not in blob

    # One cross-channel conversation spanning all three channels.
    assert list(out["cross_channel"]) == [person_id]
    view = out["cross_channel"][person_id]
    assert view.communication_count == 3
    assert set(view.channels) == {
        CommunicationChannel.EMAIL,
        CommunicationChannel.IMESSAGE,
        CommunicationChannel.VOICEMAIL,
    }
    assert set(view.thread_ids) == {
        ALICE["email_thread"],
        ALICE["message_conversation"],
        ALICE["voicemail_thread"],
    }
    assert set(view.communication_ids) == {"alice-email-1", "alice-msg-1", "alice-vm-1"}
    assert view.display_name == "Alice"
    assert view.started_at == datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    assert view.last_activity_at == datetime(2026, 10, 20, 13, 40, tzinfo=UTC)

    # R101 per-channel fold remains intact alongside the cross-channel view.
    per_channel = {c.channel: c for c in service.list_conversations() if c.person_id == person_id}
    assert len(per_channel) == 3
    assert per_channel[CommunicationChannel.EMAIL].communication_ids == ["alice-email-1"]
    assert per_channel[CommunicationChannel.IMESSAGE].communication_ids == ["alice-msg-1"]
    assert per_channel[CommunicationChannel.VOICEMAIL].communication_ids == ["alice-vm-1"]


def test_alice_scenario_batch_is_idempotent_on_replay(service):
    person_id = _seed_alice(service)
    first = normalize_batch(_alice_batch(), service=service)
    second = normalize_batch(_alice_batch(), service=service)
    assert [r["result"]["created"] for r in first["results"]] == [True, True, True]
    assert [r["result"]["created"] for r in second["results"]] == [False, False, False]
    # No growth on replay.
    assert len(service.list_people()) == 1
    assert len(service.list_communications()) == 3
    assert len(second["cross_channel"][person_id].communication_ids) == 3


def test_unresolved_identity_yields_no_cross_channel_view(normalizer, service):
    out = normalize_batch(
        [("email", _alice_email())],
        service=service,
    )
    # No identity seeded: attribution is conservative -> unlinked.
    assert out["results"][0]["result"]["communication"].person_id is None
    assert out["cross_channel"] == {}


def test_alice_scenario_callback_attention_is_an_r103_blocker(normalizer, service):
    """§62 expects a ``CALL ALICE`` attention item, reason "direct callback
    request". The ``AttentionItem`` model has no structured callback/request
    metadata field and ``backend/api/models.py`` is not an R102-owned file, so
    wiring it here requires speculative coupling — reported as an unresolved
    AC, attention left unchanged. The raw material (voicemail transcript +
    deterministic callback_reason) is available and the normalized voicemail
    carries only the content_ref, so R103 detection can act on it later."""
    person_id = _seed_alice(service)
    out = normalize_batch(_alice_batch(), service=service)
    vm = out["results"][2]["result"]["communication"]
    assert vm.channel is CommunicationChannel.VOICEMAIL
    assert vm.content_ref == "voicemail:alice-vm-1"
    # The deterministic callback signal exists on the source payload ...
    assert _alice_voicemail()["needs_callback"] is True
    assert _alice_voicemail()["callback_reason"] == "call me"
    # ... but it is NOT stored in the normalized record (no plaintext).
    assert "callback_reason" not in repr(vm.model_dump())
    # Attention service remains empty/unchanged by normalization.
    assert out["cross_channel"][person_id].communication_count == 3
