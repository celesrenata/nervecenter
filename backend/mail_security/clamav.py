"""ClamAV (clamd) client for the NerveShield attachment-scanner interface.

Owner-requested amendment to Task 2.4. It implements the
:class:`~backend.mail_security.attachments.AttachmentScanner` Protocol as a
*local-only* clamd client. **Nothing here is wired into the worker, policy,
inspections or app startup**; how a ``NOT_RUN`` / incomplete scan surfaces is an
open owner policy question. ``attachments.py`` stays socket-free: this is the
single deliberate local-IPC module in ``backend/mail_security``.

NOT VERIFIED AGAINST REAL CLAMD. Neither clamd nor freshclam exist in the
development environment; this client has only been exercised against a fake
clamd server in the tests. Protocol details come from the clamd documentation
(``clamd(8)`` / ``clamd.conf(5)`` "INSTREAM", "VERSION", "PING", and the
``z`` NUL-delimited command prefix) and must be re-checked against the deployed
ClamAV version before anyone relies on this.

Protocol subset (all NUL-delimited, ``z`` prefix)
-------------------------------------------------
* ``zINSTREAM\\0`` then repeated ``<4-byte big-endian length><data>`` chunks and
  a ``<4 zero bytes>`` terminator. Reply ``stream: OK``,
  ``stream: <Name> FOUND`` or ``... ERROR`` (``INSTREAM size limit exceeded.
  ERROR`` when clamd ``StreamMaxLength`` is hit).
* ``zVERSION\\0`` -> ``ClamAV <engine>/<signature db version>/<db date>``.
* ``zPING\\0`` -> ``PONG``.

Safety rules
------------
* **Local only by construction (R-017.3, T-13 spirit).** A config is either an
  absolute UNIX socket path or a loopback IP literal (127.0.0.0/8, ``::1``;
  the exact string ``localhost`` maps to ``127.0.0.1``). Anything else (remote
  or private IPs, hostnames, URLs, empty) raises :class:`ClamdConfigError`
  at construction, with a fixed message that never echoes the input. No DNS
  lookup is ever performed. The endpoint is re-validated before every connect.
* **Never clean unless proven (I-02, I-08, T-18).** Only an exact
  ``stream: OK`` reply, a *fully sent* stream and a *fresh, parseable*
  signature database yield ``CLEAN_ENGINE_VERDICT``. Empty, truncated,
  garbled, early-closed, oversize, timeout, refused, stale-signature and
  unparseable-VERSION cases are non-clean typed outcomes; no exception escapes
  :meth:`ClamdScanner.scan` (except cancellation, which is propagated safely).
* **Stale signatures.** If the database date is older than
  ``max_signature_age`` (default 3 days), is implausibly in the future, or the
  VERSION reply cannot be parsed, a would-be clean verdict becomes
  ``UNAVAILABLE`` with reason ``SIGNATURES_STALE`` / ``SIGNATURES_UNKNOWN`` /
  ``SIGNATURES_DATE_INVALID``. A DETECTED verdict is reported regardless. The
  VERSION date is interpreted as UTC (not verified against real clamd).
* **Privacy (R-005.4, R-022).** Attachment bytes go only to the local clamd.
  They, filenames and raw reply text are never logged, stored or put in an
  exception message; the only reply-derived text is a charset-restricted,
  length-capped signature code. Engine/signature version is recorded in
  ``ScanOutcome.engine_version`` as ``<engine>-sig<db>-<YYYYMMDD>``.
* **DoS bounds (R-022).** Client-side stream cap, chunk cap, bounded reply
  length, connect/io/total timeouts, a semaphore on concurrent scans, one
  connection per scan closed in ``finally``.

Deployment requirements for a real clamd (NOT applied here)
-----------------------------------------------------------
* Run clamd unprivileged and isolated: no network egress, read-only root
  filesystem except the signature database directory.
* Reachable only over a UNIX socket or loopback inside the same pod/host; never
  expose TCP on a routable interface (this client would refuse it anyway).
* Set ``StreamMaxLength`` and ``MaxFileSize`` / ``MaxScanSize`` /
  ``MaxRecursion`` / ``MaxFiles`` consistently with ``max_stream_bytes`` here
  (StreamMaxLength >= client cap, so the client cap triggers first).
* Run freshclam to keep signatures current and alert on signature age.
  Updating signatures needs outbound access, which belongs in a *separate
  updater component*, not in the scanner's network namespace.

Open items for the owner: deployment manifest for clamd and freshclam (not
created; nothing under ``deployments/`` was touched), verification against a
real clamd, and the worker/policy wiring decision.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backend.mail_security.attachments import ScanOutcome, ScanStatus

logger = logging.getLogger(__name__)

CLAMAV_ENGINE = "clamav"

# -- reason codes (safe, fixed vocabulary) ---------------------------------------
REASON_SIGNATURES_STALE = "SIGNATURES_STALE"
REASON_SIGNATURES_UNKNOWN = "SIGNATURES_UNKNOWN"
REASON_SIGNATURES_DATE_INVALID = "SIGNATURES_DATE_INVALID"
REASON_BAD_REPLY = "CLAMD_BAD_REPLY"
REASON_SIZE_LIMIT = "CLAMD_SIZE_LIMIT"
REASON_CLAMD_ERROR = "CLAMD_ERROR_REPLY"
REASON_CONNECT_FAILED = "CLAMD_CONNECT_FAILED"
REASON_ENDPOINT_REJECTED = "CLAMD_ENDPOINT_REJECTED"
REASON_EARLY_CLOSE = "CLAMD_EARLY_CLOSE"
REASON_TIMEOUT = "CLAMD_TIMEOUT"
REASON_EMPTY_PAYLOAD = "EMPTY_PAYLOAD"
REASON_BAD_PAYLOAD = "BAD_PAYLOAD"
REASON_INTERNAL = "CLAMD_INTERNAL_ERROR"

_CMD_INSTREAM = b"zINSTREAM\0"
_CMD_VERSION = b"zVERSION\0"
_CMD_PING = b"zPING\0"
_TERMINATOR = struct.pack(">I", 0)
_MAX_CHUNK_CAP = 1024 * 1024
_MAX_SIGNATURE_CODE = 64
_FUTURE_TOLERANCE = timedelta(days=1)

_SAFE_SIG_RE = re.compile(r"[^A-Za-z0-9._-]")
_FOUND_RE = re.compile(r"stream: (.+) FOUND", re.DOTALL)
_VERSION_RE = re.compile(r"ClamAV ([0-9]+(?:\.[0-9]+){1,3}[A-Za-z0-9._-]{0,16})/([0-9]+)/(.{1,64})")
_DATE_RE = re.compile(r"[A-Za-z]{3} ([A-Za-z]{3}) (\d{1,2}) (\d{2}):(\d{2}):(\d{2}) (\d{4})")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}


class ClamdConfigError(ValueError):
    """Invalid clamd configuration. The message is fixed text plus a code; it
    never contains the rejected value (which may be a URL, host or path)."""

    def __init__(self, code: str) -> None:
        super().__init__(f"invalid clamd configuration: {code}")
        self.code = code


def _loopback_literal(host: object) -> str:
    """Return a normalized loopback IP literal or raise. Never resolves DNS."""
    if not isinstance(host, str) or not host:
        raise ClamdConfigError("HOST_INVALID")
    if host == "localhost":  # the only name accepted; mapped, not resolved
        return "127.0.0.1"
    if host != host.strip() or "%" in host:
        raise ClamdConfigError("HOST_NOT_LOOPBACK")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ClamdConfigError("HOST_NOT_LOOPBACK") from None
    if isinstance(ip, ipaddress.IPv4Address):
        ok = ip in ipaddress.ip_network("127.0.0.0/8")
    else:
        ok = ip == ipaddress.IPv6Address("::1")
    if not ok:
        raise ClamdConfigError("HOST_NOT_LOOPBACK")
    return str(ip)


@dataclass(frozen=True)
class ClamdConfig:
    """Local-only clamd endpoint plus protocol bounds.

    Exactly one of ``socket_path`` (absolute UNIX socket path) or ``host``
    (loopback literal) must be given.
    """

    socket_path: str | None = None
    host: str | None = None
    port: int = 3310
    max_stream_bytes: int = 16 * 1024 * 1024
    chunk_size: int = 64 * 1024
    max_reply_bytes: int = 1024
    connect_timeout: float = 2.0
    io_timeout: float = 10.0
    total_timeout: float = 30.0
    max_concurrent: int = 4
    max_signature_age: timedelta = timedelta(days=3)
    version_cache_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.socket_path is not None and self.host is not None:
            raise ClamdConfigError("ENDPOINT_AMBIGUOUS")
        if self.socket_path is None and self.host is None:
            raise ClamdConfigError("ENDPOINT_MISSING")
        if self.socket_path is not None:
            path = self.socket_path
            if isinstance(path, os.PathLike):
                path = os.fspath(path)
                object.__setattr__(self, "socket_path", path)
            if (
                not isinstance(path, str)
                or not path.startswith("/")
                or "\0" in path
                or len(path) < 2
            ):
                raise ClamdConfigError("SOCKET_PATH_INVALID")
        else:
            object.__setattr__(self, "host", _loopback_literal(self.host))
            if (
                not isinstance(self.port, int)
                or isinstance(self.port, bool)
                or not 1 <= self.port <= 65535
            ):
                raise ClamdConfigError("PORT_INVALID")
        self._check_bounds()

    def _check_bounds(self) -> None:
        def _pos(value: object) -> bool:
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value > 0
            )

        ints_ok = all(
            isinstance(v, int) and not isinstance(v, bool) and v > 0
            for v in (self.max_stream_bytes, self.chunk_size, self.max_reply_bytes)
        ) and (
            isinstance(self.max_concurrent, int)
            and not isinstance(self.max_concurrent, bool)
            and self.max_concurrent > 0
        )
        if not ints_ok or self.chunk_size > _MAX_CHUNK_CAP:
            raise ClamdConfigError("LIMIT_INVALID")
        if self.max_reply_bytes < 64:
            raise ClamdConfigError("LIMIT_INVALID")
        if not all(_pos(v) for v in (self.connect_timeout, self.io_timeout, self.total_timeout)):
            raise ClamdConfigError("TIMEOUT_INVALID")
        if not isinstance(self.max_signature_age, timedelta) or (
            self.max_signature_age <= timedelta(0)
        ):
            raise ClamdConfigError("SIGNATURE_AGE_INVALID")
        if (
            not isinstance(self.version_cache_seconds, (int, float))
            or isinstance(self.version_cache_seconds, bool)
            or not math.isfinite(self.version_cache_seconds)
            or self.version_cache_seconds < 0
        ):
            raise ClamdConfigError("CACHE_INVALID")


@dataclass(frozen=True)
class ClamdVersion:
    """Parsed ``VERSION`` reply."""

    engine_version: str
    signature_version: int
    signature_date: datetime  # timezone-aware, interpreted as UTC

    @property
    def token(self) -> str:
        """``<engine>-sig<db>-<YYYYMMDD>`` (fits ScanOutcome.engine_version)."""
        return f"{self.engine_version}-sig{self.signature_version}-{self.signature_date:%Y%m%d}"


@dataclass(frozen=True)
class ClamdScanOutcome(ScanOutcome):
    """A :class:`ScanOutcome` that also carries clamd-specific detail.

    ``reason_code`` is a fixed-vocabulary code (never reply text). The generic
    runner only reads the base fields; ``engine_version`` already embeds the
    signature version and date.
    """

    reason_code: str = ""
    signature_version: int | None = None
    signature_date: datetime | None = None


class _Failure(Exception):  # noqa: N818 - internal control flow, never leaves the module
    """Internal typed failure. Carries no message text."""

    def __init__(self, status: ScanStatus, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _sanitize_signature(raw: str) -> str:
    code = _SAFE_SIG_RE.sub("_", raw)[:_MAX_SIGNATURE_CODE]
    return code or "UNKNOWN"


def parse_version_reply(reply: str) -> ClamdVersion | None:
    """Parse ``ClamAV <engine>/<db>/<date>``; ``None`` if anything is off."""
    m = _VERSION_RE.fullmatch(reply)
    if m is None:
        return None
    sig_version = int(m.group(2))
    if sig_version <= 0:
        return None
    d = _DATE_RE.fullmatch(" ".join(m.group(3).split()))
    if d is None:
        return None
    month = _MONTHS.get(d.group(1).lower())
    if month is None:
        return None
    try:
        when = datetime(
            int(d.group(6)), month, int(d.group(2)),
            int(d.group(3)), int(d.group(4)), int(d.group(5)),
            tzinfo=UTC,
        )  # fmt: skip
    except ValueError:
        return None
    return ClamdVersion(m.group(1), sig_version, when)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _default_clock() -> datetime:
    return datetime.now(UTC)


class ClamdScanner:
    """``AttachmentScanner`` backed by a local clamd. See module docstring."""

    def __init__(
        self,
        config: ClamdConfig,
        *,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        if not isinstance(config, ClamdConfig):
            raise ClamdConfigError("CONFIG_TYPE")
        self._check_endpoint(config)
        self._config = config
        self._clock = clock
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._version_lock = asyncio.Lock()
        self._cached: tuple[datetime, ClamdVersion] | None = None

    # -- public API -----------------------------------------------------------

    async def scan(self, payload: bytes, *, deadline_seconds: float) -> ScanOutcome:
        """Scan ``payload``. Never raises (except cancellation); never lies."""
        try:
            if not isinstance(payload, (bytes, bytearray, memoryview)):
                return self._fail(ScanStatus.ERROR, REASON_BAD_PAYLOAD)
            size = len(payload)
            if size == 0:
                return self._fail(ScanStatus.NOT_RUN, REASON_EMPTY_PAYLOAD)
            if size > self._config.max_stream_bytes:
                return self._fail(ScanStatus.TOO_LARGE, REASON_SIZE_LIMIT)
            timeout = self._effective_timeout(deadline_seconds)
            if timeout <= 0:
                return self._fail(ScanStatus.TIMEOUT, REASON_TIMEOUT)
            return await asyncio.wait_for(self._scan_guarded(bytes(payload)), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return self._fail(ScanStatus.TIMEOUT, REASON_TIMEOUT)
        except _Failure as exc:
            return self._fail(exc.status, exc.reason)
        except Exception:  # noqa: BLE001 - contain; never surface exception text
            return self._fail(ScanStatus.ERROR, REASON_INTERNAL)

    async def ping(self) -> bool:
        """Health: ``True`` only if clamd answers exactly ``PONG``."""
        try:
            async with self._semaphore:
                reply = await asyncio.wait_for(
                    self._command(_CMD_PING), timeout=self._config.total_timeout
                )
            return reply == "PONG"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return False

    async def engine_info(self) -> ClamdVersion | None:
        """Current (briefly cached) engine/signature info, or ``None``."""
        try:
            async with self._semaphore:
                return await asyncio.wait_for(self._version(), timeout=self._config.total_timeout)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return None

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _check_endpoint(config: ClamdConfig) -> None:
        """Re-validate right before use (defends against a forged frozen config)."""
        if config.socket_path is not None:
            if (
                config.host is not None
                or not isinstance(config.socket_path, str)
                or not config.socket_path.startswith("/")
            ):
                raise ClamdConfigError("SOCKET_PATH_INVALID")
        elif _loopback_literal(config.host) != config.host:
            raise ClamdConfigError("HOST_NOT_LOOPBACK")

    def _effective_timeout(self, deadline_seconds: float) -> float:
        try:
            deadline = float(deadline_seconds)
        except (TypeError, ValueError):
            return 0.0
        if math.isnan(deadline):
            return 0.0
        return min(deadline, self._config.total_timeout)

    def _fail(self, status: ScanStatus, reason: str) -> ClamdScanOutcome:
        logger.debug("clamd scan not clean status=%s reason=%s", status.value, reason)
        return ClamdScanOutcome(status=status, engine=CLAMAV_ENGINE, reason_code=reason)

    async def _scan_guarded(self, payload: bytes) -> ScanOutcome:
        async with self._semaphore:
            reply, fully_sent = await self._instream(payload)
            return await self._interpret(reply, fully_sent)

    async def _interpret(self, reply: str, fully_sent: bool) -> ScanOutcome:
        if reply.startswith("INSTREAM size limit exceeded") and reply.endswith("ERROR"):
            return self._fail(ScanStatus.TOO_LARGE, REASON_SIZE_LIMIT)
        found = _FOUND_RE.fullmatch(reply)
        if found is not None:
            code = _sanitize_signature(found.group(1))
            info = await self._safe_version()
            return ClamdScanOutcome(
                status=ScanStatus.DETECTED,
                engine=CLAMAV_ENGINE,
                engine_version=info.token if info else "",
                signature_ids=(code,),
                signature_version=info.signature_version if info else None,
                signature_date=info.signature_date if info else None,
            )
        if reply == "stream: OK" and fully_sent:
            return await self._clean_if_signatures_fresh()
        if reply.endswith("ERROR"):
            return self._fail(ScanStatus.ERROR, REASON_CLAMD_ERROR)
        return self._fail(ScanStatus.ERROR, REASON_BAD_REPLY)

    async def _safe_version(self) -> ClamdVersion | None:
        try:
            return await self._version()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return None

    async def _clean_if_signatures_fresh(self) -> ScanOutcome:
        try:
            info = await self._version()
        except _Failure as exc:
            if exc.status is ScanStatus.TIMEOUT:
                return self._fail(ScanStatus.TIMEOUT, REASON_TIMEOUT)
            return self._fail(ScanStatus.UNAVAILABLE, REASON_SIGNATURES_UNKNOWN)
        now = _utc(self._clock())
        age = now - info.signature_date
        if age > self._config.max_signature_age:
            reason = REASON_SIGNATURES_STALE
        elif age < -_FUTURE_TOLERANCE:
            reason = REASON_SIGNATURES_DATE_INVALID
        else:
            return ClamdScanOutcome(
                status=ScanStatus.CLEAN_ENGINE_VERDICT,
                engine=CLAMAV_ENGINE,
                engine_version=info.token,
                signature_version=info.signature_version,
                signature_date=info.signature_date,
            )
        return ClamdScanOutcome(
            status=ScanStatus.UNAVAILABLE,
            engine=CLAMAV_ENGINE,
            engine_version=info.token,
            reason_code=reason,
            signature_version=info.signature_version,
            signature_date=info.signature_date,
        )

    async def _version(self) -> ClamdVersion:
        """VERSION, cached briefly. Raises :class:`_Failure` if unknown."""
        async with self._version_lock:
            now = _utc(self._clock())
            cached = self._cached
            ttl = timedelta(seconds=self._config.version_cache_seconds)
            if cached is not None and timedelta(0) <= now - cached[0] < ttl:
                return cached[1]
            reply = await self._command(_CMD_VERSION)
            info = parse_version_reply(reply)
            if info is None:
                self._cached = None
                raise _Failure(ScanStatus.UNAVAILABLE, REASON_SIGNATURES_UNKNOWN)
            self._cached = (now, info)
            return info

    # -- transport ------------------------------------------------------------

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        cfg = self._config
        try:
            self._check_endpoint(cfg)
        except ClamdConfigError:
            raise _Failure(ScanStatus.UNAVAILABLE, REASON_ENDPOINT_REJECTED) from None
        limit = cfg.max_reply_bytes
        try:
            if cfg.socket_path is not None:
                coro = asyncio.open_unix_connection(cfg.socket_path, limit=limit)
            else:
                assert cfg.host is not None  # validated by _check_endpoint above
                coro = asyncio.open_connection(cfg.host, cfg.port, limit=limit)
            return await asyncio.wait_for(coro, timeout=cfg.connect_timeout)
        except TimeoutError:
            raise _Failure(ScanStatus.TIMEOUT, REASON_TIMEOUT) from None
        except OSError:
            raise _Failure(ScanStatus.UNAVAILABLE, REASON_CONNECT_FAILED) from None

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except Exception:  # noqa: BLE001 - best effort; CancelledError still propagates
            pass

    async def _read_reply(self, reader: asyncio.StreamReader) -> str:
        """Read one NUL-terminated reply, bounded. Truncation/garble => failure."""
        cfg = self._config
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\0"), timeout=cfg.io_timeout)
        except TimeoutError:
            raise _Failure(ScanStatus.TIMEOUT, REASON_TIMEOUT) from None
        except asyncio.IncompleteReadError:
            raise _Failure(ScanStatus.ERROR, REASON_EARLY_CLOSE) from None
        except (asyncio.LimitOverrunError, ValueError):
            raise _Failure(ScanStatus.ERROR, REASON_BAD_REPLY) from None
        except OSError:
            raise _Failure(ScanStatus.ERROR, REASON_EARLY_CLOSE) from None
        body = raw[:-1]
        if len(body) > cfg.max_reply_bytes:
            raise _Failure(ScanStatus.ERROR, REASON_BAD_REPLY)
        return body.decode("utf-8", "replace")

    async def _command(self, command: bytes) -> str:
        reader, writer = await self._open()
        try:
            writer.write(command)
            await asyncio.wait_for(writer.drain(), timeout=self._config.io_timeout)
            return await self._read_reply(reader)
        except TimeoutError:
            raise _Failure(ScanStatus.TIMEOUT, REASON_TIMEOUT) from None
        except OSError:
            raise _Failure(ScanStatus.UNAVAILABLE, REASON_EARLY_CLOSE) from None
        finally:
            await self._close(writer)

    async def _instream(self, payload: bytes) -> tuple[str, bool]:
        """Send INSTREAM; return ``(reply, fully_sent)``."""
        cfg = self._config
        reader, writer = await self._open()
        fully_sent = False
        try:
            try:
                writer.write(_CMD_INSTREAM)
                view = memoryview(payload)
                for start in range(0, len(view), cfg.chunk_size):
                    chunk = view[start : start + cfg.chunk_size]
                    writer.write(struct.pack(">I", len(chunk)))
                    writer.write(chunk)
                    await asyncio.wait_for(writer.drain(), timeout=cfg.io_timeout)
                writer.write(_TERMINATOR)
                await asyncio.wait_for(writer.drain(), timeout=cfg.io_timeout)
                fully_sent = True
            except TimeoutError:
                raise _Failure(ScanStatus.TIMEOUT, REASON_TIMEOUT) from None
            except OSError:
                # clamd may have replied (e.g. size limit) and closed; read it.
                fully_sent = False
            reply = await self._read_reply(reader)
            return reply, fully_sent
        finally:
            await self._close(writer)


__all__ = [
    "CLAMAV_ENGINE",
    "ClamdConfig",
    "ClamdConfigError",
    "ClamdScanOutcome",
    "ClamdScanner",
    "ClamdVersion",
    "parse_version_reply",
]
