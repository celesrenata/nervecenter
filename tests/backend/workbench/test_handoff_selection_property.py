"""Property 27 (Python part): handoff selection for a project.

Feature: opencode-menagerie-workbench, Property 27: Handoff selection and the Browser IDE link
**Validates: Requirements 3.5, 10.3, 10.4**
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.persistence.models.workbench import WorkbenchProject
from backend.workbench.projects import RECENT_HANDOFFS, ProjectService

_BASE = datetime(2024, 1, 1, tzinfo=UTC)
_KIND = "opencode"
_SLUG = "target"

# Raw DDL: the models' CHECK constraints / server defaults are PostgreSQL-only.
_DDL = (
    "CREATE TABLE workbench_projects (id TEXT PRIMARY KEY, slug TEXT NOT NULL, "
    "kind TEXT NOT NULL, title TEXT NOT NULL, created_at TIMESTAMP NOT NULL, "
    "last_activity_at TIMESTAMP NOT NULL, size_bytes INTEGER NOT NULL DEFAULT 0, "
    "archived BOOLEAN NOT NULL DEFAULT 0, push_approved BOOLEAN NOT NULL DEFAULT 0, "
    "UNIQUE (slug, kind))",
    "CREATE TABLE workbench_sessions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
    "title TEXT, status TEXT, updated_at TIMESTAMP, model TEXT, small_model TEXT)",
    "CREATE TABLE coding_handoffs (id TEXT PRIMARY KEY, created_at TIMESTAMP NOT NULL, "
    "updated_at TIMESTAMP NOT NULL, target TEXT NOT NULL, runner TEXT NOT NULL, "
    "title TEXT NOT NULL, brief TEXT NOT NULL, project_slug TEXT, repo TEXT, "
    "requested_by TEXT NOT NULL, status TEXT NOT NULL, push_approved BOOLEAN NOT NULL, "
    "claimed_by TEXT, claimed_at TIMESTAMP, finished_at TIMESTAMP, result_summary TEXT, "
    "error TEXT, session_ref TEXT)",
)


class _StubStore:
    pass


# (matches project?, other-target?) -> rows for the project, other slug, other kind.
_rows = st.lists(
    st.sampled_from(["mine", "other_slug", "other_target"]), min_size=0, max_size=30
)


async def _run(kinds, offsets):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    try:
        async with engine.begin() as conn:
            for ddl in _DDL:
                await conn.execute(text(ddl))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        expected = []
        async with factory() as session:
            session.add(
                WorkbenchProject(
                    slug=_SLUG,
                    kind=_KIND,
                    title="t",
                    created_at=_BASE,
                    last_activity_at=_BASE,
                    size_bytes=0,
                    archived=False,
                    push_approved=False,
                )
            )
            for which, off in zip(kinds, offsets, strict=True):
                at = _BASE + timedelta(seconds=off)
                hid = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO coding_handoffs (id, created_at, updated_at, target, "
                        "runner, title, brief, project_slug, requested_by, status, "
                        "push_approved) VALUES (:id, :at, :at, :target, 'central', :title, "
                        "'b', :slug, 'owner', 'queued', 0)"
                    ),
                    {
                        "id": str(hid),
                        "at": at.replace(tzinfo=None).isoformat(sep=" "),
                        "target": "menagerie" if which == "other_target" else _KIND,
                        "title": f"h{off}",
                        "slug": "other" if which == "other_slug" else _SLUG,
                    },
                )
                if which == "mine":
                    expected.append((off, str(hid)))
            await session.commit()

        async def snapshot():
            async with engine.connect() as conn:
                res = await conn.execute(text("SELECT * FROM coding_handoffs ORDER BY id"))
                return [tuple(r) for r in res]

        before = await snapshot()
        svc = ProjectService(factory, {_KIND: _StubStore(), "menagerie": _StubStore()})
        result = await svc.get(_KIND, _SLUG)
        after = await snapshot()
        return result["handoffs"], expected, before, after
    finally:
        await engine.dispose()


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data(), kinds=_rows)
def test_get_returns_ten_newest_handoffs_newest_first_without_writes(data, kinds):
    offsets = data.draw(
        st.lists(
            st.integers(0, 10_000),
            min_size=len(kinds),
            max_size=len(kinds),
            unique=True,
        )
    )
    handoffs, expected, before, after = asyncio.run(_run(kinds, offsets))

    assert len(handoffs) <= RECENT_HANDOFFS
    expected.sort(key=lambda e: e[0], reverse=True)
    want = [hid.replace("-", "") for _, hid in expected[:RECENT_HANDOFFS]]
    got = [h["id"].replace("-", "") for h in handoffs]
    assert got == want
    assert before == after
