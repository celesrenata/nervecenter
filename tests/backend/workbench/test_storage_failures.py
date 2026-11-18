"""Failure-injection tests for the Workbench storage layer.

Validates: Requirements 1.8, 2.8, 9.4.
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx
import pytest

from backend.workbench import fsworker, health
from backend.workbench.projects import ProjectService
from backend.workbench.store import StoreClient, StoreUnavailable

URL = "https://example.invalid/repo.git"

# Stand-in for `git clone -- URL TMP`: leaves a partial tree, then fails or hangs.
_FAKE_GIT = (
    "import os, sys, time\n"
    "tmp = sys.argv[2]\n"
    "os.makedirs(os.path.join(tmp, '.git'))\n"
    "open(os.path.join(tmp, 'partial.txt'), 'w').write('x')\n"
    "if sys.argv[1] == 'hang':\n"
    "    time.sleep(30)\n"
    "sys.stderr.write('fatal: boom')\n"
    "sys.exit(128)\n"
)


class _FakeSession:
    def __init__(self, db: "_FakeDb") -> None:
        self._db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_a, **_k):
        class _R:
            def scalar_one_or_none(self):
                return None

        return _R()

    def add(self, obj) -> None:
        self._db.added.append(obj)

    async def commit(self) -> None:
        self._db.commits += 1

    async def rollback(self) -> None:
        pass

    async def refresh(self, _obj) -> None:
        pass


class _FakeDb:
    """Session factory that records what would be persisted."""

    def __init__(self) -> None:
        self.added: list = []
        self.commits = 0

    def __call__(self) -> _FakeSession:
        return _FakeSession(self)


def _worker_client(kind: str, root, **kw) -> StoreClient:
    app = fsworker.create_app(kind, str(root))
    return StoreClient(
        kind,
        "http://worker",
        transport=httpx.ASGITransport(app=app),
        **kw,
    )


def _timeout_client(kind: str) -> StoreClient:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    return StoreClient(kind, "http://worker", transport=httpx.MockTransport(boom))


@pytest.fixture
def roots(tmp_path):
    oc = tmp_path / "opencode"
    mg = tmp_path / "menagerie"
    oc.mkdir()
    mg.mkdir()
    return oc, mg


@pytest.fixture
def fake_git(monkeypatch):
    """Route the worker's `git clone` to the stand-in script; mode picked per test."""
    mode = {"value": "fail"}
    real = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kwargs):
        tmp = args[-1]
        return await real(
            sys.executable, "-c", _FAKE_GIT, mode["value"], tmp, **kwargs
        )

    monkeypatch.setattr(fsworker.asyncio, "create_subprocess_exec", fake_exec)
    return mode


async def test_store_timeout_leaves_other_store_usable_and_no_partial_root(roots):
    oc_root, mg_root = roots
    db = _FakeDb()
    svc = ProjectService(
        db,
        {"opencode": _timeout_client("opencode"), "menagerie": _worker_client("menagerie", mg_root)},
    )

    with pytest.raises(StoreUnavailable) as err:
        await svc.create(slug="alpha", title="Alpha", kind="opencode")
    assert err.value.kind == "opencode"
    assert db.added == []
    assert os.listdir(oc_root) == []

    created = await svc.create(slug="beta", title="Beta", kind="menagerie")
    assert created["slug"] == "beta"
    assert (mg_root / "beta").is_dir()
    assert len(db.added) == 1


async def test_clone_failure_removes_partial_directory_and_records_nothing(roots, fake_git):
    _oc, mg_root = roots
    fake_git["value"] = "fail"
    db = _FakeDb()
    svc = ProjectService(db, {"menagerie": _worker_client("menagerie", mg_root)})

    with pytest.raises(StoreUnavailable):
        await svc.create(slug="gamma", title="Gamma", kind="menagerie", git_url=URL)

    assert os.listdir(mg_root) == []
    assert db.added == [] and db.commits == 0


async def test_clone_timeout_removes_partial_directory_and_records_nothing(
    roots, fake_git, monkeypatch
):
    _oc, mg_root = roots
    fake_git["value"] = "hang"
    monkeypatch.setattr(fsworker, "CLONE_TIMEOUT_S", 0.5)
    db = _FakeDb()
    svc = ProjectService(db, {"menagerie": _worker_client("menagerie", mg_root)})

    with pytest.raises(StoreUnavailable) as err:
        await svc.create(slug="delta", title="Delta", kind="menagerie", git_url=URL)
    assert "504" in err.value.reason

    assert os.listdir(mg_root) == []
    assert db.added == [] and db.commits == 0


async def test_worker_reports_clone_timeout_code(roots, fake_git, monkeypatch):
    _oc, mg_root = roots
    fake_git["value"] = "hang"
    monkeypatch.setattr(fsworker, "CLONE_TIMEOUT_S", 0.5)
    transport = httpx.ASGITransport(app=fsworker.create_app("menagerie", str(mg_root)))
    async with httpx.AsyncClient(transport=transport, base_url="http://w") as c:
        resp = await c.post("/clone", json={"slug": "eps", "git_url": URL})
    assert resp.status_code == 504
    assert resp.json()["detail"]["error"] == "clone_timeout"
    assert os.listdir(mg_root) == []


async def test_health_probe_timeout_reports_store_unmounted(roots, monkeypatch):
    _oc, mg_root = roots
    monkeypatch.setattr(health, "PROBE_TIMEOUT_S", 0.05)

    class Hanging(StoreClient):
        async def probe(self):
            await asyncio.sleep(30)
            return {}

    clients = {
        "opencode": Hanging("opencode", "http://worker"),
        "menagerie": _worker_client("menagerie", mg_root),
    }
    report = await health.store_health(clients)

    assert report["opencode"] == {"mounted": False, "writable": False, "free_bytes": None}
    assert report["menagerie"]["mounted"] is True
    assert report["menagerie"]["writable"] is True
