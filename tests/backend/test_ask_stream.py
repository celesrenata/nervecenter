"""POST /api/context/ask/stream: SSE events, guard, unavailable, calendar proposal."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


class FlakyTransport:
    """Refuses ``failures`` times with ``reason`` before streaming an answer."""

    fixture_mode = False
    base_url = "http://omniroute.invalid"

    def __init__(self, failures: int, reason: str = "http_503", partial: bool = False) -> None:
        self.failures = failures
        self.reason = reason
        self.partial = partial
        self.calls: list[dict[str, Any]] = []

    def generate_stream(self, model: str, prompt: str, **kwargs: Any):
        from backend.inference.omniroute import OmniRouteUnavailableError

        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            if self.partial:
                yield "half an "
            raise OmniRouteUnavailableError([(model, model)], {(model, model): self.reason})
        yield "Warm "
        yield "answer."


def _flaky_service(transport: FlakyTransport) -> tuple[AskService, list[float]]:
    svc = AskService(transport=transport, remote_provider_allowed=True)
    sleeps: list[float] = []
    svc._sleep = sleeps.append
    return svc, sleeps


async def test_busy_model_is_retried_with_status_events_then_answers(world: World) -> None:
    transport = FlakyTransport(failures=3, reason="http_503")
    world.app.state.ask_service, sleeps = _flaky_service(transport)
    events = stream(world, "hello world")
    names = [n for n, _ in events]
    assert names == ["meta", "status", "status", "status", "delta", "delta", "done"]
    statuses = [d for n, d in events if n == "status"]
    assert [s["attempt"] for s in statuses] == [1, 2, 3]
    assert statuses[0]["code"] == "retrying" and "http_503" in statuses[0]["message"]
    assert sleeps == [2.0, 4.0, 8.0]
    assert events[-1][1]["status"] == "answered" and events[-1][1]["answer"] == "Warm answer."
    assert all(c.get("attempts") == 1 for c in transport.calls)


async def test_connection_errors_and_500s_are_retried_too(world: World) -> None:
    for reason in ("ConnectError", "http_500", "empty"):
        world.app.state.ask_service, _ = _flaky_service(FlakyTransport(failures=1, reason=reason))
        events = stream(world, "hello world")
        assert events[-1][1]["status"] == "answered", reason


async def test_real_rejections_are_not_retried(world: World) -> None:
    transport = FlakyTransport(failures=5, reason="http_401")
    world.app.state.ask_service, sleeps = _flaky_service(transport)
    events = stream(world, "hello world")
    assert [n for n, _ in events] == ["meta", "done"] and sleeps == []
    assert events[-1][1]["status"] == "unavailable" and len(transport.calls) == 1


async def test_gives_up_after_the_attempt_budget(world: World) -> None:
    from backend.context import ask as ask_mod

    transport = FlakyTransport(failures=99, reason="http_503")
    world.app.state.ask_service, sleeps = _flaky_service(transport)
    events = stream(world, "hello world")
    assert len(transport.calls) == ask_mod.ASK_STREAM_ATTEMPTS
    assert [n for n, _ in events].count("status") == ask_mod.ASK_STREAM_ATTEMPTS - 1
    assert events[-1][1]["status"] == "unavailable"
    assert max(sleeps) <= ask_mod.ASK_STREAM_BACKOFF_MAX_S


async def test_a_partial_answer_is_never_retried(world: World) -> None:
    transport = FlakyTransport(failures=1, reason="http_503", partial=True)
    world.app.state.ask_service, sleeps = _flaky_service(transport)
    events = stream(world, "hello world")
    assert sleeps == [] and len(transport.calls) == 1
    assert events[-1] == (
        "error",
        {"code": "stream_interrupted", "message": "The answer stopped before it finished."},
    )


async def test_quiet_streams_get_keepalive_comments_and_events_pass_through() -> None:
    import time as _time

    from backend.api.endpoints.context import _with_keepalive

    def slow():
        yield "meta", {"request_id": "r"}
        _time.sleep(0.35)
        yield "done", {"status": "answered"}

    frames = [f async for f in _with_keepalive(slow, interval=0.1)]
    assert frames[0].startswith("event: meta") and frames[-1].startswith("event: done")
    assert ": keep-alive\n\n" in frames[1:-1]


async def test_producer_crash_becomes_one_error_event() -> None:
    from backend.api.endpoints.context import _with_keepalive

    def broken():
        yield "meta", {}
        raise RuntimeError("secret detail")

    frames = [f async for f in _with_keepalive(broken, interval=5)]
    assert frames[-1].startswith("event: error") and "secret detail" not in frames[-1]
