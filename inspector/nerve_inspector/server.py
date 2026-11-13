"""UNIX-socket front end of the static inspector sidecar (NS-ADR-086).

Wire format (one request per connection)::

    request:  b"NSI1" + uint32-be length + sample bytes
    response: uint32-be length + JSON report

Each sample is parsed in a fresh child (:mod:`nerve_inspector.child`) with
rlimits (CPU seconds, address space, file size 0) and a wall-clock deadline;
a child that overruns is killed and reported as ``TIMEOUT`` or
``RESOURCE_LIMIT``. At most :data:`MAX_CONCURRENT` children run at once.
The server never reads or logs sample content.
"""
from __future__ import annotations

import asyncio
import json
import os
import resource
import signal
import struct
import subprocess
import sys

from nerve_inspector.analyze import DEFAULT_LIMITS, REPORT_VERSION

MAGIC = b"NSI1"
SOCKET_PATH = os.environ.get("NERVE_INSPECTOR_SOCKET", "/run/inspector/inspector.sock")
WALL_SECONDS = float(os.environ.get("NERVE_INSPECTOR_WALL_SECONDS", "10"))
CPU_SECONDS = int(os.environ.get("NERVE_INSPECTOR_CPU_SECONDS", "8"))
MEMORY_BYTES = int(os.environ.get("NERVE_INSPECTOR_MEMORY_BYTES", str(768 * 1024 * 1024)))
MAX_CONCURRENT = 2
MAX_OUTPUT = 256 * 1024
_SLOTS: asyncio.Semaphore | None = None


def _incomplete(reason: str) -> dict:
    return {"v": REPORT_VERSION, "kind": "not_applicable", "complete": False,
            "incomplete_reasons": [reason], "codes": [], "counts": {}, "link_hosts": []}


def _limit_child() -> None:  # runs in the child between fork and exec
    """Strict on Linux (the sidecar): any failed limit aborts the spawn, which
    the caller reports as INTERNAL_ERROR. Elsewhere (developer macOS, which has
    no RLIMIT_AS) unsupported limits are skipped."""
    strict = sys.platform.startswith("linux")
    for which, value in (
        (resource.RLIMIT_CPU, CPU_SECONDS),
        (resource.RLIMIT_AS, MEMORY_BYTES),
        (resource.RLIMIT_FSIZE, 0),
        (resource.RLIMIT_CORE, 0),
    ):
        try:
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError):
            if strict:
                raise


async def inspect(sample: bytes) -> dict:
    """Parse one sample in a sandboxed child; always returns a report dict."""
    assert _SLOTS is not None  # noqa: S101
    async with _SLOTS:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-s", "-B", "-m", "nerve_inspector.child",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, preexec_fn=_limit_child,
                env={"PYTHONPATH": os.environ.get("PYTHONPATH", ""), "PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.SubprocessError):
            return _incomplete("INTERNAL_ERROR")  # never run a sample unbounded
        try:
            out, _ = await asyncio.wait_for(proc.communicate(sample), WALL_SECONDS)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return _incomplete("TIMEOUT")
        if proc.returncode in (-signal.SIGXCPU, -signal.SIGKILL, -signal.SIGSEGV):
            return _incomplete("RESOURCE_LIMIT" if proc.returncode != -signal.SIGXCPU
                               else "TIMEOUT")
        if not out or len(out) > MAX_OUTPUT:
            return _incomplete("INTERNAL_ERROR")
        try:
            report = json.loads(out)
        except ValueError:
            return _incomplete("INTERNAL_ERROR")
        return report if isinstance(report, dict) else _incomplete("INTERNAL_ERROR")


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(reader.readexactly(8), 5)
        if head[:4] != MAGIC:
            return
        (length,) = struct.unpack(">I", head[4:])
        if length > DEFAULT_LIMITS.max_input_bytes:
            report = _incomplete("TOO_LARGE")
        else:
            sample = await asyncio.wait_for(reader.readexactly(length), 30)
            report = await inspect(sample)
        body = json.dumps(report, separators=(",", ":")).encode()
        writer.write(struct.pack(">I", len(body)) + body)
        await writer.drain()
    except (asyncio.IncompleteReadError, TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()


async def serve(path: str = SOCKET_PATH) -> None:
    global _SLOTS
    _SLOTS = asyncio.Semaphore(MAX_CONCURRENT)
    if os.path.exists(path):
        os.unlink(path)
    old = os.umask(0o007)  # socket usable by the pod's shared group only
    try:
        server = await asyncio.start_unix_server(_handle, path=path)
    finally:
        os.umask(old)
    print("nerve-inspector listening", flush=True)  # noqa: T201 - no content
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(serve())
