"""Property 5 (backend part): project list ordering and archived inclusion.
Feature: opencode-menagerie-workbench, Property 5: Project list ordering and grouping
**Validates: Requirements 2.1, 3.3, 3.4**
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.persistence.models.workbench import WorkbenchProject
from backend.workbench.projects import ProjectService

_BASE = datetime(2024, 1, 1, tzinfo=UTC)

# Raw DDL: the model's CHECK constraints use PostgreSQL-only char_length and ~.
_DDL = (
    "CREATE TABLE workbench_projects (id TEXT PRIMARY KEY, slug TEXT NOT NULL, "
    "kind TEXT NOT NULL, title TEXT NOT NULL, created_at TIMESTAMP NOT NULL, "
    "last_activity_at TIMESTAMP NOT NULL, size_bytes INTEGER NOT NULL DEFAULT 0, "
    "archived BOOLEAN NOT NULL DEFAULT 0, push_approved BOOLEAN NOT NULL DEFAULT 0, "
    "UNIQUE (slug, kind))"
)


class _StubStore:
    """No filesystem: list() never calls the store."""

    async def mkproject(self, slug):  # pragma: no cover
        return {}

    async def clone(self, slug, git_url):  # pragma: no cover
        return {}

    async def rmproject(self, slug, confirm):  # pragma: no cover
        return {}

    async def dirs(self):  # pragma: no cover
        return []


# (kind, archived) per project; activity offsets are distinct so order is unambiguous.
_entries = st.lists(
    st.tuples(st.sampled_from(["opencode", "menagerie"]), st.booleans()),
    max_size=15,
)


async def _list_for(entries, offsets):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_DDL))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            for i, ((kind, archived), off) in enumerate(zip(entries, offsets, strict=True)):
                at = _BASE + timedelta(seconds=off)
                session.add(
                    WorkbenchProject(
                        slug=f"p{i}",
                        kind=kind,
                        title=f"t{i}",
                        created_at=_BASE,
                        last_activity_at=at,
                        size_bytes=0,
                        archived=archived,
                        push_approved=False,
                    )
                )
            await session.commit()
        stores = {"opencode": _StubStore(), "menagerie": _StubStore()}
        return await ProjectService(factory, stores).list()
    finally:
        await engine.dispose()


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(data=st.data(), entries=_entries)
def test_list_returns_all_projects_ordered_by_last_activity_desc(data, entries):
    offsets = data.draw(
        st.lists(
            st.integers(0, 10_000_000),
            min_size=len(entries),
            max_size=len(entries),
            unique=True,
        )
    )
    result = asyncio.run(_list_for(entries, offsets))

    # Every project is present, archived ones included.
    assert len(result) == len(entries)
    assert {r["slug"] for r in result} == {f"p{i}" for i in range(len(entries))}
    expected_archived = {f"p{i}" for i, (_, a) in enumerate(entries) if a}
    assert {r["slug"] for r in result if r["archived"]} == expected_archived

    # Ordered by last_activity_at descending.
    stamps = [datetime.fromisoformat(r["last_activity_at"]) for r in result]
    assert stamps == sorted(stamps, reverse=True)


def test_list_empty():
    assert asyncio.run(_list_for([], [])) == []
