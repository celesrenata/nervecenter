"""POST /api/context/ask/stream: SSE events, guard, unavailable, calendar proposal."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_ask_calendar_create import SENTENCE, World, world  # noqa: F401 - fixture import

from backend.context.ask import AskService
from backend.inference.omniroute import OmniRouteTransport
from backend.inference.secure_client import SecureTransport


def read_events(response: Any) -> list[tuple[str, dict[str, Any]]]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    events = []
    for block in response.text.strip().split("\n\n"):
        name, data = block.split("\n", 1)
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


def stream(world: World, question: str, **extra: Any) -> list[tuple[str, dict[str, Any]]]:
    return read_events(
        world.client.post("/api/context/ask/stream", json={"question": question, **extra})
    )


def fixture_service(tmp_path: Path, content: str | None = "Streamed fixture answer") -> AskService:
    if content is not None:
        (tmp_path / "nerve-ask.json").write_text(json.dumps({"content": content}), encoding="utf-8")
    return AskService(
        transport=OmniRouteTransport(fixture_dir=str(tmp_path), api_key="k"),
        remote_provider_allowed=True,
    )


async def test_events_arrive_in_order_and_done_matches_the_ask_body(world: World, tmp_path) -> None:
    world.app.state.ask_service = fixture_service(tmp_path)
    events = stream(world, "hello world")
    names = [n for n, _ in events]
    assert names[0] == "meta" and names[-1] == "done"
    assert set(names[1:-1]) == {"delta"} and len(names) >= 4
    assert events[0][1]["request_id"] == events[-1][1]["request_id"]
    text = "".join(d["text"] for n, d in events if n == "delta")
    done = events[-1][1]
    assert text == done["answer"] == "Streamed fixture answer" and done["status"] == "answered"
    plain = world.ask("hello world").json()
    assert set(done) == set(plain)  # the same body shape as POST /api/context/ask


async def test_empty_question_is_the_same_422(world: World) -> None:
    response = world.client.post("/api/context/ask/stream", json={"question": "  "})
    assert response.status_code == 422 and response.json()["status"] == "invalid_request"


async def test_unavailable_transport_ends_with_done_unavailable(world: World, tmp_path) -> None:
    world.app.state.ask_service = fixture_service(tmp_path, content=None)  # no fixture file
    events = stream(world, "hello world")
    assert [n for n, _ in events] == ["meta", "done"]
    assert events[-1][1]["status"] == "unavailable" and events[-1][1]["answer"] is None


async def test_guard_deny_yields_an_error_event_and_sends_nothing(world: World) -> None:
    class Inner:
        fixture_mode = False
        base_url = "http://omniroute.invalid"
        api_key = "k"
        calls = 0

        def generate(self, *a, **k):
            Inner.calls += 1
            return {"content": "{}"}

        def generate_stream(self, *a, **k):
            Inner.calls += 1
            yield "leak"

    async def deny(_alias: str) -> Any:
        class Decision:
            allowed = False

            class reason:  # noqa: N801
                value = "LEAF_NON_LOCAL"

        return Decision()

    world.app.state.ask_service = AskService(
        transport=SecureTransport(Inner(), "test", deny), remote_provider_allowed=True
    )
    events = stream(world, "hello world")
    assert events[-1] == ("error", {"code": "secure_route_denied", "message": "LEAF_NON_LOCAL"})
    assert "done" not in [n for n, _ in events] and Inner.calls == 0


async def test_calendar_request_streams_a_proposal_event(world: World) -> None:
    events = stream(world, SENTENCE)
    names = [n for n, _ in events]
    assert names == ["meta", "delta", "proposal", "done"]
    proposal = dict(events)["proposal"]
    assert proposal["start"] == "2026-06-02T16:30:00-07:00" and proposal["requires_confirmation"]
    done = events[-1][1]
    assert done["status"] == "proposal" and done["proposal"] == proposal
    assert world.google.writes() == []


async def test_stream_accepts_account_and_session_fields(world: World) -> None:
    events = stream(world, SENTENCE, account_id=world.personal, session_id="s-1")
    assert events[0][1]["session_id"] == "s-1"
    assert dict(events)["proposal"]["account"]["id"] == world.personal


async def test_streamed_plain_answer_uses_the_model_stream(world: World) -> None:
    events = stream(world, "hello world")
    assert [d["text"] for n, d in events if n == "delta"] == ["A ", "streamed ", "answer."]
    assert events[-1][1]["answer"] == "A streamed answer."
