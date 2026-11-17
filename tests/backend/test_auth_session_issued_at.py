"""Login must not depend on a DB default for auth_sessions.issued_at.

Production's auth_sessions table (created by an early migration) has NO server
default on issued_at; only the ORM declares one. This builds the table the way
production has it and records a session.
"""
from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import backend.api.app  # noqa: F401 - import-order guard
from backend.api import auth_service
from backend.persistence.connection import make_session_factory


async def test_record_session_sets_issued_at_without_a_db_default(tmp_path: pathlib.Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE auth_sessions (id VARCHAR PRIMARY KEY, sub VARCHAR NOT NULL, "
            "typ VARCHAR NOT NULL, source VARCHAR NOT NULL, device_id VARCHAR, "
            "issued_at TIMESTAMP NOT NULL, expires_at TIMESTAMP NOT NULL, "
            "revoked BOOLEAN NOT NULL, revoked_at TIMESTAMP)"))
    ctx = SimpleNamespace(session_factory=make_session_factory(engine))
    await auth_service.record_session(
        ctx, jti="j1", sub="owner", typ="refresh",
        expires_at=datetime.now(UTC) + timedelta(days=1), source="sso")
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT issued_at FROM auth_sessions"))).first()
    assert row is not None and row[0] is not None
    await engine.dispose()
