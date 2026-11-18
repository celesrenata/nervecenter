"""Property 7: Stale saves are rejected.

**Validates: Requirements 4.5**
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

import httpx
from hypothesis import HealthCheck, given, settings, strategies as st

from backend.workbench import fsworker

_text = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=200
)


async def _scenario(original: str, new: str, offset: int) -> None:
    root = tempfile.mkdtemp(prefix="wb-stale-")
    try:
        os.makedirs(os.path.join(root, "proj"))
        target = os.path.join(root, "proj", "f.txt")
        with open(target, "wb") as fh:
            fh.write(original.encode())
        before = os.stat(target).st_mtime_ns
        transport = httpx.ASGITransport(app=fsworker.create_app("menagerie", root))
        async with httpx.AsyncClient(transport=transport, base_url="http://w") as c:
            # Stale base: always differs from the current mtime.
            r = await c.put(
                "/write",
                json={"path": "proj/f.txt", "content": new, "base_mtime_ns": before + offset},
            )
            assert r.status_code == 409
            body = r.json()
            detail = body.get("detail", body)
            assert detail["error"] == "conflict"
            assert detail["current_mtime_ns"] == before
            with open(target, "rb") as fh:
                assert fh.read() == original.encode()
            assert os.stat(target).st_mtime_ns == before

            # Matching base succeeds and replaces the content.
            r = await c.put(
                "/write",
                json={"path": "proj/f.txt", "content": new, "base_mtime_ns": before},
            )
            assert r.status_code == 200
            with open(target, "rb") as fh:
                assert fh.read() == new.encode()
            assert r.json()["mtime_ns"] == os.stat(target).st_mtime_ns
    finally:
        shutil.rmtree(root, ignore_errors=True)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    original=_text,
    new=_text,
    offset=st.integers(min_value=-10**12, max_value=10**12).filter(lambda n: n != 0),
)
def test_stale_save_rejected_matching_save_succeeds(original: str, new: str, offset: int) -> None:
    asyncio.run(_scenario(original, new, offset))
