"""Startup rehydration of the in-memory Messages plane from durable relay rows."""
from __future__ import annotations

from pathlib import Path

import pytest
from test_connectors_registry import _engine, _pair_and_register

from backend.connectors.registry import ConnectionControlPlane
from backend.connectors.vault import FernetVault, generate_key
from backend.messages.rehydrate import rehydrate_messages
from backend.messages.service import MessagesService
from backend.persistence.connection import make_session_factory
from backend.persistence.repositories import RepositoryRegistry
from backend.storage.memory import InMemoryObjectStore


class _Attention:
    def __init__(self) -> None:
        self.created = 0

    def create(self, payload):  # pragma: no cover - must never be called
        self.created += 1
        raise AssertionError("rehydration must not create attention items")

    def get(self, _id):
        return None

    def refresh(self, *_a, **_k):  # pragma: no cover
        raise AssertionError("rehydration must not refresh attention items")


@pytest.fixture
async def plane(tmp_path: Path):
    engine = await _engine(tmp_path / "r.db")
    try:
        vault = FernetVault(generate_key())
        registry = RepositoryRegistry(make_session_factory(engine), secret_codec=vault)
        store = InMemoryObjectStore()
        yield ConnectionControlPlane(registry, vault=vault, object_store=store), registry, store
    finally:
        await engine.dispose()


async def test_restart_restores_relay_messages_to_the_messages_screen(plane) -> None:
    cp, registry, store = plane
    pair, _ = await _pair_and_register(cp)
    await cp.relay_ingest_batch(device_id=pair["device_id"], messages=[
        {"message_id": "g-out", "direction": "outbound", "peer_identifier": "+15550001111",
         "body": "test", "received_at": "2026-11-14T14:27:29Z"},
        {"message_id": "g-in", "direction": "inbound", "peer_identifier": "+15550001111",
         "body": "test", "received_at": "2026-11-14T14:27:30Z"},
    ])
    fresh = MessagesService()  # what an API restart leaves behind: empty
    assert fresh.list_conversations() == []
    counts = await rehydrate_messages(registry, store, fresh)
    assert counts == {"restored": 2, "no_body": 0, "failed": 0}
    (conv,) = fresh.list_conversations()
    assert conv.peer_identifier == "+15550001111" and conv.message_count == 2
    assert conv.last_body == "test"


async def test_rows_without_a_durable_body_are_skipped(plane, tmp_path) -> None:
    cp, registry, store = plane
    legacy = ConnectionControlPlane(registry, vault=cp._vault, object_store=None)
    pair, _ = await _pair_and_register(legacy)
    await legacy.relay_ingest_batch(device_id=pair["device_id"], messages=[
        {"message_id": "old", "peer_identifier": "+1", "body": "gone"}])
    fresh = MessagesService()
    counts = await rehydrate_messages(registry, store, fresh)
    assert counts["no_body"] == 1 and counts["restored"] == 0


async def test_rehydration_never_raises_attention(plane) -> None:
    cp, registry, store = plane
    pair, _ = await _pair_and_register(cp)
    await cp.relay_ingest_batch(device_id=pair["device_id"], messages=[
        {"message_id": "a", "direction": "inbound", "peer_identifier": "+1",
         "body": "can you reply?"}])
    attention = _Attention()
    fresh = MessagesService(attention=attention)
    assert (await rehydrate_messages(registry, store, fresh))["restored"] == 1
    assert attention.created == 0


async def test_a_second_rehydration_is_idempotent(plane) -> None:
    cp, registry, store = plane
    pair, _ = await _pair_and_register(cp)
    await cp.relay_ingest_batch(device_id=pair["device_id"], messages=[
        {"message_id": "a", "peer_identifier": "+1", "body": "x"}])
    svc = MessagesService()
    await rehydrate_messages(registry, store, svc)
    await rehydrate_messages(registry, store, svc)
    (conv,) = svc.list_conversations()
    assert conv.message_count == 1


async def test_missing_pieces_are_a_no_op() -> None:
    assert await rehydrate_messages(None, None, None) == {"restored": 0, "no_body": 0, "failed": 0}
