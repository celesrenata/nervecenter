"""The relay -> live Messages mirror (regression for a live failure, 2026-11-14).

The relay sends lowercase ``inbound``/``outbound``; the live plane's
``MessageDirection`` is upper case. Every mirror raised
"'outbound' is not a valid MessageDirection", was swallowed as a warning, and
the Messages screen never showed relay messages.
"""
from __future__ import annotations

import pytest

from backend.api.models import MessageDirection
from backend.messages.ingest_bridge import MessagesIngestBridge, _live_direction
from backend.messages.service import MessagesService


def _wire(message_id: str, direction: str | None, body: str = "test") -> dict:
    msg = {"message_id": message_id, "channel": "imessage", "peer_identifier": "+15550001111",
           "peer_name": None, "body": body, "received_at": "2026-11-14T14:27:29Z",
           "source": "personal-relay"}
    if direction is not None:
        msg["direction"] = direction
    return msg


@pytest.mark.parametrize(
    ("wire", "live"),
    [("inbound", "INBOUND"), ("outbound", "OUTBOUND"), ("INBOUND", "INBOUND"),
     ("OUTBOUND", "OUTBOUND"), ("incoming", "INBOUND"), ("outgoing", "OUTBOUND"),
     (None, "INBOUND"), ("", "INBOUND")],
)
def test_live_direction_mapping(wire, live) -> None:
    assert _live_direction(wire) == live


def test_relay_messages_reach_the_live_messages_list() -> None:
    svc = MessagesService()
    bridge = MessagesIngestBridge(service=svc)
    out = bridge.mirror_message(_wire("g-out", "outbound"))
    inc = bridge.mirror_message(_wire("g-in", "inbound"))
    assert out is not None and inc is not None, "mirror must not fail on relay directions"
    assert out["message"].direction is MessageDirection.OUTBOUND
    assert inc["message"].direction is MessageDirection.INBOUND
    convs = svc.list_conversations()
    assert len(convs) == 1 and convs[0].message_count == 2


def test_replayed_relay_message_is_a_duplicate_not_an_error() -> None:
    bridge = MessagesIngestBridge(service=MessagesService())
    assert bridge.mirror_message(_wire("g1", "inbound"))["duplicate"] is False
    assert bridge.mirror_message(_wire("g1", "inbound"))["duplicate"] is True
