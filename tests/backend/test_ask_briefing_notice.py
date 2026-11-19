"""Bug condition exploration (defect C): irrelevant briefing failure surfaces (EXPECTED to fail pre-fix).
Findings (investigation C1-C5, UNFIXED code):
- C1: production log `ask.retrieval.adapter_failed domain=briefing error=AttributeError` (19:18, 19:28, 19:28
  on 2026-11-18). The log keeps only the exception type. It is not a timeout (that would be TimeoutError).
- C2/C3: sources/briefing.py does `e.start.isoformat()` for every event in `brief.today`, but
  BriefingEvent.start is Optional (all-day or start-less calendar events carry start=None), so
  AttributeError is raised. Other optional fields are guarded by `if a.due`. The adapter catches nothing,
  so retrieval marks the domain skipped.
- C4: app.state.briefing_service is wired (app.py:777), so the adapter does run in production.
- C5: `compose` is synchronous and called inline on the event loop in the adapter, so a slow compose
  blocks the loop and asyncio.wait_for cannot time it out or let other adapters proceed.
- Notice: ask._skipped_notice lists every skipped domain regardless of whether the question asked for it.
"""
from __future__ import annotations
import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.api.models import Briefing, BriefingEvent, BriefingOvernight
from backend.context.ask import _skipped_notice
from backend.context.retrieval import retrieve
from backend.context.retrieval.sources.briefing import briefing
from backend.context.retrieval.intent import parse_intent
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
QUESTION = "when do i next see pam?"
def _brief_with_startless_event() -> Briefing:
    return Briefing(
        kind="morning", generated_at=NOW, actions=[], waiting_on=[],
        today=[BriefingEvent(event_id="e1", title="Holiday", start=None)],
        overnight=BriefingOvernight(), empty=False,
    )
async def _calendar(state, intent, limit):
    return []
def test_question_has_no_briefing_hint() -> None:
    assert "briefing" not in parse_intent(QUESTION, NOW).domain_hints
def test_real_adapter_survives_event_without_start() -> None:
    state = SimpleNamespace(briefing_service=SimpleNamespace(compose=lambda *a, **k: _brief_with_startless_event()))
    rows = asyncio.run(briefing(state, parse_intent(QUESTION, NOW), 20))
    assert rows is not None  # UNFIXED: AttributeError: 'NoneType' object has no attribute 'isoformat'
def test_raising_adapter_does_not_surface_irrelevant_notice() -> None:
    async def boom(state, intent, limit):
        raise AttributeError("x")
    res = asyncio.run(retrieve(SimpleNamespace(), QUESTION, NOW, adapters={"calendar": _calendar, "briefing": boom}))
    assert "could not search briefing" not in _skipped_notice(res).lower()
def test_timing_out_adapter_does_not_surface_irrelevant_notice() -> None:
    async def slow(state, intent, limit):
        await asyncio.sleep(1)
        return []
    res = asyncio.run(retrieve(SimpleNamespace(), QUESTION, NOW, adapters={"calendar": _calendar, "briefing": slow}, timeout=0.05))
    assert "could not search briefing" not in _skipped_notice(res).lower()
def test_slow_synchronous_compose_does_not_block_other_adapters() -> None:
    def slow_compose(*a, **k):
        time.sleep(0.6)
        return _brief_with_startless_event().model_copy(update={"today": []})
    state = SimpleNamespace(briefing_service=SimpleNamespace(compose=slow_compose))
    stamp: dict[str, float] = {}
    async def fast(state, intent, limit):
        stamp["t"] = time.monotonic()
        return []
    async def go() -> float:
        t0 = time.monotonic()
        await retrieve(state, QUESTION, NOW, adapters={"briefing": briefing, "calendar": fast}, timeout=0.2)
        return stamp["t"] - t0
    # UNFIXED: the inline sleep blocks the loop, so the calendar adapter starts only after ~0.6s
    assert asyncio.run(go()) < 0.3
