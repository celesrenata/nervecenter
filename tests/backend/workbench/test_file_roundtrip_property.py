"""Property 6: file save and read round trip.

Saving a text file through the file worker (PUT /write) then reading it back
(GET /read) returns identical content, including mixed ``\\n`` and ``\\r\\n``
line endings and the trailing-newline state, up to 2 MiB. The bytes on disk
must also equal the UTF-8 encoding of the saved text.

Validates: Requirements 4.14, 11.6
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import httpx
from hypothesis import HealthCheck, given, settings, strategies as st

from backend.workbench.fsworker import create_app
from backend.workbench.validation import MAX_OPEN_BYTES

SLUG = "proj"
REL = f"{SLUG}/src/file.txt"

# No surrogates (not encodable as UTF-8). NUL and control chars are allowed.
_chars = st.characters(blacklist_categories=("Cs",), blacklist_characters="\r\n")
_line = st.text(_chars, max_size=60)
_eol = st.sampled_from(["\n", "\r\n"])


@st.composite
def mixed_text(draw) -> str:
    lines = draw(st.lists(_line, max_size=40))
    out = []
    for line in lines:
        out.append(line)
        out.append(draw(_eol))
    text = "".join(out)
    # Trailing newline state: sometimes strip it, sometimes add a final partial line.
    tail = draw(st.sampled_from(["keep", "strip", "partial"]))
    if tail == "strip":
        text = text.rstrip("\r\n")
    elif tail == "partial":
        text += draw(_line)
    return text


async def _roundtrip(text: str) -> tuple[str, bytes]:
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, SLUG, "src"))
        app = create_app("opencode", root)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://wb") as client:
            put = await client.put("/write", json={"path": REL, "content": text})
            assert put.status_code == 200, put.text
            assert put.json()["size"] == len(text.encode("utf-8"))
            got = await client.get("/read", params={"path": REL})
            assert got.status_code == 200, got.text
            body = got.json()
            with open(os.path.join(root, SLUG, "src", "file.txt"), "rb") as fh:
                disk = fh.read()
            assert body["size"] == len(disk)
            return body["content"], disk


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(text=mixed_text())
def test_save_read_roundtrip_mixed_line_endings(text: str) -> None:
    content, disk = asyncio.run(_roundtrip(text))
    assert content == text
    assert content.encode("utf-8") == disk == text.encode("utf-8")


_big_unit = st.sampled_from(["a\n", "bb\r\n", "é\n", "日本\r\n", "x", "\r\n\r\n"])


@settings(max_examples=6, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.large_base_example])
@given(
    unit=_big_unit,
    trailing=st.sampled_from(["", "\n", "\r\n"]),
    pad=st.integers(min_value=0, max_value=3),
)
def test_save_read_roundtrip_up_to_two_mib(unit: str, trailing: str, pad: int) -> None:
    unit_len = len(unit.encode("utf-8"))
    count = (MAX_OPEN_BYTES - len(trailing) - pad) // unit_len
    text = unit * count + "z" * pad + trailing
    assert len(text.encode("utf-8")) <= MAX_OPEN_BYTES
    content, disk = asyncio.run(_roundtrip(text))
    assert content == text
    assert disk == text.encode("utf-8")


def test_req_11_6_round_trip_preserves_crlf_and_trailing_newline() -> None:
    """Req 11.6: a saved file reads back byte-identical (CRLF, LF, no trailing newline)."""
    cases = [
        "one\r\ntwo\nthree\r\n",
        "one\r\ntwo\nthree",
        "\r\n",
        "\n",
        "",
        "no newline at all",
        "mixed\r\n\nlone\rcr\n",
    ]
    for text in cases:
        content, disk = asyncio.run(_roundtrip(text))
        assert content == text
        assert disk == text.encode("utf-8")
