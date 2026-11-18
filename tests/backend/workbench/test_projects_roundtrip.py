"""Property 3: Project create then read round trip and store placement.

Validates: Requirements 1.6, 1.7, 2.2, 2.15, 11.6
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import CheckConstraint
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.persistence.models import CodingHandoff
from backend.persistence.models.workbench import WorkbenchProject, WorkbenchSession
from backend.workbench.fsworker import create_app
from backend.workbench.projects import ProjectService
from backend.workbench.store import StoreClient
from backend.workbench.validation import KINDS

slugs = st.from_regex(r"[a-z0-9][a-z0-9-]{0,62}", fullmatch=True)
titles = st.text(min_size=1, max_size=120).filter(lambda t: t.strip() != "" and "\x00" not in t)
kinds = st.sampled_from(sorted(KINDS))


class _Env:
    """Real fsworker apps over temp dirs plus in-memory SQLite."""

    def __init__(self, base: Path) -> None:
        self.roots = {k: base / k for k in KINDS}
        for root in self.roots.values():
            root.mkdir()
        self.stores = {
            k: StoreClient(
                k,
                "http://worker.test",
                transport=httpx.ASGITransport(app=create_app(k, self.roots[k])),
            )
            for k in KINDS
        }
        self.engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        self.service = ProjectService(
            async_sessionmaker(self.engine, expire_on_commit=False), self.stores
        )

    async def setup(self) -> None:
        tables = [
            WorkbenchProject.__table__,
            WorkbenchSession.__table__,
            CodingHandoff.__table__,
        ]
        # PostgreSQL-only CHECK constraints (regex, char_length) do not parse in SQLite.
        for table in tables:
            for c in [c for c in table.constraints if isinstance(c, CheckConstraint)]:
                table.constraints.discard(c)
        async with self.engine.begin() as conn:
            for table in tables:
                await conn.run_sync(table.create)

    async def close(self) -> None:
        await self.engine.dispose()

    def dirs(self, kind: str) -> list[str]:
        return sorted(p.name for p in self.roots[kind].iterdir() if not p.name.startswith("."))


async def _roundtrip(slug: str, title: str, kind: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = _Env(Path(tmp))
        await env.setup()
        try:
            created = await env.service.create(slug=slug, title=title, kind=kind)
            read = await env.service.get(kind, slug)
            listed = [p for p in await env.service.list() if p["id"] == created["id"]]

            assert (read["slug"], read["title"], read["kind"]) == (slug, title, kind)
            assert len(listed) == 1
            assert listed[0] == created
            for key, value in created.items():
                assert read[key] == value

            other = next(k for k in KINDS if k != kind)
            assert (env.roots[kind] / slug).is_dir()
            assert not (env.roots[other] / slug).exists()
            assert env.dirs(kind) == [slug]
            assert env.dirs(other) == []
        finally:
            await env.close()


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(slugs, titles, kinds)
def test_create_then_read_round_trip_and_store_placement(slug: str, title: str, kind: str) -> None:
    asyncio.run(_roundtrip(slug, title, kind))


def test_project_created_saved_and_read_back_is_identical() -> None:
    """Req 11.6: a Project created, saved and read back returns identical content."""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            await env.setup()
            try:
                for kind in sorted(KINDS):
                    created = await env.service.create(
                        slug="demo-1", title="Démo ✓ project", kind=kind
                    )
                    first = await env.service.get(kind, "demo-1")
                    second = await env.service.get(kind, "demo-1")
                    assert first == second
                    for key, value in created.items():
                        assert first[key] == value
                    assert first["title"] == "Démo ✓ project"
                    assert (env.roots[kind] / "demo-1").is_dir()
            finally:
                await env.close()

    asyncio.run(run())
