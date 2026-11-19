"""Calendar-event → obligation producer: dedup, reschedule, cancel."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.obligations.producers import bridge_calendar_event
from backend.persistence.repositories.obligations_repo import ObligationsRepository
from tests.backend.test_obligations import _make_harness

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


async def _bridge(registry, start, **kw):
    return await bridge_calendar_event(
        registry, account_id="acct1", ical_uid="uid-1", title="Dentist",
        start=start, now=NOW, **kw,
    )


async def _all(registry):
    return await ObligationsRepository(registry.session_factory).list(limit=50)


@pytest.mark.asyncio
async def test_repeated_sync_yields_one_obligation(tmp_path):
    engine, registry = await _make_harness(tmp_path)
    try:
        start = NOW + timedelta(days=3)
        assert (await _bridge(registry, start))["action"] == "created"
        assert (await _bridge(registry, start))["action"] == "merged"
        rows = await _all(registry)
        assert len(rows) == 1
        assert rows[0].source_record_ref == {"plane": "calendar", "id": "acct1:uid-1"}
        assert rows[0].evidence[0]["source_kind"] == "calendar_event"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reschedule_moves_without_duplicate(tmp_path):
    engine, registry = await _make_harness(tmp_path)
    try:
        await _bridge(registry, NOW + timedelta(days=3))
        assert (await _bridge(registry, NOW + timedelta(days=5)))["action"] == "moved"
        rows = await _all(registry)
        open_rows = [r for r in rows if r.status not in ("dismissed", "done")]
        assert len(open_rows) == 1
        assert open_rows[0].due_at.date() == (NOW + timedelta(days=5)).date()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_and_out_of_window(tmp_path):
    engine, registry = await _make_harness(tmp_path)
    try:
        await _bridge(registry, NOW + timedelta(days=3))
        out = await _bridge(registry, NOW + timedelta(days=3), deleted=True)
        assert out["action"] == "retired"
        assert all(r.status == "dismissed" for r in await _all(registry))
        far = await _bridge(registry, NOW + timedelta(days=90))
        assert far["action"] == "skipped"
    finally:
        await engine.dispose()
