"""Property 13: the event stream preserves received order and filters by Session.

Validates: Requirements 3.7
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from hypothesis import given, settings, strategies as st

from backend.workbench.sessions import SessionService

TARGET = "ses_target"
OTHERS = ["ses_a", "ses_b", "ses_c"]


class FakeClient:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def stream_events(self) -> AsyncIterator[dict[str, Any]]:
        for e in self._events:
            await asyncio.sleep(0)
            yield e


def _service(events: list[dict[str, Any]]) -> SessionService:
    svc = SessionService(session_factory=None, client=FakeClient(events), usage=object())  # type: ignore[arg-type]

    async def _mirror(session_id: str) -> Any:
        return SimpleNamespace(id=session_id, model=None)

    svc._mirror = _mirror  # type: ignore[method-assign]
    return svc


# Plain events (no path / usage fields) so only the event itself is relayed.
stream = st.lists(
    st.sampled_from([TARGET, *OTHERS]),
    max_size=40,
).map(
    lambda sids: [
        {"type": "message.part", "data": {"sessionID": sid, "seq": i}}
        for i, sid in enumerate(sids)
    ]
)


@settings(max_examples=100, deadline=None)
@given(events=stream)
def test_stream_preserves_order_and_filters_other_sessions(events: list[dict[str, Any]]) -> None:
    async def run() -> list[dict[str, Any]]:
        return [e async for e in _service(events).events(TARGET)]

    got = asyncio.run(run())
    expected = [e for e in events if e["data"]["sessionID"] == TARGET]
    assert got == expected
    seqs = [e["data"]["seq"] for e in got]
    assert seqs == sorted(seqs)
