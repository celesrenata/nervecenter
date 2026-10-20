"""Gaps4 R101 — unified communications plane tests (AC-R01/AC-R02).

Covers the canonical ``Person`` → ``ContactIdentity`` → ``Conversation`` →
``Communication`` graph (prompt2 §5–§7, §32):

- **AC-R01 (model/shape):** the §7 ``Communication`` shape with allowed
  channels, §36 sensitivity labels, a non-empty ``content_ref`` pointer
  (raw content is *never* stored), default ``LOCAL_ONLY`` routing, and the
  per-(person, channel) ``Conversation`` fold.
- **AC-R02 (conservative identity resolution):** an equal canonical value
  resolves idempotently to the *same* person (no duplicate), while a
  same-display-name / different-value result is a separate person flagged
  ``PROPOSED`` and is *never* joined implicitly.
- **Explicit merge:** the only path that joins two persons — identities,
  communications, and per-channel conversations fold into the survivor; the
  source person is removed; VIP state is preserved.
- **JSON-safe endpoints:** 201/200 codes, honest 404s, the ``meta``
  envelope, and no raw content/credentials leaking into serialized
  identity or communication records.

The communications service is an in-process singleton (no engine, no DB),
so tests build a fresh app per test via :func:`backend.api.app.create_app`
and fresh singletons via the reset hook — no database or NATS required.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.api.app import create_app
from backend.communications import (
    DEFAULT_CONTENT_ROUTING,
    Communication,
    CommunicationChannel,
    ContactIdentity,
    ContactIdentityKind,
    Conversation,
    IdentityMatchStatus,
    Person,
    SensitivityLabel,
    get_communications_service,
    normalize_identity_value,
    reset_communications_service,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    reset_communications_service()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_communications_service()


def _ingest_identity(client: TestClient, **over) -> dict:
    payload = {
        "kind": "email",
        "value": "alice.n@example.com",
        "display_name": "Alice Nguyen",
    }
    payload.update(over)
    res = client.post("/api/people/identities/ingest", json=payload)
    assert res.status_code in (200, 201), res.text
    return res.json()


def _ingest_communication(client: TestClient, **over) -> dict:
    payload = {
        "channel": "email",
        "direction": "incoming",
        "sender": "alice.n@example.com",
        "content_ref": "enc://comm/alice-1",
    }
    payload.update(over)
    res = client.post("/api/communications/ingest", json=payload)
    assert res.status_code in (200, 201), res.text
    return res.json()


def _person_ids(client: TestClient) -> list[str]:
    res = client.get("/api/people")
    assert res.status_code == 200
    return [p["person_id"] for p in res.json()["data"]]


# ---------------------------------------------------------------------------
# normalize_identity_value (value-first canonical form, §6)
# ---------------------------------------------------------------------------


def test_normalize_email_case_folds_and_strips_url_fragment():
    # A URL-ish query token is never stored in an identity record (§6).
    assert (
        normalize_identity_value(ContactIdentityKind.EMAIL, "Alice.N@Example.com?token=abc")
        == "alice.n@example.com"
    )


def test_normalize_phone_keeps_e164_prefix():
    assert normalize_identity_value(ContactIdentityKind.PHONE, "+1 415-555-0134") == "+14155550134"
    # ``00`` international prefix folds to ``+``.
    assert normalize_identity_value(ContactIdentityKind.PHONE, "0014155550134") == "+14155550134"


def test_normalize_contact_collapses_whitespace():
    assert (
        normalize_identity_value(ContactIdentityKind.CONTACT, "  Alice   Nguyen  ")
        == "alice nguyen"
    )


def test_normalize_empty_value_raises():
    with pytest.raises(ValueError):
        normalize_identity_value(ContactIdentityKind.EMAIL, "   ")


# ---------------------------------------------------------------------------
# model validation (AC-R01 — §7 shape, §36 labels)
# ---------------------------------------------------------------------------


def test_channel_enum_allowed_values():
    assert {c.value for c in CommunicationChannel} == {
        "email",
        "imessage",
        "sms",
        "rcs",
        "voicemail",
    }


def test_sensitivity_label_allowed_values():
    assert {s.value for s in SensitivityLabel} == {
        "PERSONAL",
        "PRIVATE",
        "FINANCIAL",
        "WORK",
        "PROJECT",
        "LOW-SENSITIVITY",
    }
    assert DEFAULT_CONTENT_ROUTING == "LOCAL_ONLY"


def test_communication_defaults():
    c = Communication(
        communication_id="comm-1",
        channel=CommunicationChannel.EMAIL,
        direction="incoming",
        sender="alice@example.com",
        timestamp="2026-01-01T00:00:00Z",
        content_ref="enc://comm/1",
    )
    assert c.sensitivity is SensitivityLabel.PERSONAL
    assert c.content_routing == DEFAULT_CONTENT_ROUTING
    assert c.recipients == []
    assert c.attachments == []


def test_communication_rejects_empty_content_ref():
    with pytest.raises(ValidationError):
        Communication(
            communication_id="comm-2",
            channel=CommunicationChannel.EMAIL,
            direction="incoming",
            sender="alice@example.com",
            timestamp="2026-01-01T00:00:00Z",
            content_ref="   ",
        )


def test_communication_rejects_unknown_channel():
    with pytest.raises(ValidationError):
        Communication(
            communication_id="comm-3",
            channel="telegram",
            direction="incoming",
            sender="alice@example.com",
            timestamp="2026-01-01T00:00:00Z",
            content_ref="enc://comm/3",
        )


def test_contact_identity_requires_value():
    # ``value`` is mandatory — an identity must carry a value to be a link.
    assert ContactIdentity.model_fields["value"].is_required()


# ---------------------------------------------------------------------------
# idempotent identity ingest (AC-R01 — no duplicate)
# ---------------------------------------------------------------------------


def test_reingest_same_value_is_idempotent(client):
    first = _ingest_identity(client)
    person_id = first["person"]["person_id"]
    assert first["created"] is True
    assert first["status"] == "UNLINKED"

    # Re-ingest the same canonical value (different case) → same person, no
    # duplicate identity, created=False (AC-R01).
    second = _ingest_identity(client, value="ALICE.N@example.com")
    assert second["created"] is False
    assert second["status"] == "RESOLVED"
    assert second["person"]["person_id"] == person_id

    people = _person_ids(client)
    assert people.count(person_id) == 1
    person = second["person"]
    values = [i["value"] for i in person["identities"]]
    assert values.count("alice.n@example.com") == 1


def test_reingest_same_value_adds_no_duplicate_identity(client):
    _ingest_identity(client)
    _ingest_identity(client, value="alice.n@example.com")
    person = _person_ids(client)
    detail = client.get(f"/api/people/{person[0]}").json()
    assert len(detail["item"]["identities"]) == 1


# ---------------------------------------------------------------------------
# conservative resolution (AC-R02 — same name, different value → PROPOSED)
# ---------------------------------------------------------------------------


def test_same_name_different_value_is_proposed_not_merged(client):
    first = _ingest_identity(client, value="alice.n@example.com")
    person_a = first["person"]["person_id"]

    # Same display name, a *different* canonical value → separate person,
    # flagged PROPOSED, never auto-merged (AC-R02).
    second = _ingest_identity(client, value="alice.other@example.com")
    person_b = second["person"]["person_id"]

    assert second["created"] is True
    assert second["status"] == "PROPOSED"
    assert person_b != person_a
    # The uncertain candidate is cross-linked as a proposed merge.
    assert person_a in second["proposed_candidates"]
    # Two distinct persons remain — no implicit merge happened.
    assert set(_person_ids(client)) == {person_a, person_b}

    # The *original* person is also cross-linked to the proposed candidate.
    detail_a = client.get(f"/api/people/{person_a}").json()
    assert person_b in detail_a["item"]["proposed_merges"]


def test_unknown_value_is_unlinked(client):
    res = _ingest_identity(client, value="brand-new@example.com")
    assert res["status"] == "UNLINKED"
    assert res["proposed_candidates"] == []


def test_explicit_person_id_resolves_resolved(client):
    first = _ingest_identity(client, value="alice.n@example.com")
    person_id = first["person"]["person_id"]
    # Pinning the same person explicitly is a RESOLVED upsert, no new person.
    res = _ingest_identity(client, value="alice-phone-1", kind="phone", person_id=person_id)
    assert res["status"] == "RESOLVED"
    assert res["created"] is False
    assert len(_person_ids(client)) == 1


# ---------------------------------------------------------------------------
# explicit merge (AC-R02 — the only way to join two persons)
# ---------------------------------------------------------------------------


def test_explicit_merge_folds_people_communications_and_conversations(client):
    # Two same-name persons, each with their own email conversation.
    alice = _ingest_identity(client, value="alice.n@example.com")
    person_a = alice["person"]["person_id"]
    alice2 = _ingest_identity(client, value="alice.other@example.com")
    person_b = alice2["person"]["person_id"]
    assert person_a != person_b

    _ingest_communication(client, person_id=person_a, content_ref="enc://comm/a1")
    _ingest_communication(client, person_id=person_b, content_ref="enc://comm/b1")

    # Mark person_b VIP so we can assert VIP is preserved on the survivor.
    vip = client.post(f"/api/people/{person_b}/vip", json={"is_vip": True})
    assert vip.status_code == 200

    # Merge person_b into person_a.
    res = client.post(f"/api/people/{person_a}/merge", json={"merge_person_id": person_b})
    assert res.status_code == 200
    body = res.json()
    assert body["merged_from"] == person_b
    assert body["remaining_persons"] == 1
    assert body["item"]["person_id"] == person_a
    assert body["item"]["is_vip"] is True  # VIP preserved on the survivor

    # The source person is gone.
    assert _person_ids(client) == [person_a]
    assert client.get(f"/api/people/{person_b}").status_code == 404

    # Identities from both persons are folded onto the survivor.
    survivor = client.get(f"/api/people/{person_a}").json()["item"]
    survivor_values = {i["value"] for i in survivor["identities"]}
    assert "alice.n@example.com" in survivor_values
    assert "alice.other@example.com" in survivor_values

    # Both communications now point at the survivor.
    comms = client.get("/api/communications", params={"person_id": person_a}).json()["data"]
    assert {c["content_ref"] for c in comms} == {"enc://comm/a1", "enc://comm/b1"}
    assert all(c["person_id"] == person_a for c in comms)


def test_explicit_merge_dedupes_shared_identity_values(client):
    a = _ingest_identity(client, value="alice.n@example.com")
    person_a = a["person"]["person_id"]
    # An explicit re-pin on a *second* person is not possible via PROPOSED;
    # instead, build a second person by same-name / different-value, then give
    # it the same email value explicitly so the merge must dedupe it.
    b = _ingest_identity(client, value="alice.other@example.com")
    person_b = b["person"]["person_id"]
    # Link the first person's email onto person_b explicitly.
    res = _ingest_identity(
        client, value="alice.n@example.com", person_id=person_b
    )
    assert res["status"] == "RESOLVED"
    # Now person_b carries both values; merge should dedupe the shared value.
    client.post(f"/api/people/{person_a}/merge", json={"merge_person_id": person_b})
    survivor = client.get(f"/api/people/{person_a}").json()["item"]
    values = [i["value"] for i in survivor["identities"]]
    assert values.count("alice.n@example.com") == 1
    assert "alice.other@example.com" in values


def test_merge_self_is_rejected_422(client):
    alice = _ingest_identity(client)
    person_id = alice["person"]["person_id"]
    res = client.post(f"/api/people/{person_id}/merge", json={"merge_person_id": person_id})
    assert res.status_code == 422


def test_merge_unknown_target_is_404(client):
    alice = _ingest_identity(client)
    person_id = alice["person"]["person_id"]
    res = client.post(
        f"/api/people/{person_id}/merge", json={"merge_person_id": "ghost-person"}
    )
    assert res.status_code == 404


def test_merge_unknown_source_is_404(client):
    alice = _ingest_identity(client)
    person_id = alice["person"]["person_id"]
    res = client.post(
        f"/api/people/ghost-person/merge", json={"merge_person_id": person_id}
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# communication ingest + conversation fold (AC-R01 §7 / §32)
# ---------------------------------------------------------------------------


def test_communication_ingest_creates_and_folds_conversation(client):
    alice = _ingest_identity(client)
    person_id = alice["person"]["person_id"]
    res = client.post(
        "/api/communications/ingest",
        json={
            "person_id": person_id,
            "channel": "email",
            "direction": "incoming",
            "sender": "alice.n@example.com",
            "content_ref": "enc://comm/1",
        },
    )
    assert res.status_code == 201
    res = res.json()
    item = res["item"]
    assert item["person_id"] == person_id
    assert item["content_ref"] == "enc://comm/1"
    # Folded into a per-(person, channel) conversation.
    conv = res["conversation"]
    assert conv["person_id"] == person_id
    assert conv["channel"] == "email"
    assert conv["communication_count"] == 1

    # The conversation is listed and returns its communications in order.
    convs = client.get("/api/conversations").json()["data"]
    assert any(c["conversation_id"] == conv["conversation_id"] for c in convs)
    detail = client.get(f"/api/conversations/{conv['conversation_id']}").json()
    assert detail["item"]["conversation_id"] == conv["conversation_id"]
    assert [c["communication_id"] for c in detail["communications"]] == [
        item["communication_id"]
    ]


def test_communication_reingest_same_id_is_200(client):
    alice = _ingest_identity(client)
    person_id = alice["person"]["person_id"]
    first = _ingest_communication(
        client, person_id=person_id, content_ref="enc://comm/1", communication_id="comm-X"
    )
    assert first["status_code" if False else "item"]["communication_id"] == "comm-X"
    # Re-ingest the same id → idempotent update, 200 (not 201).
    res = client.post(
        "/api/communications/ingest",
        json={
            "person_id": person_id,
            "channel": "email",
            "direction": "incoming",
            "sender": "alice.n@example.com",
            "content_ref": "enc://comm/1",
            "communication_id": "comm-X",
        },
    )
    assert res.status_code == 200
    assert res.json()["item"]["communication_id"] == "comm-X"


def test_communication_unknown_person_id_is_404(client):
    res = client.post(
        "/api/communications/ingest",
        json={
            "person_id": "ghost-person",
            "channel": "email",
            "direction": "incoming",
            "sender": "alice.n@example.com",
            "content_ref": "enc://comm/1",
        },
    )
    assert res.status_code == 404


def test_communication_unknown_channel_is_422(client):
    res = client.post(
        "/api/communications/ingest",
        json={
            "channel": "telegram",
            "direction": "incoming",
            "sender": "alice.n@example.com",
            "content_ref": "enc://comm/1",
        },
    )
    assert res.status_code == 422


def test_communication_unknown_sensitivity_is_422(client):
    res = client.post(
        "/api/communications/ingest",
        json={
            "channel": "email",
            "direction": "incoming",
            "sender": "alice.n@example.com",
            "content_ref": "enc://comm/1",
            "sensitivity": "ULTRA",
        },
    )
    assert res.status_code == 422


def test_communication_omitted_person_resolves_by_value(client):
    alice = _ingest_identity(client)
    person_id = alice["person"]["person_id"]
    # No person_id: the sender value resolves against the identity index.
    res = _ingest_communication(client, sender="alice.n@example.com", content_ref="enc://auto")
    assert res["item"]["person_id"] == person_id


def test_communication_uncertain_counterpart_stays_unlinked(client):
    alice = _ingest_identity(client)
    # A sender value nobody has linked leaves the record unlinked (person_id
    # null) rather than guessing (§6 conservative resolution).
    res = _ingest_communication(client, sender="stranger@example.com", content_ref="enc://unk")
    assert res["item"]["person_id"] is None


def test_list_communications_filters(client):
    a = _ingest_identity(client, value="a@example.com")
    b = _ingest_identity(client, value="b@example.com")
    pa = a["person"]["person_id"]
    pb = b["person"]["person_id"]
    _ingest_communication(client, person_id=pa, channel="email", content_ref="enc://a")
    _ingest_communication(client, person_id=pb, channel="sms", content_ref="enc://b")

    by_person = client.get("/api/communications", params={"person_id": pa}).json()["data"]
    assert [c["content_ref"] for c in by_person] == ["enc://a"]

    by_channel = client.get("/api/communications", params={"channel": "sms"}).json()["data"]
    assert [c["content_ref"] for c in by_channel] == ["enc://b"]


def test_get_communication_unknown_is_404(client):
    assert client.get("/api/communications/comm-ghost").status_code == 404


def test_get_conversation_unknown_is_404(client):
    assert client.get("/api/conversations/conv-ghost").status_code == 404


# ---------------------------------------------------------------------------
# envelope shape + JSON-safety (no raw content leakage)
# ---------------------------------------------------------------------------


def test_list_people_envelope_has_meta(client):
    _ingest_identity(client)
    body = client.get("/api/people").json()
    assert "data" in body
    assert "meta" in body
    assert body["meta"]["pagination"]["total"] == 1
    assert body["meta"]["request_id"]
    assert "version" in body["meta"]


def test_ingest_identity_envelope_has_meta(client):
    body = _ingest_identity(client)
    assert "meta" in body
    assert body["meta"]["request_id"]
    assert "person" in body
    assert "identity" in body
    assert "status" in body


def test_identity_records_are_json_safe(client):
    # A token-laden email value must not survive normalization; the stored
    # identity carries only the normalized value, trimmed raw, and
    # provenance — never raw content or credentials (§6).
    body = _ingest_identity(client, value="Alice.N@example.com?token=SECRET123")
    ident = body["identity"]
    assert ident["value"] == "alice.n@example.com"
    # No raw content / credential fields exist on the identity record.
    assert set(ident.keys()) == {
        "kind",
        "value",
        "raw_value",
        "display_name",
        "source",
        "first_seen_at",
        "last_seen_at",
    }
    # The secret fragment never appears anywhere in the serialized record.
    serialized = client.get(f"/api/people/{body['person']['person_id']}").text
    assert "SECRET123" not in serialized
    assert "token=" not in serialized


def test_communication_records_carry_only_content_ref(client):
    body = _ingest_communication(client, content_ref="enc://comm/safety")
    item = body["item"]
    # The record is a pointer, not a payload: no raw content key exists.
    assert item["content_ref"] == "enc://comm/safety"
    assert "content" not in item
    assert "body" not in item
    assert item["content_routing"] == DEFAULT_CONTENT_ROUTING


def test_unknown_person_is_404(client):
    assert client.get("/api/people/no-such-person").status_code == 404


def test_ingest_identity_empty_value_is_422(client):
    res = client.post(
        "/api/people/identities/ingest",
        json={"kind": "email", "value": "   "},
    )
    assert res.status_code == 422


def test_ingest_identity_unknown_kind_is_422(client):
    res = client.post(
        "/api/people/identities/ingest",
        json={"kind": "twitter", "value": "alice"},
    )
    assert res.status_code == 422


def test_vip_toggle_on_and_off(client):
    alice = _ingest_identity(client)
    person_id = alice["person"]["person_id"]
    on = client.post(f"/api/people/{person_id}/vip", json={"is_vip": True})
    assert on.status_code == 200
    assert on.json()["item"]["is_vip"] is True
    off = client.post(f"/api/people/{person_id}/vip", json={"is_vip": False})
    assert off.status_code == 200
    assert off.json()["item"]["is_vip"] is False


def test_vip_unknown_person_is_404(client):
    assert client.post("/api/people/ghost/vip", json={"is_vip": True}).status_code == 404


# ---------------------------------------------------------------------------
# service-level resolution + reset hook
# ---------------------------------------------------------------------------


def test_service_resolve_identity_statuses():
    reset_communications_service()
    svc = get_communications_service()
    try:
        svc.ingest_identity({"kind": "email", "value": "alice@example.com", "display_name": "Alice"})
        # Equal canonical value → RESOLVED.
        person, status, proposed = svc.resolve_identity("email", "ALICE@example.com")
        assert status is IdentityMatchStatus.RESOLVED
        assert proposed == []
        # Same display name, different canonical value → PROPOSED, not
        # joined: the incoming value string matches the existing display
        # name, but its canonical value is a fresh, unlinked value.
        _, status2, proposed2 = svc.resolve_identity("contact", "Alice")
        assert status2 is IdentityMatchStatus.PROPOSED
        assert len(proposed2) == 1
        # Unknown value → UNLINKED.
        _, status3, proposed3 = svc.resolve_identity("email", "nobody@example.com")
        assert status3 is IdentityMatchStatus.UNLINKED
        assert proposed3 == []
        assert person is not None
    finally:
        reset_communications_service()


def test_service_reset_hook():
    reset_communications_service()
    svc = get_communications_service()
    svc.ingest_identity({"kind": "email", "value": "x@example.com"})
    assert len(svc.list_people()) == 1
    reset_communications_service()
    # A fresh singleton after reset is empty.
    assert len(get_communications_service().list_people()) == 0


def test_merge_people_service_error_paths():
    reset_communications_service()
    svc = get_communications_service()
    try:
        a = svc.ingest_identity({"kind": "email", "value": "a@example.com"})
        pa = a["person"].person_id
        with pytest.raises(KeyError):
            svc.merge_people(pa, "ghost")
        with pytest.raises(ValueError):
            svc.merge_people(pa, pa)
    finally:
        reset_communications_service()
