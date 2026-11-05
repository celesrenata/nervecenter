"""Tests for the clamd client (owner-requested Task 2.4 amendment).

FAKE CLAMD ONLY. Nothing here talks to a real ClamAV (none is installed), so
real-clamd behavior is NOT VERIFIED. The fake speaks the documented subset
(zINSTREAM / zVERSION / zPING, NUL-delimited) over a real local UNIX socket and
a real loopback TCP socket.

This module deliberately does NOT install the autouse socket-block guard used by
the parser/inspections tests: it needs real *local* sockets. Instead it proves
the client refuses every non-loopback destination before any connection attempt.

Covers R-005.4, R-017.3, R-022, I-02, I-08, T-08, T-13 (spirit), T-18.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
import shutil
import socket
import struct
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from backend.mail_security import clamav as clam
from backend.mail_security.attachments import (
    AttachmentScanner,
    ScanStatus,
    run_scan,
    summarize_scans,
)
from backend.mail_security.clamav import (
    ClamdConfig,
    ClamdConfigError,
    ClamdScanner,
    parse_version_reply,
)
from backend.mail_security.models import FindingStatus

CANARY = "CANARYZXQ9"
NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
FRESH_VERSION = b"ClamAV 1.4.2/27800/Thu Apr 30 08:00:00 2026\0"
STALE_VERSION = b"ClamAV 1.4.2/25000/Mon Jan  1 08:00:00 2024\0"
OK = b"stream: OK\0"
REF = "part:attachment:0"


# =============================================================================
# fake clamd
# =============================================================================


@dataclass
class FakeClamd:
    """Scriptable fake clamd. ``mode`` controls the INSTREAM behaviour."""

    mode: str = "reply"  # reply | hang | close_early | size_limit | size_limit_rst
    instream_reply: bytes = OK
    version_reply: bytes | None = FRESH_VERSION  # None => hang on VERSION
    ping_reply: bytes = b"PONG\0"
    accept_delay: float = 0.0
    hold: float = 0.0  # keep INSTREAM connections open this long before replying
    stream_limit: int = 1000
    # observations
    transcripts: list[bytes] = field(default_factory=list)  # raw INSTREAM bytes
    commands: list[bytes] = field(default_factory=list)
    total_conns: int = 0
    open_conns: int = 0
    max_open: int = 0
    client_eof: int = 0
    server: asyncio.AbstractServer | None = None
    host: str = "127.0.0.1"
    port: int = 0
    path: str = ""

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.total_conns += 1
        self.open_conns += 1
        self.max_open = max(self.max_open, self.open_conns)
        try:
            if self.accept_delay:
                await asyncio.sleep(self.accept_delay)
            command = await reader.readuntil(b"\0")
            self.commands.append(command)
            if command == b"zPING\0":
                writer.write(self.ping_reply)
                await writer.drain()
            elif command == b"zVERSION\0":
                if self.version_reply is None:
                    await reader.read()
                else:
                    writer.write(self.version_reply)
                    await writer.drain()
                    if not self.version_reply.endswith(b"\0"):
                        writer.close()
            elif command == b"zINSTREAM\0":
                await self._instream(command, reader, writer)
            await reader.read()  # wait for the client to close its end
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if reader.at_eof():
                self.client_eof += 1
            self.open_conns -= 1
            writer.close()

    async def _instream(
        self, command: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        raw = bytearray(command)
        if self.mode == "close_early":
            self.transcripts.append(bytes(raw))
            writer.close()  # hang up without reading the stream or replying
            return
        total = 0
        while True:
            header = await reader.readexactly(4)
            raw += header
            (length,) = struct.unpack(">I", header)
            if length == 0:
                break
            raw += await reader.readexactly(length)
            total += length
            if self.mode.startswith("size_limit") and total > self.stream_limit:
                self.transcripts.append(bytes(raw))
                writer.write(b"INSTREAM size limit exceeded. ERROR\0")
                await writer.drain()
                if self.mode == "size_limit_rst":
                    return  # close immediately with unread data
                await reader.read()  # polite: keep draining until the client closes
                return
        self.transcripts.append(bytes(raw))
        if self.hold:
            await asyncio.sleep(self.hold)
        if self.mode == "hang":
            await reader.read()
            return
        writer.write(self.instream_reply)
        await writer.drain()
        if not self.instream_reply.endswith(b"\0"):
            writer.close()  # truncated/empty reply: the daemon went away mid-reply


@pytest.fixture
def sock_dir(tmp_path: Path) -> AsyncIterator[str]:
    """Short directory: macOS AF_UNIX paths are limited to ~104 bytes."""
    if len(str(tmp_path)) < 60:
        yield str(tmp_path)
        return
    d = tempfile.mkdtemp(prefix="nc", dir="/tmp")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest_asyncio.fixture(params=["unix", "tcp"])
async def fake(request: pytest.FixtureRequest, sock_dir: str) -> AsyncIterator[FakeClamd]:
    f = FakeClamd()
    if request.param == "unix":
        f.path = f"{sock_dir}/clamd.sock"
        f.server = await asyncio.start_unix_server(f._handle, path=f.path)
    else:
        f.server = await asyncio.start_server(f._handle, "127.0.0.1", 0)
        f.port = f.server.sockets[0].getsockname()[1]
    try:
        yield f
    finally:
        f.server.close()
        await f.server.wait_closed()


def cfg(f: FakeClamd, **kw: object) -> ClamdConfig:
    base: dict[str, object] = {"io_timeout": 2.0, "connect_timeout": 2.0, "total_timeout": 5.0}
    base.update(kw)
    if f.path:
        return ClamdConfig(socket_path=f.path, **base)  # type: ignore[arg-type]
    return ClamdConfig(host=f.host, port=f.port, **base)  # type: ignore[arg-type]


def scanner(
    f: FakeClamd, clock: Callable[[], datetime] | None = None, **kw: object
) -> ClamdScanner:
    return ClamdScanner(cfg(f, **kw), clock=clock or (lambda: NOW))


async def wait_closed(f: FakeClamd) -> None:
    for _ in range(100):
        if f.open_conns == 0 and f.client_eof == f.total_conns:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"sockets not closed: open={f.open_conns} eof={f.client_eof}/{f.total_conns}"
    )


def assert_not_clean(outcome: object) -> None:
    assert getattr(outcome, "status") is not ScanStatus.CLEAN_ENGINE_VERDICT  # noqa: B009


# =============================================================================
# clean / detected / framing
# =============================================================================


async def test_ok_with_fresh_signatures_is_clean_and_records_versions(fake: FakeClamd) -> None:
    s = scanner(fake)
    assert isinstance(s, AttachmentScanner)
    out = await s.scan(b"hello", deadline_seconds=5)
    assert out.status is ScanStatus.CLEAN_ENGINE_VERDICT
    assert out.engine == "clamav" and out.engine_version == "1.4.2-sig27800-20260430"
    assert out.signature_ids == ()
    assert out.signature_version == 27800 and out.signature_date == datetime(  # type: ignore[attr-defined]
        2026, 4, 30, 8, tzinfo=UTC
    )
    rec = await run_scan(s, b"hello", evidence_ref=REF, timeout_seconds=5)
    assert rec.clean_by_engine and not rec.incomplete
    assert rec.engine == "clamav" and rec.engine_version == "1.4.2-sig27800-20260430"
    assert rec.finding.explanation_code == "ENGINE_CLEAN_VERDICT_NOT_SAFETY"
    await wait_closed(fake)


async def test_exact_instream_framing(fake: FakeClamd) -> None:
    payload = bytes(range(256)) * 1 + b"Z" * 44  # 300 bytes, 3 chunks of <=128
    out = await scanner(fake, chunk_size=128).scan(payload, deadline_seconds=5)
    assert out.status is ScanStatus.CLEAN_ENGINE_VERDICT
    expected = (
        b"zINSTREAM\0"
        + struct.pack(">I", 128) + payload[:128]
        + struct.pack(">I", 128) + payload[128:256]
        + struct.pack(">I", 44) + payload[256:]
        + struct.pack(">I", 0)
    )  # fmt: skip
    assert fake.transcripts == [expected]
    assert fake.commands == [b"zINSTREAM\0", b"zVERSION\0"]


async def test_payload_exact_multiple_of_chunk_has_no_zero_length_data_chunk(
    fake: FakeClamd,
) -> None:
    await scanner(fake, chunk_size=64).scan(b"a" * 128, deadline_seconds=5)
    t = fake.transcripts[0]
    assert t == b"zINSTREAM\0" + (struct.pack(">I", 64) + b"a" * 64) * 2 + struct.pack(">I", 0)


@pytest.mark.parametrize(
    "name",
    [
        "Eicar-Test-Signature",
        "Win.Test.EICAR_HDB-1",
        "A" * 500,
        "evil\nstream: OK",
        "ctl\x01\x1b[31m\x7fchars",
        "ünï\u202ecode\u200b",
        "../../etc/passwd",
        "tab\tand space and 'quotes' <b>",
    ],
)
async def test_found_is_detected_with_sanitized_bounded_code(fake: FakeClamd, name: str) -> None:
    fake.instream_reply = f"stream: {name} FOUND\0".encode()
    s = scanner(fake)
    out = await s.scan(b"x", deadline_seconds=5)
    assert out.status is ScanStatus.DETECTED
    (code,) = out.signature_ids
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,64}", code), code
    rec = await run_scan(s, b"x", evidence_ref=REF, timeout_seconds=5)
    assert rec.status is ScanStatus.DETECTED and not rec.clean_by_engine
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,40}", rec.signature_ids[0])
    assert rec.engine_version == "1.4.2-sig27800-20260430"
    assert "\n" not in code and name not in rec.finding.model_dump_json() or name == code


async def test_found_even_with_stale_signatures_is_still_detected(fake: FakeClamd) -> None:
    fake.instream_reply = b"stream: Eicar-Test-Signature FOUND\0"
    fake.version_reply = STALE_VERSION
    out = await scanner(fake).scan(b"x", deadline_seconds=5)
    assert out.status is ScanStatus.DETECTED and out.signature_ids == ("Eicar-Test-Signature",)


async def test_found_with_unknown_version_is_detected_without_version(fake: FakeClamd) -> None:
    fake.instream_reply = b"stream: Bad.Sig FOUND\0"
    fake.version_reply = b"nonsense\0"
    out = await scanner(fake).scan(b"x", deadline_seconds=5)
    assert out.status is ScanStatus.DETECTED and out.engine_version == ""


# =============================================================================
# non-clean outcomes (I-02, I-08, T-18)
# =============================================================================


@pytest.mark.parametrize(
    ("reply", "status"),
    [
        (b"stream: OK", ScanStatus.ERROR),  # truncated: no NUL terminator
        (b"", ScanStatus.ERROR),  # empty reply, connection closed
        (b"\0", ScanStatus.ERROR),  # empty reply
        (b"garbage\0", ScanStatus.ERROR),
        (b"OK\0", ScanStatus.ERROR),
        (b"stream:  OK\0", ScanStatus.ERROR),
        (b"stream: OK \0", ScanStatus.ERROR),
        (b"STREAM: OK\0", ScanStatus.ERROR),
        (b"stream: OK\nstream: OK\0", ScanStatus.ERROR),
        (b"stream: SECRETREPLY ERROR\0", ScanStatus.ERROR),
        (b"Empty file ERROR\0", ScanStatus.ERROR),
        (b"stream: Foo FOUN\0", ScanStatus.ERROR),
        (b"x" * 5000 + b"\0", ScanStatus.ERROR),  # reply over the bound
        (b"x" * 5000, ScanStatus.ERROR),  # unterminated and over the bound
        (b"INSTREAM size limit exceeded. ERROR\0", ScanStatus.TOO_LARGE),
    ],
)
async def test_bad_replies_are_non_clean_and_runner_marks_incomplete(
    fake: FakeClamd, reply: bytes, status: ScanStatus
) -> None:
    fake.instream_reply = reply
    s = scanner(fake)
    out = await s.scan(b"payload", deadline_seconds=5)
    assert out.status is status
    rec = await run_scan(s, b"payload", evidence_ref=REF, timeout_seconds=5)
    assert rec.status is status and rec.incomplete and not rec.clean_by_engine
    assert rec.finding.rule_id == "ATTACHMENT_SCAN_INCOMPLETE"
    assert rec.finding.status is FindingStatus.INCOMPLETE
    summary = summarize_scans([rec])
    assert not summary.complete and summary.engine_clean == 0
    await wait_closed(fake)


async def test_early_close_is_error_not_clean(fake: FakeClamd) -> None:
    fake.mode = "close_early"
    s = scanner(fake)
    out = await s.scan(b"x" * 100_000, deadline_seconds=5)
    assert_not_clean(out)
    assert out.status in (ScanStatus.ERROR, ScanStatus.UNAVAILABLE)
    rec = await run_scan(s, b"x", evidence_ref=REF, timeout_seconds=5)
    assert rec.incomplete and not rec.clean_by_engine


async def test_hang_times_out_and_closes_socket(fake: FakeClamd) -> None:
    fake.mode = "hang"
    s = scanner(fake, io_timeout=0.2)
    out = await s.scan(b"x", deadline_seconds=5)
    assert out.status is ScanStatus.TIMEOUT
    await wait_closed(fake)


async def test_total_deadline_is_honoured_through_runner(fake: FakeClamd) -> None:
    fake.mode = "hang"
    s = scanner(fake)
    out = await s.scan(b"x", deadline_seconds=0.2)
    assert out.status is ScanStatus.TIMEOUT
    rec = await run_scan(s, b"x", evidence_ref=REF, timeout_seconds=0.2)
    assert rec.status is ScanStatus.TIMEOUT and rec.incomplete and not rec.clean_by_engine
    await wait_closed(fake)


async def test_slow_accept_times_out(fake: FakeClamd) -> None:
    fake.accept_delay = 2.0
    out = await scanner(fake, io_timeout=0.2).scan(b"x", deadline_seconds=5)
    assert out.status is ScanStatus.TIMEOUT


@pytest.mark.parametrize("deadline", [0, -1, float("nan")])
async def test_non_positive_or_nan_deadline_is_timeout(fake: FakeClamd, deadline: float) -> None:
    out = await scanner(fake).scan(b"x", deadline_seconds=deadline)
    assert out.status is ScanStatus.TIMEOUT and fake.total_conns == 0


async def test_server_stream_limit_reply_is_too_large(fake: FakeClamd) -> None:
    fake.mode = "size_limit"
    fake.stream_limit = 1000
    out = await scanner(fake, chunk_size=256).scan(b"x" * 600_000, deadline_seconds=5)
    assert out.status is ScanStatus.TOO_LARGE


async def test_server_closing_mid_stream_is_never_clean(fake: FakeClamd) -> None:
    fake.mode = "size_limit_rst"
    out = await scanner(fake, chunk_size=256).scan(b"x" * 2_000_000, deadline_seconds=5)
    assert_not_clean(out)
    assert out.status in (ScanStatus.TOO_LARGE, ScanStatus.ERROR)


async def test_client_side_cap_never_connects(fake: FakeClamd) -> None:
    s = scanner(fake, max_stream_bytes=100)
    assert (await s.scan(b"x" * 101, deadline_seconds=5)).status is ScanStatus.TOO_LARGE
    assert fake.total_conns == 0
    assert (await s.scan(b"x" * 100, deadline_seconds=5)).status is ScanStatus.CLEAN_ENGINE_VERDICT


async def test_runner_byte_cap_never_calls_scanner(fake: FakeClamd) -> None:
    rec = await run_scan(scanner(fake), b"x" * 50, evidence_ref=REF, max_bytes=10)
    assert rec.status is ScanStatus.TOO_LARGE and fake.total_conns == 0


async def test_empty_and_wrong_type_payload_never_clean(fake: FakeClamd) -> None:
    s = scanner(fake)
    assert (await s.scan(b"", deadline_seconds=5)).status is ScanStatus.NOT_RUN
    assert (await s.scan("text", deadline_seconds=5)).status is ScanStatus.ERROR  # type: ignore[arg-type]
    assert fake.total_conns == 0


async def test_connection_refused_or_missing_socket_is_unavailable(sock_dir: str) -> None:
    missing = ClamdScanner(ClamdConfig(socket_path=f"{sock_dir}/nope.sock"), clock=lambda: NOW)
    out = await missing.scan(b"x", deadline_seconds=5)
    assert out.status is ScanStatus.UNAVAILABLE
    with socket.socket() as probe:  # grab a free loopback port, then close it
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    refused = ClamdScanner(ClamdConfig(host="127.0.0.1", port=port), clock=lambda: NOW)
    assert (await refused.scan(b"x", deadline_seconds=5)).status is ScanStatus.UNAVAILABLE
    rec = await run_scan(refused, b"x", evidence_ref=REF, timeout_seconds=5)
    assert rec.status is ScanStatus.UNAVAILABLE and rec.incomplete and not rec.clean_by_engine
    assert rec.finding.explanation_code == "SCANNER_UNAVAILABLE"


async def test_internal_exception_is_contained_never_clean(
    fake: FakeClamd, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def boom(self: ClamdScanner, payload: bytes) -> tuple[str, bool]:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(ClamdScanner, "_instream", boom)
    out = await scanner(fake).scan(CANARY.encode(), deadline_seconds=5)
    assert out.status is ScanStatus.ERROR
    assert CANARY not in repr(out) and CANARY not in caplog.text


# =============================================================================
# signatures: stale / unknown (the key rule)
# =============================================================================


async def test_stale_signatures_never_clean(fake: FakeClamd) -> None:
    fake.version_reply = STALE_VERSION
    s = scanner(fake)
    out = await s.scan(b"x", deadline_seconds=5)
    assert out.status is ScanStatus.UNAVAILABLE
    assert out.reason_code == "SIGNATURES_STALE"  # type: ignore[attr-defined]
    assert out.engine_version == "1.4.2-sig25000-20240101"
    rec = await run_scan(s, b"x", evidence_ref=REF, timeout_seconds=5)
    assert rec.incomplete and not rec.clean_by_engine
    assert rec.finding.rule_id == "ATTACHMENT_SCAN_INCOMPLETE"


@pytest.mark.parametrize(
    "version",
    [
        b"ClamAV 1.4.2\0",  # no database info
        b"garbage\0",
        b"\0",
        b"ClamAV 1.4.2/0/Thu Apr 30 08:00:00 2026\0",  # db version 0 = no database
        b"ClamAV 1.4.2/27800/Thu Foo 30 08:00:00 2026\0",
        b"ClamAV 1.4.2/27800/not a date\0",
        b"ClamAV 1.4.2/27800/Thu Feb 31 08:00:00 2026\0",
        b"ClamAV 1.4.2/abc/Thu Apr 30 08:00:00 2026\0",
        b"clamav 1.4.2/27800/Thu Apr 30 08:00:00 2026\0",
        b"ClamAV 1.4.2/27800/Thu Apr 30 08:00:00 2026" + b"x" * 5000 + b"\0",
        b"ClamAV 1.4.2/27800/Thu Apr 30 08:00:00 2026",  # truncated
        b"",
    ],
)
async def test_unparseable_version_never_clean(fake: FakeClamd, version: bytes) -> None:
    fake.version_reply = version
    s = scanner(fake)
    out = await s.scan(b"x", deadline_seconds=5)
    assert out.status is ScanStatus.UNAVAILABLE
    assert out.reason_code == "SIGNATURES_UNKNOWN"  # type: ignore[attr-defined]
    rec = await run_scan(s, b"x", evidence_ref=REF, timeout_seconds=5)
    assert rec.incomplete and not rec.clean_by_engine


async def test_future_dated_signatures_never_clean(fake: FakeClamd) -> None:
    fake.version_reply = b"ClamAV 1.4.2/27800/Mon Jan  1 00:00:00 2035\0"
    out = await scanner(fake).scan(b"x", deadline_seconds=5)
    assert out.status is ScanStatus.UNAVAILABLE
    assert out.reason_code == "SIGNATURES_DATE_INVALID"  # type: ignore[attr-defined]


async def test_version_hang_is_not_clean(fake: FakeClamd) -> None:
    fake.version_reply = None
    out = await scanner(fake, io_timeout=0.2).scan(b"x", deadline_seconds=5)
    assert out.status is ScanStatus.TIMEOUT


async def test_max_signature_age_boundary_uses_injected_clock(fake: FakeClamd) -> None:
    db_date = datetime(2026, 4, 30, 8, tzinfo=UTC)
    limit = timedelta(days=3)
    at_limit = scanner(fake, clock=lambda: db_date + limit, max_signature_age=limit)
    assert (await at_limit.scan(b"x", deadline_seconds=5)).status is ScanStatus.CLEAN_ENGINE_VERDICT
    over = scanner(
        fake, clock=lambda: db_date + limit + timedelta(seconds=1), max_signature_age=limit
    )
    assert (await over.scan(b"x", deadline_seconds=5)).status is ScanStatus.UNAVAILABLE


async def test_version_cache_and_staleness_checked_at_scan_time(fake: FakeClamd) -> None:
    now = [NOW]
    s = scanner(
        fake, clock=lambda: now[0], version_cache_seconds=60, max_signature_age=timedelta(days=3)
    )
    assert (await s.scan(b"x", deadline_seconds=5)).status is ScanStatus.CLEAN_ENGINE_VERDICT
    assert (await s.scan(b"y", deadline_seconds=5)).status is ScanStatus.CLEAN_ENGINE_VERDICT
    assert fake.commands.count(b"zVERSION\0") == 1  # cached
    now[0] = NOW + timedelta(seconds=61)
    await s.scan(b"z", deadline_seconds=5)
    assert fake.commands.count(b"zVERSION\0") == 2  # cache expired
    # cached info + clock advances past the age limit => stale without a refresh
    s2 = scanner(fake, clock=lambda: now[0], version_cache_seconds=10**9)
    now[0] = NOW
    assert (await s2.scan(b"x", deadline_seconds=5)).status is ScanStatus.CLEAN_ENGINE_VERDICT
    now[0] = NOW + timedelta(days=10)
    assert (await s2.scan(b"x", deadline_seconds=5)).status is ScanStatus.UNAVAILABLE


async def test_ping_and_engine_info(fake: FakeClamd) -> None:
    s = scanner(fake)
    assert await s.ping() is True
    info = await s.engine_info()
    assert info is not None and info.token == "1.4.2-sig27800-20260430"
    fake.ping_reply = b"nope\0"
    assert await s.ping() is False
    fake.version_reply = b"junk\0"
    assert await ClamdScanner(cfg(fake), clock=lambda: NOW).engine_info() is None


def test_parse_version_reply_unit() -> None:
    v = parse_version_reply("ClamAV 1.0.1/27123/Mon Jan  1 08:00:00 2024")
    assert v is not None and v.signature_version == 27123
    assert v.signature_date == datetime(2024, 1, 1, 8, tzinfo=UTC)
    assert parse_version_reply("ClamAV 1.0.1") is None


# =============================================================================
# concurrency, cancellation, sockets
# =============================================================================


async def test_concurrency_is_bounded(fake: FakeClamd) -> None:
    fake.hold = 0.1
    s = scanner(fake, max_concurrent=2)
    outs = await asyncio.gather(*(s.scan(b"x" * (i + 1), deadline_seconds=10) for i in range(6)))
    assert all(o.status is ScanStatus.CLEAN_ENGINE_VERDICT for o in outs)
    assert fake.max_open == 2
    assert len(fake.transcripts) == 6
    await wait_closed(fake)


async def test_each_scan_uses_its_own_connection_and_closes_it(fake: FakeClamd) -> None:
    s = scanner(fake, version_cache_seconds=0)
    for _ in range(3):
        await s.scan(b"x", deadline_seconds=5)
    assert fake.total_conns == 6  # INSTREAM + VERSION per scan, never reused
    await wait_closed(fake)


async def test_cancellation_is_safe_and_releases_resources(fake: FakeClamd) -> None:
    fake.mode = "hang"
    s = scanner(fake, max_concurrent=1)
    task = asyncio.create_task(s.scan(b"x", deadline_seconds=30))
    for _ in range(100):
        if fake.open_conns:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_closed(fake)
    fake.mode = "reply"
    out = await asyncio.wait_for(s.scan(b"x", deadline_seconds=5), 5)  # semaphore was released
    assert out.status is ScanStatus.CLEAN_ENGINE_VERDICT


# =============================================================================
# local-only by construction (R-017.3, T-13 spirit)
# =============================================================================

BAD_HOSTS: list[object] = [
    "8.8.8.8", "1.1.1.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.1.1",
    "0.0.0.0", "::", "fe80::1", "2001:db8::1", "::ffff:127.0.0.1", "::ffff:8.8.8.8",
    "example.com", "clamd", "clamd.svc.cluster.local", "localhost.example.com",
    "LOCALHOST", "localhost.", "http://127.0.0.1:3310", "tcp://127.0.0.1", "127.0.0.1:3310",
    "127.0.0.1/8", "", " ", " 127.0.0.1", "127.0.0.1 ", "127.0.0.1\n", "::1%lo", "[::1]",
    "127.1", "2130706433", "0x7f.0.0.1", "127.0.0.1.example.com", None, 5, b"127.0.0.1",
]  # fmt: skip


@pytest.fixture
def no_connect(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any connection / DNS primitive being touched fails the test."""
    touched: list[str] = []

    def trap(name: str) -> Callable[..., object]:
        def _t(*_a: object, **_k: object) -> object:
            touched.append(name)
            raise AssertionError(f"{name} must not be called")

        return _t

    monkeypatch.setattr(asyncio, "open_connection", trap("open_connection"))
    monkeypatch.setattr(asyncio, "open_unix_connection", trap("open_unix_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", trap("getaddrinfo"))
    monkeypatch.setattr(socket, "gethostbyname", trap("gethostbyname"))
    monkeypatch.setattr(socket, "create_connection", trap("create_connection"))
    return touched


@pytest.mark.parametrize("host", BAD_HOSTS, ids=[repr(h)[:30] for h in BAD_HOSTS])
def test_non_loopback_hosts_rejected_at_construction_before_any_connection(
    host: object, no_connect: list[str]
) -> None:
    with pytest.raises(ClamdConfigError) as exc:
        ClamdConfig(host=host)  # type: ignore[arg-type]
    assert isinstance(exc.value, ValueError) and exc.value.code
    assert repr(host) not in str(exc.value) or not isinstance(host, str) or host == ""
    assert no_connect == []


@pytest.mark.parametrize(
    ("host", "normalized"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("127.255.255.254", "127.255.255.254"),
        ("::1", "::1"),
        ("localhost", "127.0.0.1"),
    ],
)
def test_loopback_hosts_accepted_and_normalized(host: str, normalized: str) -> None:
    assert ClamdConfig(host=host).host == normalized


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"socket_path": ""},
        {"socket_path": "relative/clamd.sock"},
        {"socket_path": "unix:///run/clamd.sock"},
        {"socket_path": "http://evil.example/x"},
        {"socket_path": "/run/clam\0d.sock"},
        {"socket_path": "/"},
        {"socket_path": "/run/clamd.sock", "host": "127.0.0.1"},
        {"host": "127.0.0.1", "port": 0},
        {"host": "127.0.0.1", "port": 70000},
        {"host": "127.0.0.1", "port": True},
        {"host": "127.0.0.1", "chunk_size": 0},
        {"host": "127.0.0.1", "chunk_size": 2 * 1024 * 1024},
        {"host": "127.0.0.1", "max_stream_bytes": 0},
        {"host": "127.0.0.1", "max_reply_bytes": 8},
        {"host": "127.0.0.1", "max_concurrent": 0},
        {"host": "127.0.0.1", "io_timeout": 0},
        {"host": "127.0.0.1", "connect_timeout": float("nan")},
        {"host": "127.0.0.1", "total_timeout": float("inf")},
        {"host": "127.0.0.1", "max_signature_age": timedelta(0)},
        {"host": "127.0.0.1", "version_cache_seconds": -1},
    ],
)
def test_empty_ambiguous_and_out_of_bounds_config_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ClamdConfigError):
        ClamdConfig(**kwargs)  # type: ignore[arg-type]


def test_config_error_never_echoes_value() -> None:
    secret = f"http://{CANARY}.example.com/x"
    with pytest.raises(ClamdConfigError) as exc:
        ClamdConfig(host=secret)
    assert CANARY not in str(exc.value) and CANARY not in repr(exc.value)
    with pytest.raises(ClamdConfigError) as exc2:
        ClamdConfig(socket_path=f"{CANARY}/x")
    assert CANARY not in str(exc2.value)


def test_scanner_requires_a_validated_config() -> None:
    with pytest.raises(ClamdConfigError):
        ClamdScanner({"host": "8.8.8.8"})  # type: ignore[arg-type]


async def test_forged_config_is_refused_before_any_connection(no_connect: list[str]) -> None:
    config = ClamdConfig(host="127.0.0.1")
    s = ClamdScanner(config, clock=lambda: NOW)
    object.__setattr__(config, "host", "8.8.8.8")  # bypass the frozen dataclass
    out = await s.scan(b"private", deadline_seconds=5)
    assert out.status is ScanStatus.UNAVAILABLE
    assert out.reason_code == "CLAMD_ENDPOINT_REJECTED"  # type: ignore[attr-defined]
    assert no_connect == []
    with pytest.raises(ClamdConfigError):
        ClamdScanner(config)


async def test_no_dns_lookup_even_for_localhost(
    fake: FakeClamd, monkeypatch: pytest.MonkeyPatch
) -> None:
    if fake.path:
        pytest.skip("tcp only")

    def trap(*_a: object, **_k: object) -> object:
        raise AssertionError("DNS lookup attempted")

    monkeypatch.setattr(socket, "getaddrinfo", trap)
    monkeypatch.setattr(socket, "gethostbyname", trap)
    s = ClamdScanner(ClamdConfig(host="localhost", port=fake.port), clock=lambda: NOW)
    assert (await s.scan(b"x", deadline_seconds=5)).status is ScanStatus.CLEAN_ENGINE_VERDICT


async def test_only_loopback_peer_is_ever_contacted(fake: FakeClamd) -> None:
    if fake.path:
        pytest.skip("tcp only")
    peers: list[str] = []
    real = asyncio.open_connection

    async def spy(host: str, port: int, **kw: object) -> tuple[object, object]:
        peers.append(host)
        return await real(host, port, **kw)  # type: ignore[arg-type]

    orig = asyncio.open_connection
    asyncio.open_connection = spy  # type: ignore[assignment]
    try:
        await scanner(fake).scan(b"x", deadline_seconds=5)
    finally:
        asyncio.open_connection = orig
    assert peers and set(peers) == {"127.0.0.1"}


# =============================================================================
# privacy (R-017.3, R-022)
# =============================================================================


async def test_bytes_filenames_and_reply_text_never_leak(
    fake: FakeClamd, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = CANARY.encode() * 10
    secret_reply = b"stream: SECRETREPLYTEXT ERROR\0"
    blobs: list[str] = []
    for mode, reply, version in [
        ("reply", secret_reply, FRESH_VERSION),
        ("reply", OK, STALE_VERSION),
        ("reply", OK, b"SECRETVERSIONTEXT\0"),
        ("reply", b"SECRETGARBLE\0", FRESH_VERSION),
        ("close_early", OK, FRESH_VERSION),
        ("hang", OK, FRESH_VERSION),
        ("reply", OK, FRESH_VERSION),
    ]:
        fake.mode, fake.instream_reply, fake.version_reply = mode, reply, version
        s = scanner(fake, io_timeout=0.2)
        out = await s.scan(payload, deadline_seconds=3)
        rec = await run_scan(
            s, payload, evidence_ref=f"part:{CANARY}:0".replace(CANARY, "x"), timeout_seconds=3
        )
        blobs += [repr(out), repr(rec), rec.finding.model_dump_json()]
    text = caplog.text + "".join(blobs)
    for secret in (CANARY, "SECRETREPLYTEXT", "SECRETVERSIONTEXT", "SECRETGARBLE"):
        assert secret not in text, secret


# =============================================================================
# static: the single deliberate local-IPC module
# =============================================================================


def test_clamav_module_imports_no_remote_capable_libraries() -> None:
    tree = ast.parse(Path(clam.__file__).read_text(encoding="utf-8"))
    banned = {"http", "urllib", "requests", "httpx", "aiohttp", "ssl", "subprocess", "socket"}
    banned |= {"smtplib", "ftplib", "telnetlib", "websockets", "dns", "ctypes"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & banned
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in banned
    called = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert not called & {
        "getaddrinfo",
        "gethostbyname",
        "create_subprocess_exec",
        "system",
        "Popen",
    }
    # the only connection primitives are asyncio's loopback / unix streams
    assert {"open_connection", "open_unix_connection"} <= called
    assert not called & {"create_connection", "create_datagram_endpoint", "start_server"}
