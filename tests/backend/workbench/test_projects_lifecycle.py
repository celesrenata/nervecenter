"""Property 4: Lifecycle operations preserve files.
Feature: opencode-menagerie-workbench, Property 4: Lifecycle operations preserve files
**Validates: Requirements 2.9, 2.10, 2.11, 2.12, 2.13**
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import asynccontextmanager

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.workbench.fsworker import create_app
from backend.workbench.projects import ProjectService, ProjectValidationError
from backend.workbench.store import StoreClient

KINDS = ("opencode", "menagerie")

_DDL = (
    "CREATE TABLE workbench_projects (id CHAR(32) PRIMARY KEY, slug TEXT NOT NULL, "
    "kind TEXT NOT NULL, title TEXT NOT NULL, created_at TIMESTAMP NOT NULL, "
    "last_activity_at TIMESTAMP NOT NULL, size_bytes BIGINT NOT NULL DEFAULT 0, "
    "archived BOOLEAN NOT NULL DEFAULT 0, push_approved BOOLEAN NOT NULL DEFAULT 0, "
    "UNIQUE (slug, kind))",
    "CREATE TABLE workbench_sessions (id TEXT PRIMARY KEY, project_id CHAR(32) NOT NULL, "
    "title TEXT, status TEXT, updated_at TIMESTAMP, model TEXT, small_model TEXT)",
    "CREATE TABLE workbench_ide (project_id CHAR(32) PRIMARY KEY, pod_name TEXT, "
    "state TEXT, last_traffic_at TIMESTAMP, started_at TIMESTAMP)",
)

_slug = st.from_regex(r"[a-z0-9][a-z0-9-]{0,15}", fullmatch=True)
_fname = st.from_regex(r"[a-z0-9]{1,8}(\.[a-z]{1,3})?", fullmatch=True)
_files = st.dictionaries(_fname, st.binary(max_size=64), max_size=4)
_slugs = st.dictionaries(_slug, _files, min_size=2, max_size=4)  # slug -> files


@asynccontextmanager
async def _env():
    with tempfile.TemporaryDirectory() as tmp:
        roots = {}
        stores = {}
        for kind in KINDS:
            roots[kind] = os.path.join(tmp, kind)
            os.mkdir(roots[kind])
            transport = httpx.ASGITransport(app=create_app(kind, roots[kind]))
            stores[kind] = StoreClient(kind, "http://worker", transport=transport)
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        try:
            async with engine.begin() as conn:
                for ddl in _DDL:
                    await conn.execute(text(ddl))
            factory = async_sessionmaker(engine, expire_on_commit=False)
            yield ProjectService(factory, stores), roots
        finally:
            await engine.dispose()


def _write(root: str, slug: str, files: dict[str, bytes]) -> None:
    os.makedirs(os.path.join(root, slug), exist_ok=True)
    for name, data in files.items():
        with open(os.path.join(root, slug, name), "wb") as fh:
            fh.write(data)


def _snapshot(root: str) -> dict[str, bytes | None]:
    snap: dict[str, bytes | None] = {}
    for dirpath, dirs, files in os.walk(root):
        for d in dirs:
            snap[os.path.relpath(os.path.join(dirpath, d), root)] = None
        for f in files:
            full = os.path.join(dirpath, f)
            with open(full, "rb") as fh:
                snap[os.path.relpath(full, root)] = fh.read()
    return snap


def _snapshots(roots: dict[str, str]) -> dict[str, dict]:
    return {k: _snapshot(r) for k, r in roots.items()}


async def _seed(svc: ProjectService, roots, kind: str, data: dict):
    """First slug registered via create, the rest left as unregistered dirs."""
    registered, *others = list(data)
    await svc.create(slug=registered, title=registered, kind=kind)
    _write(roots[kind], registered, data[registered])
    for slug in others:
        _write(roots[kind], slug, data[slug])
    return registered, others


_settings = settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)


@_settings
@given(kind=st.sampled_from(KINDS), data=_slugs)
def test_archive_list_import_never_modify_files(kind, data):
    async def run():
        async with _env() as (svc, roots):
            registered, others = await _seed(svc, roots, kind, data)
            before = _snapshots(roots)

            archived = await svc.archive(kind, registered)
            assert archived["archived"] is True
            assert _snapshots(roots) == before

            unreg = await svc.list_unregistered()
            assert {(u["kind"], u["slug"]) for u in unreg} == {(kind, s) for s in others}
            assert _snapshots(roots) == before

            for slug in others:
                rec = await svc.import_dir(kind=kind, slug=slug)
                assert rec["slug"] == slug
            assert _snapshots(roots) == before
            assert await svc.list_unregistered() == []

    asyncio.run(run())


@_settings
@given(
    kind=st.sampled_from(KINDS),
    data=_slugs,
    wrong=st.one_of(st.none(), st.text(max_size=20)),
)
def test_delete_without_correct_confirmation_changes_nothing(kind, data, wrong):
    async def run():
        async with _env() as (svc, roots):
            registered, _ = await _seed(svc, roots, kind, data)
            if wrong == registered:
                wrong_value = registered + "x"
            else:
                wrong_value = wrong
            before = _snapshots(roots)
            records = await svc.list()

            with pytest.raises(ProjectValidationError):
                await svc.delete(kind, registered, wrong_value)

            assert _snapshots(roots) == before
            assert await svc.list() == records

    asyncio.run(run())


@_settings
@given(kind=st.sampled_from(KINDS), data=_slugs)
def test_delete_with_correct_confirmation_removes_root_and_record(kind, data):
    async def run():
        async with _env() as (svc, roots):
            registered, _ = await _seed(svc, roots, kind, data)
            before = _snapshots(roots)

            await svc.delete(kind, registered, registered)

            after = _snapshots(roots)
            assert not os.path.lexists(os.path.join(roots[kind], registered))
            assert all(p.split(os.sep)[0] != registered for p in after[kind])
            assert not [p for p in await svc.list() if p["slug"] == registered]
            # Everything outside the deleted root is untouched.
            expected = {
                p: v for p, v in before[kind].items() if p.split(os.sep)[0] != registered
            }
            assert after[kind] == expected
            other = next(k for k in KINDS if k != kind)
            assert after[other] == before[other]

    asyncio.run(run())
