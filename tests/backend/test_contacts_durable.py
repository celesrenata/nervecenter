"""Mac Contacts -> people, Messages names, Contacts screen, photos (NS-ADR-097)."""
from __future__ import annotations

import base64
import pathlib
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import backend.api.app  # noqa: F401 - import-order guard
from backend.attention.service import AttentionService
from backend.contacts.durable import (
    apply_contacts,
    hydrate_views,
    normalize_phone,
)
from backend.contacts.service import ContactsService
from backend.messages.service import MessagesService
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import PersonIdentityRow, PersonRow
from backend.persistence.repositories import RepositoryRegistry
from backend.storage.memory import InMemoryObjectStore

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
TABLES = [PersonRow.__table__, PersonIdentityRow.__table__]


@pytest.fixture
async def registry(tmp_path: pathlib.Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(PersonRow.__table__.metadata.create_all, tables=TABLES)
    reg = RepositoryRegistry(make_session_factory(engine))
    async with reg.session_factory() as s:
        s.add(PersonRow(id="p-text", display_name="+15551234567"))
        s.add(PersonIdentityRow(person_id="p-text", channel="imessage", handle="+15551234567"))
        s.add(PersonRow(id="p-mail", display_name="Robert from Mail"))
        s.add(PersonIdentityRow(person_id="p-mail", channel="email", handle="Bob@Example.com"))
        await s.commit()
    yield reg
    await engine.dispose()


def _contact(uid: str, name: str, phones: list[str] = (), emails: list[str] = (),
             photo: bytes | None = None, ptype: str = "image/jpeg") -> dict[str, Any]:
    return {
        "uid": uid, "display_name": name,
        "phones": [{"number": n, "label": "Mobile"} for n in phones],
        "emails": [{"address": e, "label": None} for e in emails],
        "photo_b64": base64.b64encode(photo).decode() if photo else None,
        "photo_type": ptype if photo else None,
    }


async def _people(reg: RepositoryRegistry) -> dict[str, tuple[str, set[str]]]:
    async with reg.session_factory() as s:
        people = {p.id: p.display_name for p in (await s.execute(select(PersonRow))).scalars()}
        idents: dict[str, set[str]] = {}
        for i in (await s.execute(select(PersonIdentityRow))).scalars():
            idents.setdefault(i.person_id, set()).add(i.handle)
    return {pid: (name, idents.get(pid, set())) for pid, name in people.items()}


@pytest.mark.parametrize(("raw", "want"), [
    ("(555) 123-4567", "+15551234567"),
    ("1-555-123-4567", "+15551234567"),
    ("+44 20 7946 0958", "+442079460958"),
    ("", None),
])
def test_normalize_phone(raw: str, want: str | None) -> None:
    assert normalize_phone(raw) == want


async def test_merge_matches_renames_only_handles_and_is_idempotent(registry) -> None:
    store = InMemoryObjectStore()
    batch = [
        _contact("U1", "Ada Lovelace", phones=["(555) 123-4567"], emails=["ada@example.com"],
                 photo=JPEG),
        _contact("U2", "Bob Smith", emails=["bob@example.com"]),
        _contact("U3", "New Person", phones=["555-000-1111"]),
        _contact("U4", "Bad Photo", phones=["555-000-2222"], photo=b"GIF89a", ptype="image/gif"),
        _contact("U5", "No Handles"),
    ]
    counts = await apply_contacts(registry, store, batch)
    assert counts == {"created": 2, "matched": 2, "renamed": 1, "identities": 3,
                      "photos": 1, "skipped": 1}
    people = await _people(registry)
    assert people["p-text"] == ("Ada Lovelace", {"+15551234567", "ada@example.com"})
    assert people["p-mail"][0] == "Robert from Mail"  # a real name is kept
    assert people["p-mail"][1] == {"Bob@Example.com"}  # same address, no duplicate
    assert any(n == "New Person" and h == {"+15550001111"} for n, h in people.values())

    again = await apply_contacts(registry, store, batch)
    assert again["created"] == 0 and again["identities"] == 0 and again["renamed"] == 0
    assert len(await _people(registry)) == 4
    # A later rename in Contacts follows a name Contacts set, never another one.
    await apply_contacts(registry, store, [
        _contact("U1", "Ada King", phones=["(555) 123-4567"]),
        _contact("U2", "Bobby", emails=["bob@example.com"]),
    ])
    people = await _people(registry)
    assert people["p-text"][0] == "Ada King"
    assert people["p-mail"][0] == "Robert from Mail"


async def test_hydrate_names_messages_and_fills_contacts_screen(registry) -> None:
    await apply_contacts(registry, InMemoryObjectStore(), [
        _contact("U1", "Ada Lovelace", phones=["(555) 123-4567"]),
    ])
    messages = MessagesService(attention=AttentionService())
    messages.ingest({"message_id": "m1", "channel": "imessage", "direction": "INBOUND",
                     "peer_identifier": "+15551234567", "body": "hi",
                     "timestamp": datetime(2026, 11, 14, tzinfo=UTC)}, generate_attention=False)
    contacts = ContactsService()
    counts = await hydrate_views(registry, messages, contacts)
    assert counts["people"] == 1
    assert [c.peer_name for c in messages.list_conversations()] == ["Ada Lovelace"]
    assert [(c.display_name, c.phones) for c in contacts.list_contacts()] == [
        ("Ada Lovelace", ["+15551234567"])]


class _Plane:
    registry_configured = True

    async def relay_verify_token(self, *, device_id: str, token: str) -> bool:
        return device_id == "dev-1" and token == "good"


async def test_relay_route_requires_token_and_photo_route_serves(registry, monkeypatch) -> None:
    from backend.api.endpoints import contact_photos, relay
    from backend.ingest import supervisor

    store = InMemoryObjectStore()
    monkeypatch.setattr(supervisor, "_object_store_from_env", lambda: store)
    app = FastAPI()
    app.include_router(relay.router)
    app.include_router(contact_photos.router)
    app.state.connection_control_plane = _Plane()
    app.state.repository_registry = registry
    app.state.messages_service = MessagesService(attention=AttentionService())
    app.state.contacts_service = ContactsService()
    body = {"device_id": "dev-1",
            "contacts": [_contact("U1", "Ada Lovelace", phones=["(555) 123-4567"], photo=JPEG)]}
    with TestClient(app) as client:
        denied = client.post("/api/relay/contacts", json=body,
                             headers={"Authorization": "Bearer wrong"})
        assert denied.status_code == 401
        assert (await _people(registry))["p-text"][0] == "+15551234567"  # untouched

        ok = client.post("/api/relay/contacts", json=body,
                         headers={"Authorization": "Bearer good"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["matched"] == 1 and ok.json()["photos"] == 1
        assert "Ada" not in ok.text  # counts only

        by_handle = client.get("/api/contact-photos", params={"handle": "+15551234567"})
        assert by_handle.status_code == 200
        assert by_handle.content == JPEG
        assert by_handle.headers["content-type"] == "image/jpeg"
        assert client.get("/api/contact-photos/p-text").content == JPEG
        assert client.get("/api/contact-photos/p-mail").status_code == 404
        missing = client.get("/api/contact-photos", params={"handle": "+19999999999"})
        assert missing.status_code == 404
