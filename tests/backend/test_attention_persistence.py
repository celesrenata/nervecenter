"""Durable attention state transitions (Personal-Ops Recovery §6 / §12).

Proves that the ``resolve`` / ``acknowledge`` / ``suppress`` / ``snooze``
routes — previously in-memory only — now write through the durable
repository (``attention_items`` / ``snoozes``) *and* mirror into the
in-memory service when a durable registry is present on ``app.state``; and
that without a registry the behavior is byte-identical to the old
in-memory surface (the wire shape ``web/tests/e2e/personal-flow.spec.ts``
PF-3 pins: ``status:"TODAY"`` + a ``due`` on snooze, ``status`` on the
other actions, 200/404 codes, no exception, no 503).

Id-space contract (the same one ``POST /{id}/dismiss`` already uses):
- **wired** (registry on ``app.state``): the route id is the durable row id;
  the response is a durable row-shaped dict (``state`` / ``snoozed_until`` /
  ``resolved_at``), and the matching in-memory item is mirrored.
- **degraded** (no registry): the route id is the in-memory id; the response
  is the in-memory ``AttentionItem`` dict (``status`` / ``due``).

Harness: ``tests/backend/test_attention_v2.py`` (durable plane on a
file-backed SQLite scratch DB via the real ``RepositoryRegistry``; HTTP
surface through ``TestClient``). The *file* DB is deliberate so a simulated
restart (fresh engine over the same path) can prove the transitions survive.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

# ``backend.api.app`` must be imported BEFORE the attention endpoint module:
# the endpoint module imports ``backend.attention.service`` at module level,
# whose chain re-enters ``backend.api.app`` — importing the app first lets
# that package cycle complete cleanly (the established test-suite convention).
import backend.api.app  # noqa: F401
from backend.api.endpoints import attention as attention_ep
from backend.api.models import AttentionItem
from backend.attention import (
    AttentionService,
    get_attention_service,
    reset_attention_service,
)
from backend.persistence.connection import make_session_factory
from backend.persistence.models.base import Base
from backend.persistence.models.personal import (
    AttentionItemRow,
    NotificationLogRow,
    SnoozeRow,
)
from backend.persistence.repositories import RepositoryRegistry

#: A fixed reference clock so assertions are deterministic.
NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


async def _make_engine(
    tmp_path: pathlib.Path, name: str = "attention_persistence.db"
) -> tuple[AsyncEngine, RepositoryRegistry]:
    """A durable SQLite engine + ``RepositoryRegistry``, wired like production.

    Returns ``(engine, registry)``; the caller disposes the engine.
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / name}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
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
    app.state.attention_service = get_attention_service()
    return app


def _degraded_app() -> FastAPI:
    """No ``repository_registry`` at all → the honest degrade paths."""
    app = FastAPI()
    app.include_router(attention_ep.router)
    app.state.attention_service = get_attention_service()
    return app


async def _create_durable(
    registry: RepositoryRegistry,
    *,
    title: str = "Durable attention item",
    source_refs: list[str] | None = None,
) -> str:
    """Create a durable attention row; return its id.

    ``source_refs`` is the ref the in-memory mirror matches on (the same value
    passed as the in-memory item's ``sources``)."""
    refs = source_refs or ["test:1"]
    row = await registry.attention.create_attention_item(
        title=title,
        importance=6,
        urgency="normal",
        source_refs=list(refs),
    )
    return row.id


async def _create_in_memory(
    *,
    reason: str = "In-memory reason",
    ref: str = "test:1",
) -> str:
    """Create an in-memory item whose ``sources`` carry ``ref`` (the mirror
    match key the durable row uses)."""
    item = get_attention_service().create(
        {
            "reason": reason,
            "urgency": 0.5,
            "importance": 0.5,
            "sources": [ref],
        }
    )
    return item.attention_id


def _mem_by_ref(service: AttentionService, ref: str) -> AttentionItem | None:
    """The in-memory item whose ``sources`` carry ``ref`` (the mirror match)."""
    for item in service.list():
        if ref in list(item.sources):
            return item
    return None


def _status(item: AttentionItem) -> str:
    """The in-memory ``AttentionItem.status`` (``AttentionState``)."""
    return item.status.value


def _aware(dt: datetime | None) -> datetime | None:
    """Normalize a possibly-offset-naive datetime (SQLite returns naive) to UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _same(dt: datetime | None, expect: datetime) -> bool:
    a = _aware(dt)
    return a is not None and abs((a - expect).total_seconds()) < 2


# ---------------------------------------------------------------------------
# durable transitions (registry on app.state): durable row + in-memory mirror
# ---------------------------------------------------------------------------


class TestDurableTransitions:
    async def test_resolve_writes_durable_and_mirrors(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_engine(tmp_path)
        reset_attention_service()
        try:
            client = TestClient(_wired_app(registry))
            durable_id = await _create_durable(
                registry, title="resolve me", source_refs=["test:resolve"]
            )
            await _create_in_memory(reason="resolve me", ref="test:resolve")

            res = client.post(f"/api/attention/{durable_id}/resolve")
            assert res.status_code == 200
            body = res.json()
            # durable row-shaped wire contract (the dismiss pattern)
            assert body["state"] == "resolved"
            assert body["resolved_at"] is not None

            row = await registry.attention.get_attention_item(durable_id)
            assert row.state == "resolved"
            assert row.resolved_at is not None

            # the in-memory mirror (same reason/source ref) was resolved too
            service = get_attention_service()
            mem = _mem_by_ref(service, "test:resolve")
            assert mem is not None
            assert _status(mem) == "RESOLVED"
        finally:
            reset_attention_service()
            await engine.dispose()

    async def test_acknowledge_is_resolve_alias(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_engine(tmp_path)
        reset_attention_service()
        try:
            client = TestClient(_wired_app(registry))
            durable_id = await _create_durable(
                registry, title="handle me", source_refs=["test:handle"]
            )
            await _create_in_memory(reason="handle me", ref="test:handle")

            res = client.post(f"/api/attention/{durable_id}/acknowledge")
            assert res.status_code == 200
            assert res.json()["state"] == "resolved"
            assert res.json()["resolved_at"] is not None

            row = await registry.attention.get_attention_item(durable_id)
            assert row.state == "resolved"
            assert row.resolved_at is not None

            mem = _mem_by_ref(get_attention_service(), "test:handle")
            assert mem is not None
            assert _status(mem) == "RESOLVED"
        finally:
            reset_attention_service()
            await engine.dispose()

    async def test_suppress_dismisses_durable_and_mirrors(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_engine(tmp_path)
        reset_attention_service()
        try:
            client = TestClient(_wired_app(registry))
            durable_id = await _create_durable(
                registry, title="mute me", source_refs=["test:mute"]
            )
            await _create_in_memory(reason="mute me", ref="test:mute")

            res = client.post(f"/api/attention/{durable_id}/suppress")
            assert res.status_code == 200
            assert res.json()["state"] == "dismissed"

            # the frozen §6 state set has no "suppressed" member: an explicit
            # user mute is the same durable state as a dismiss.
            row = await registry.attention.get_attention_item(durable_id)
            assert row.state == "dismissed"

            mem = _mem_by_ref(get_attention_service(), "test:mute")
            assert mem is not None
            assert _status(mem) == "SUPPRESSED"
        finally:
            reset_attention_service()
            await engine.dispose()

    async def test_snooze_records_until_and_mirrors_due(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_engine(tmp_path)
        reset_attention_service()
        try:
            client = TestClient(_wired_app(registry))
            durable_id = await _create_durable(
                registry, title="later please", source_refs=["test:later"]
            )
            await _create_in_memory(reason="later please", ref="test:later")

            until = NOW + timedelta(hours=2)
            res = client.post(
                f"/api/attention/{durable_id}/snooze",
                json={"until": until.isoformat()},
            )
            assert res.status_code == 200
            body = res.json()
            assert body["state"] == "snoozed"
            assert body["snoozed_until"] is not None

            row = await registry.attention.get_attention_item(durable_id)
            assert row.state == "snoozed"
            assert row.snoozed_until is not None
            assert _same(row.snoozed_until, until)

            # a durable snooze row was recorded
            async with registry.session_factory() as session:
                count = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM snoozes "
                            "WHERE attention_item_id = :id"
                        ),
                        {"id": durable_id},
                    )
                ).scalar_one()
            assert count == 1

            mem = _mem_by_ref(get_attention_service(), "test:later")
            assert mem is not None
            assert _status(mem) == "TODAY"
            assert mem is not None
            assert mem.due is not None
            assert abs((mem.due - until).total_seconds()) < 2
        finally:
            reset_attention_service()
            await engine.dispose()

    async def test_unknown_id_404s_without_touching_durable(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_engine(tmp_path)
        reset_attention_service()
        try:
            client = TestClient(_wired_app(registry))
            durable_id = await _create_durable(registry, title="leave me")

            for action in ("resolve", "acknowledge", "suppress", "snooze"):
                res = client.post(
                    f"/api/attention/att-missing-{action}/{action}",
                    json={} if action == "snooze" else None,
                )
                assert res.status_code == 404

            # the durable row is untouched (still active)
            row = await registry.attention.get_attention_item(durable_id)
            assert row.state == "active"
        finally:
            reset_attention_service()
            await engine.dispose()

    async def test_degraded_item_with_no_durable_row_stays_in_memory(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A wired app must not fabricate durable state for in-memory-only ids:
        an id that exists only in memory degrades to the in-memory transition
        (200 + the in-memory dict) instead of a 404 or a phantom row."""
        engine, registry = await _make_engine(tmp_path)
        reset_attention_service()
        try:
            client = TestClient(_wired_app(registry))
            assert await registry.attention.list_attention_items() == []

            mem_id = await _create_in_memory(reason="only in memory")
            res = client.post(f"/api/attention/{mem_id}/resolve")
            assert res.status_code == 200
            body = res.json()
            # in-memory wire shape (attention_id/status), NOT the row shape
            assert body["attention_id"] == mem_id
            assert body["status"] == "RESOLVED"

            # nothing durable was fabricated
            assert await registry.attention.list_attention_items() == []
        finally:
            reset_attention_service()
            await engine.dispose()


# ---------------------------------------------------------------------------
# degraded mode (no registry) — wire contract unchanged
# ---------------------------------------------------------------------------


class TestDegradedUnchanged:
    async def test_resolve_acknowledge_suppress_wire_shape(self) -> None:
        reset_attention_service()
        try:
            client = TestClient(_degraded_app())
            mem_id = await _create_in_memory()

            for action, status in (
                ("resolve", "RESOLVED"),
                ("acknowledge", "RESOLVED"),
                ("suppress", "SUPPRESSED"),
            ):
                res = client.post(f"/api/attention/{mem_id}/{action}")
                assert res.status_code == 200
                body = res.json()
                # the in-memory AttentionItem shape, untouched by this change
                assert body["attention_id"] == mem_id
                assert body["status"] == status

            item = get_attention_service().get(mem_id)
            assert _status(item) == "SUPPRESSED"  # last action wins
        finally:
            reset_attention_service()

    async def test_snooze_wire_shape(self) -> None:
        reset_attention_service()
        try:
            client = TestClient(_degraded_app())
            mem_id = await _create_in_memory()

            until = NOW + timedelta(days=1)
            res = client.post(
                f"/api/attention/{mem_id}/snooze", json={"until": until.isoformat()}
            )
            assert res.status_code == 200
            body = res.json()
            # e2e PF-3: snooze → TODAY bucket with a non-empty due
            assert body["status"] == "TODAY"
            assert isinstance(body["due"], str) and body["due"]

            item = get_attention_service().get(mem_id)
            assert _status(item) == "TODAY"
            assert item.due is not None
        finally:
            reset_attention_service()

    async def test_snooze_without_until_defaults_to_one_day(self) -> None:
        reset_attention_service()
        try:
            client = TestClient(_degraded_app())
            mem_id = await _create_in_memory()

            res = client.post(f"/api/attention/{mem_id}/snooze", json={})
            assert res.status_code == 200
            body = res.json()
            assert body["status"] == "TODAY"
            assert isinstance(body["due"], str) and body["due"]
        finally:
            reset_attention_service()

    async def test_unknown_id_404_degraded(self) -> None:
        reset_attention_service()
        try:
            client = TestClient(_degraded_app())
            for action in ("resolve", "acknowledge", "suppress", "snooze"):
                res = client.post(
                    f"/api/attention/att-missing/{action}",
                    json={} if action == "snooze" else None,
                )
                assert res.status_code == 404
        finally:
            reset_attention_service()


# ---------------------------------------------------------------------------
# restart semantics — transitions survive a simulated process restart
# ---------------------------------------------------------------------------


class TestTransitionsSurviveRestart:
    async def test_state_and_snooze_survive_restart(self, tmp_path: pathlib.Path) -> None:
        db_path = tmp_path / "restart.db"

        def _engine() -> AsyncEngine:
            return create_async_engine(
                f"sqlite+aiosqlite:///{db_path}",
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )

        def _tables() -> list[Any]:
            return [
                AttentionItemRow.__table__,
                SnoozeRow.__table__,
                NotificationLogRow.__table__,
            ]

        engine = _engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=_tables())

        registry = RepositoryRegistry(make_session_factory(engine))
        reset_attention_service()
        engine2: AsyncEngine | None = None
        try:
            # pre-restart transitions, written through the durable repo
            resolved_id = await _create_durable(registry, title="will resolve")
            snoozed_id = await _create_durable(registry, title="will snooze")
            until = NOW + timedelta(days=1)

            assert await registry.attention.set_state(resolved_id, "resolved", at=NOW)
            assert await registry.attention.snooze(snoozed_id, until=until, reason="later")

            # simulate the restart: dispose the engine, open a fresh one over
            # the same file (the app wiring re-creates the session factory)
            await engine.dispose()
            engine2 = _engine()
            registry2 = RepositoryRegistry(make_session_factory(engine2))

            # every transition is read back unchanged
            resolved = await registry2.attention.get_attention_item(resolved_id)
            assert resolved is not None
            assert resolved.state == "resolved"
            assert resolved.resolved_at is not None

            snoozed = await registry2.attention.get_attention_item(snoozed_id)
            assert snoozed is not None
            assert snoozed.state == "snoozed"
            assert snoozed.snoozed_until is not None
            assert _same(snoozed.snoozed_until, until)

            # the snooze row is durable too
            async with registry2.session_factory() as session:
                rows = (
                    await session.execute(text("SELECT count(*) FROM snoozes"))
                ).scalar_one()
            assert rows == 1

            # a transition performed AFTER the restart also lands durably
            post_id = await _create_durable(registry2, title="created post-restart")
            assert await registry2.attention.set_state(post_id, "resolved", at=NOW)
            fresh = await registry2.attention.get_attention_item(post_id)
            assert fresh is not None
            assert fresh.state == "resolved"
        finally:
            reset_attention_service()
            await engine.dispose()
            if engine2 is not None:
                await engine2.dispose()
