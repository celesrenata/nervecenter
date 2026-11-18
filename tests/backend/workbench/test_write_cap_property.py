"""Property 26: Write concurrency cap.
Validates: Requirements 9.12.
"""
from __future__ import annotations

import asyncio
import os
import threading

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backend.workbench import fsworker

CAP = 4


async def _run(tmp_path, monkeypatch, n: int, n_other: int) -> None:
    root = tmp_path / "store"
    # One subdirectory per write: the worker flocks the target directory, so
    # same-directory writes would serialize before reaching the gated step.
    for proj in ("p1", "p2"):
        for i in list(range(n_other + 8)) + list(range(100, 100 + CAP)):
            (root / proj / f"d{i}").mkdir(parents=True)
    gate = threading.Event()
    lock = threading.Lock()
    entered = {"count": 0}
    real_replace = os.replace

    def slow_replace(src, dst, *a, **kw):
        with lock:
            entered["count"] += 1
        gate.wait(timeout=20)
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(fsworker.os, "replace", slow_replace)
    app = fsworker.create_app("menagerie", str(root))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        def put(proj: str, i: int):
            return asyncio.ensure_future(
                c.put("/write", json={"path": f"{proj}/d{i}/f.txt", "content": f"c{i}"})
            )

        try:
            tasks = [put("p1", i) for i in range(n)]
            admitted = min(n, CAP)
            rejected = n - admitted
            # Wait for admitted writes to be in progress and rejections to return.
            for _ in range(400):
                done = [t for t in tasks if t.done()]
                if entered["count"] >= admitted and len(done) >= rejected:
                    break
                await asyncio.sleep(0.01)
            assert entered["count"] == admitted
            done = [t for t in tasks if t.done()]
            assert len(done) == rejected
            for t in done:
                r = t.result()
                assert r.status_code == 429
                assert r.json()["detail"]["error"] == "concurrency_limit"

            # Another project is unaffected by p1 being saturated.
            other = [put("p2", i) for i in range(n_other)]
            for _ in range(400):
                if entered["count"] >= admitted + n_other:
                    break
                await asyncio.sleep(0.01)
            assert entered["count"] == admitted + n_other
            assert not any(t.done() for t in other)
        finally:
            gate.set()

        results = await asyncio.gather(*tasks, *other)
        statuses = [r.status_code for r in results]
        assert statuses.count(200) == admitted + n_other
        assert statuses.count(429) == rejected
        for r in results:
            if r.status_code == 200:
                assert r.json()["size"] > 0

        # Permits are released: a full cap of new writes to p1 is admitted again.
        again = await asyncio.gather(*[put("p1", 100 + i) for i in range(CAP)])
        assert [r.status_code for r in again] == [200] * CAP


@settings(max_examples=6, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(n=st.integers(min_value=1, max_value=8), n_other=st.integers(min_value=1, max_value=3))
async def test_write_cap_property(tmp_path_factory, monkeypatch, n, n_other):
    with pytest.MonkeyPatch.context() as mp:
        await _run(tmp_path_factory.mktemp("cap"), mp, n, n_other)


@pytest.mark.parametrize("n", [4, 5, 8])
async def test_write_cap_boundaries(tmp_path, monkeypatch, n):
    await _run(tmp_path, monkeypatch, n, 2)
