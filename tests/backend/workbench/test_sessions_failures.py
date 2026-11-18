"""Handoff regression and Session failure tests (task 11.9).

**Validates: Requirements 3.10, 3.12, 10.1, 10.2, 10.6**
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.handoffs.central import OpenCodeError
from backend.workbench import sessions as sessions_mod
from backend.workbench.sessions import SessionError, SessionService

_DDL = (
    "CREATE TABLE workbench_projects (id CHAR(32) PRIMARY KEY, slug TEXT NOT NULL, "
    "kind TEXT NOT NULL, title TEXT NOT NULL, created_at TIMESTAMP NOT NULL, "
    "last_activity_at TIMESTAMP NOT NULL, size_bytes BIGINT NOT NULL DEFAULT 0, "
    "archived BOOLEAN NOT NULL DEFAULT 0, push_approved BOOLEAN NOT NULL DEFAULT 0, "
    "UNIQUE (slug, kind))",
    "CREATE TABLE workbench_sessions (id TEXT PRIMARY KEY, project_id CHAR(32) NOT NULL, "
    "title TEXT, status TEXT, updated_at TIMESTAMP, model TEXT, small_model TEXT)",
    "CREATE TABLE coding_handoffs (id CHAR(32) PRIMARY KEY, created_at TIMESTAMP, "
    "updated_at TIMESTAMP, target TEXT NOT NULL, runner TEXT NOT NULL, title TEXT NOT NULL, "
    "brief TEXT NOT NULL, project_slug TEXT, repo TEXT, requested_by TEXT NOT NULL, "
    "status TEXT NOT NULL, push_approved BOOLEAN NOT NULL DEFAULT 0, claimed_by TEXT, "
    "claimed_at TIMESTAMP, finished_at TIMESTAMP, result_summary TEXT, error TEXT, "
    "session_ref TEXT)",
)

_PID = uuid.uuid4().hex


class _Client:
    def __init__(self, *, create_exc=None, abort_hang=False, abort_exc=None) -> None:
        self.create_exc = create_exc
        self.abort_hang = abort_hang
        self.abort_exc = abort_exc

    async def create_session(self, **_: object) -> str:
        if self.create_exc is not None:
            raise self.create_exc
        return "ses_1"

    async def abort_session(self, session_id: str) -> None:
        if self.abort_hang:
            await asyncio.sleep(3600)
        if self.abort_exc is not None:
            raise self.abort_exc


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        for ddl in _DDL:
            await conn.execute(text(ddl))
        now = datetime.now(UTC).isoformat(sep=" ")
        await conn.execute(
            text(
                "INSERT INTO workbench_projects (id, slug, kind, title, created_at, "
                "last_activity_at) VALUES (:id, 'demo', 'opencode', 'Demo', :n, :n)"
            ),
            {"id": _PID, "n": now},
        )
        for i in range(3):
            await conn.execute(
                text(
                    "INSERT INTO coding_handoffs (id, created_at, updated_at, target, runner, "
                    "title, brief, requested_by, status) VALUES (:id, :n, :n, 'opencode', "
                    "'central', :t, :b, 'owner', 'queued')"
                ),
                {"id": uuid.uuid4().hex, "n": now, "t": f"t{i}", "b": f"brief {i}"},
            )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _handoffs(factory) -> list[tuple]:
    async with factory() as s:
        rows = await s.execute(text("SELECT * FROM coding_handoffs ORDER BY id"))
        return [tuple(r) for r in rows.all()]


async def _status(factory, sid: str) -> str:
    async with factory() as s:
        return (
            await s.execute(text("SELECT status FROM workbench_sessions WHERE id=:i"), {"i": sid})
        ).scalar_one()


async def test_session_start_leaves_coding_handoffs_unchanged(factory) -> None:
    before = await _handoffs(factory)
    assert len(before) == 3
    svc = SessionService(factory, _Client())
    result = await svc.create("demo")
    assert result["status"] == "running"
    assert await _status(factory, "ses_1") == "running"
    assert await _handoffs(factory) == before


async def test_opencode_error_returns_unavailable_and_creates_nothing(factory) -> None:
    before = await _handoffs(factory)
    svc = SessionService(factory, _Client(create_exc=OpenCodeError("down")))
    with pytest.raises(SessionError) as exc:
        await svc.create("demo")
    assert (exc.value.status, exc.value.code) == (503, "opencode_unavailable")
    assert await _handoffs(factory) == before
    assert await svc.list("demo") == []


async def test_opencode_timeout_returns_unavailable(factory, monkeypatch) -> None:
    monkeypatch.setattr(sessions_mod, "REQUEST_TIMEOUT_S", 0.05)

    class Hang(_Client):
        async def create_session(self, **_: object) -> str:
            await asyncio.sleep(3600)
            return "never"

    svc = SessionService(factory, Hang())
    with pytest.raises(SessionError) as exc:
        await svc.create("demo")
    assert (exc.value.status, exc.value.code) == (503, "opencode_unavailable")
    assert await svc.list("demo") == []


async def test_unconfirmed_abort_leaves_session_running(factory, monkeypatch) -> None:
    monkeypatch.setattr(sessions_mod, "ABORT_TIMEOUT_S", 0.05)
    await SessionService(factory, _Client()).create("demo")
    svc = SessionService(factory, _Client(abort_hang=True))
    with pytest.raises(SessionError) as exc:
        await svc.abort("ses_1")
    assert (exc.value.status, exc.value.code) == (504, "abort_timeout")
    assert await _status(factory, "ses_1") == "running"


async def test_confirmed_abort_marks_aborted(factory) -> None:
    await SessionService(factory, _Client()).create("demo")
    out = await SessionService(factory, _Client()).abort("ses_1")
    assert out["status"] == "aborted"
    assert await _status(factory, "ses_1") == "aborted"
