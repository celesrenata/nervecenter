"""Attention v2 — aggregation / escalation plane tests (Personal-Ops §12).

Covers:

- the pure helpers: canonical-key stability, duration parsing, and the
  deterministic ``escalation_level_for`` ladder (same clock → same level);
- the durable :class:`AttentionAggregator` on a SQLite scratch DB through the
  real ``RepositoryRegistry``: candidate collection, upsert (create once,
  refresh in place — never stack), the in-memory mirror, and the
  one-notification-per (item, level) anti-nag guarantee;
- the durable HTTP routes ``POST /api/attention/sync`` and
  ``POST /api/attention/{id}/dismiss`` (wired + degraded apps).

Harness pattern: ``tests/backend/test_bills.py`` — durable plane on a SQLite
scratch DB via ``RepositoryRegistry`` on ``app.state``; the HTTP surface
through ``TestClient``.
"""

from __future__ import annotations

import pathlib
import types
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

# ``backend.api.app`` must be imported BEFORE the attention endpoint module:
# the endpoint module imports ``backend.attention.service`` at module level,
# whose chain re-enters ``backend.api.app`` — importing the app first lets
# that package cycle complete cleanly (the established test-suite convention).
import backend.api.app  # noqa: F401
from backend.api.endpoints import attention as attention_ep
from backend.attention import get_attention_service, reset_attention_service
from backend.attention.aggregator import (
    AttentionAggregator,
    _canonical_key,
    _parse_duration,
    escalation_level_for,
    row_to_wire,
)
from backend.persistence.connection import make_session_factory
from backend.persistence.models.base import Base
from backend.persistence.models.bills import BillRow
from backend.persistence.models.personal import (
    AttentionItemRow,
    NotificationLogRow,
    SnoozeRow,
)
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.bills_repo import BillsRepository

# A fixed, tz-aware reference clock so the ladder tests are deterministic.
NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Shared harness (pattern: tests/backend/test_bills.py)
# ---------------------------------------------------------------------------


async def _make_harness(tmp_path: pathlib.Path) -> tuple[AsyncEngine, RepositoryRegistry]:
    """A durable SQLite DB + repository registry, wired like production."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'attention_v2.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                BillRow.__table__,
                AttentionItemRow.__table__,
                SnoozeRow.__table__,
                NotificationLogRow.__table__,
            ],
        )
    registry = RepositoryRegistry(make_session_factory(engine))
    return engine, registry


def _wired_app(registry: RepositoryRegistry) -> FastAPI:
    """App with the durable registry on ``app.state`` (production shape)."""
    app = FastAPI()
    app.include_router(attention_ep.router)
    app.state.repository_registry = registry
    return app


def _degraded_app() -> FastAPI:
    """No ``repository_registry`` at all → the degrade paths."""
    app = FastAPI()
    app.include_router(attention_ep.router)
    return app


class _State:
    """Minimal ``app.state`` stand-in for direct aggregator tests."""

    def __init__(self, registry: RepositoryRegistry | None) -> None:
        self.repository_registry = registry


async def _seed_bill(registry: RepositoryRegistry, *, due_in_days: float = 3.0) -> None:
    """A pending bill due ``due_in_days`` from now (level 1 within 7 days)."""
    repo = BillsRepository(registry.session_factory)
    await repo.create_bill(
        payee="Acme Power",
        amount_cents=14250,
        currency="USD",
        due_at=datetime.now(UTC) + timedelta(days=due_in_days),
    )


def _item(deadline, created=None, policy=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(deadline_at=deadline, created_at=created, escalation_policy=policy)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestPureHelpers:
    def test_canonical_key_stable_and_order_independent(self) -> None:
        a = _canonical_key("mail", ["mail:1", "mail:2"])
        # ref order is normalized (sorted) → same key
        assert a == _canonical_key("mail", ["mail:2", "mail:1"])
        # stable across calls
        assert a == _canonical_key("mail", ["mail:1", "mail:2"])
        # different kind or refs → different key
        assert a != _canonical_key("messages", ["mail:1", "mail:2"])
        assert a != _canonical_key("mail", ["mail:1"])
        assert a != _canonical_key("mail", ["mail:1", "mail:3"])

    def test_parse_duration_units(self) -> None:
        assert _parse_duration("7d") == timedelta(days=7)
        assert _parse_duration("48h") == timedelta(hours=48)
        assert _parse_duration("4h") == timedelta(hours=4)
        assert _parse_duration("90m") == timedelta(minutes=90)
        assert _parse_duration("90s") == timedelta(seconds=90)
        assert _parse_duration(120) == timedelta(seconds=120)
        assert _parse_duration("bogus") is None
        assert _parse_duration(None) is None


def _fake_row(**overrides) -> types.SimpleNamespace:
    """A stand-in for a durable :class:`AttentionItemRow`.

    ``row_to_wire`` only reads attributes, so a namespace suffices — but the
    endpoint's ``_dump_durable`` routes on ``__table__.name == "attention_items"``,
    so we attach a minimal table stub for that path too.
    """
    base = dict(
        id="11111111-2222-3333-4444-555555555555",
        title="Reply to Dana",
        body="follow up on the Q4 numbers",
        importance=8,
        urgency="high",
        deadline_at=None,
        source_refs=["mail:msg-1", "person:dana"],
        recommended_action="Reply to Dana's email",
        escalation_policy={},
        state="active",
        snoozed_until=None,
        resolved_at=None,
        resolution_condition=None,
        created_at=datetime(2026, 10, 28, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 10, 28, 9, 30, tzinfo=UTC),
    )
    base.update(overrides)
    row = types.SimpleNamespace(**base)
    row.__table__ = types.SimpleNamespace(name="attention_items")
    return row


class TestRowToWire:
    """The durable row → wire-shape mapper (GAP: durable dump alignment)."""

    def test_maps_to_wire_shape(self) -> None:
        row = _fake_row()
        wire = row_to_wire(row)
        # Wire keys, not raw ORM column names.
        assert set(wire) == {
            "attention_id", "reason", "priority", "urgency", "importance",
            "due", "entities", "sources", "suggested_action", "status",
            "why", "created_at", "updated_at",
        }
        assert "id" not in wire and "title" not in wire and "state" not in wire

    def test_floats_and_status_vocabulary(self) -> None:
        # importance 8 → 0.8, urgency "high" → 0.75 floats in [0, 1].
        row = _fake_row()
        wire = row_to_wire(row)
        assert wire["importance"] == 0.8
        assert wire["urgency"] == 0.75
        assert wire["attention_id"] == row.id
        assert wire["reason"] == row.title
        assert wire["suggested_action"] == row.recommended_action
        # base 0.5*0.8 + 0.5*0.75 = 0.775 + suggested_action bonus 0.10
        assert wire["priority"] == round(0.775 + 0.10, 4)
        assert 0.0 <= wire["priority"] <= 1.0
        assert wire["status"] == "NOW"
        assert wire["sources"] == ["mail:msg-1", "person:dana"]
        assert wire["entities"] == ["msg-1", "dana"]

    def test_state_vocabulary(self) -> None:
        assert row_to_wire(_fake_row(state="snoozed"))["status"] == "TODAY"
        assert row_to_wire(_fake_row(state="resolved"))["status"] == "RESOLVED"
        assert row_to_wire(_fake_row(state="dismissed"))["status"] == "SUPPRESSED"

    def test_priority_clamped_and_deadline_bonus(self) -> None:
        # overdue deadline → +0.25 on top of a high base, clamped to 1.0.
        row = _fake_row(
            importance=10,
            urgency="critical",
            deadline_at=datetime.now(UTC) - timedelta(hours=1),
        )
        assert row_to_wire(row)["priority"] == 1.0
        # no deadline, no suggested action → base only.
        row = _fake_row(importance=4, urgency="low", recommended_action=None)
        assert row_to_wire(row)["priority"] == round(0.2 + 0.15, 4)


class TestEscalationLevel:
    def test_ladder_boundaries_default_policy(self) -> None:
        # thresholds measured from the deadline: 4h critical / 48h high / 7d normal
        assert escalation_level_for(_item(NOW + timedelta(hours=1)), NOW) == "critical"
        assert escalation_level_for(_item(NOW + timedelta(hours=48)), NOW) == "high"
        # just past the 48h threshold → baseline
        assert escalation_level_for(_item(NOW + timedelta(hours=48, minutes=1)), NOW) == "normal"
        assert escalation_level_for(_item(NOW + timedelta(days=7)), NOW) == "normal"
        assert escalation_level_for(_item(NOW + timedelta(days=10)), NOW) == "normal"
        # overdue (time-to-deadline negative) → the most urgent level
        assert escalation_level_for(_item(NOW - timedelta(hours=5)), NOW) == "critical"

    def test_falls_back_to_created_at_when_no_deadline(self) -> None:
        item = _item(None, created=NOW + timedelta(hours=1))
        assert escalation_level_for(item, NOW) == "critical"

    def test_custom_policy_overrides_defaults(self) -> None:
        policy = {"levels": {"normal": "1d", "high": "2d", "critical": "3d"}}
        assert (
            escalation_level_for(_item(NOW + timedelta(days=3), policy=policy), NOW) == "critical"
        )
        assert escalation_level_for(_item(NOW + timedelta(days=2), policy=policy), NOW) == "high"
        assert escalation_level_for(_item(NOW + timedelta(days=10), policy=policy), NOW) == "normal"

    def test_no_time_anchor_stays_baseline(self) -> None:
        assert escalation_level_for(_item(None, None), NOW) == "normal"

    def test_deterministic_same_clock_same_answer(self) -> None:
        item = _item(NOW + timedelta(days=3))
        assert escalation_level_for(item, NOW) == "normal"
        assert escalation_level_for(item, NOW) == escalation_level_for(item, NOW)
        # a genuinely later clock re-evaluates (crosses the overdue line)
        assert escalation_level_for(item, NOW + timedelta(days=4)) == "critical"


# ---------------------------------------------------------------------------
# Durable aggregator (SQLite scratch DB through the real registry)
# ---------------------------------------------------------------------------


class TestAggregatorDurable:
    async def test_collect_candidates_bill_only_source(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            agg = AttentionAggregator(_State(registry))
            await _seed_bill(registry, due_in_days=3.0)
            cands = await agg.collect_candidates(datetime.now(UTC))
            # the bills table is the only one seeded; every other source
            # degrades silently (no service / empty table) → exactly 1 candidate
            assert len(cands) == 1
            c = cands[0]
            assert c["source_kind"] == "bills"
            assert c["source_refs"][0].startswith("bill:")
            # bill level 1 (due within 7 days) → high urgency, importance 6
            assert c["urgency"] == "high"
            assert c["importance"] == 6
            assert c["deadline_at"] is not None
            assert c["escalation_policy"]["levels"]
        finally:
            await engine.dispose()

    async def test_sync_upserts_in_place_and_mirrors(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        reset_attention_service()
        try:
            agg = AttentionAggregator(_State(registry))
            await _seed_bill(registry, due_in_days=3.0)

            first = await agg.sync()
            assert first == {"created": 1, "updated": 0, "skipped": 0}
            # a second sync refreshes the same item — never stacks a duplicate
            second = await agg.sync()
            assert second == {"created": 0, "updated": 1, "skipped": 0}

            rows = await registry.attention.list_attention_items()
            assert len(rows) == 1
            assert rows[0].state == "active"

            # the in-memory mirror carries the candidate exactly once
            service = get_attention_service()
            items = service.list()
            assert len(items) == 1
            assert any(s.startswith("bill:") for s in items[0].sources)
        finally:
            reset_attention_service()
            await engine.dispose()

    async def test_run_escalation_notifies_once_per_level(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        reset_attention_service()
        try:
            agg = AttentionAggregator(_State(registry))
            repo = registry.attention
            now = datetime.now(UTC)

            # overdue item → crosses the "critical" threshold on the first pass
            row = await repo.create_attention_item(
                title="Overdue bill",
                importance=5,
                urgency="normal",
                deadline_at=now - timedelta(hours=1),
                source_refs=["bill:overdue-1"],
            )
            result = await agg.run_escalation(now)
            assert result[0]["level"] == "critical"
            assert result[0]["notified"] is True
            fresh = await repo.get_attention_item(row.id)
            assert fresh.urgency == "critical"
            assert fresh.importance == 9  # bumped to the level importance
            assert await repo.already_notified(row.id, "critical") is True

            # anti-nag: a second pass at the same clock never re-notifies
            again = await agg.run_escalation(now)
            assert again[0]["notified"] is False

            # a far-future item notifies exactly once at the "normal" baseline
            far = await repo.create_attention_item(
                title="Distant item",
                importance=5,
                urgency="low",
                deadline_at=now + timedelta(days=30),
            )
            third = await agg.run_escalation(now)
            by_id = {str(e["item_id"]): e for e in third}
            assert by_id[str(far.id)]["level"] == "normal"
            assert by_id[str(far.id)]["notified"] is True
            assert by_id[str(row.id)]["notified"] is False
        finally:
            reset_attention_service()
            await engine.dispose()

    async def test_transition_dismiss_durable(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        reset_attention_service()
        try:
            agg = AttentionAggregator(_State(registry))
            repo = registry.attention
            row = await repo.create_attention_item(
                title="Dismiss me",
                importance=5,
                urgency="normal",
            )
            out = await agg.transition(row.id, "dismiss")
            assert out is not None
            assert out.state == "dismissed"
            assert out.resolved_at is not None
            assert await agg.transition("does-not-exist", "dismiss") is None
        finally:
            reset_attention_service()
            await engine.dispose()


# ---------------------------------------------------------------------------
# Durable HTTP surface
# ---------------------------------------------------------------------------


class TestDurableEndpoints:
    def test_sync_endpoint_degraded(self) -> None:
        client = TestClient(_degraded_app())
        # no registry → honest zero counts, still a 200
        r = client.post("/api/attention/sync")
        assert r.status_code == 200
        assert r.json() == {"created": 0, "updated": 0, "skipped": 0}
        # unknown item → 404
        r = client.post("/api/attention/does-not-exist/dismiss")
        assert r.status_code == 404

    async def test_sync_creates_and_dismiss_is_durable(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        reset_attention_service()
        try:
            client = TestClient(_wired_app(registry))
            await _seed_bill(registry, due_in_days=3.0)

            r = client.post("/api/attention/sync")
            assert r.status_code == 200
            assert r.json()["created"] == 1

            rows = await registry.attention.list_attention_items()
            assert len(rows) == 1
            item_id = rows[0].id

            r = client.post(f"/api/attention/{item_id}/dismiss")
            assert r.status_code == 200
            # aligned wire contract: durable rows map onto the in-memory shape
            assert r.json()["status"] == "SUPPRESSED"

            fresh = await registry.attention.get_attention_item(item_id)
            assert fresh.state == "dismissed"
            assert fresh.resolved_at is not None

            r = client.post("/api/attention/does-not-exist/dismiss")
            assert r.status_code == 404
        finally:
            reset_attention_service()
            await engine.dispose()

    def test_dismiss_in_memory_degraded(self) -> None:
        # degraded app (no registry): dismiss mirrors to the in-memory queue
        # using the endpoint vocabulary (SUPPRESSED = removed from the live queue)
        reset_attention_service()
        try:
            client = TestClient(_degraded_app())
            r = client.post("/api/attention", json={"reason": "call mom"})
            assert r.status_code == 201
            aid = r.json()["attention_id"]

            r = client.post(f"/api/attention/{aid}/dismiss")
            assert r.status_code == 200
            assert r.json()["status"] == "SUPPRESSED"

            feed = client.get("/api/attention/now").json()["data"]
            assert all(i["attention_id"] != aid for i in feed)
        finally:
            reset_attention_service()
