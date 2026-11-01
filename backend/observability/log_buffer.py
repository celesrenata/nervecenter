"""Bounded in-memory log ring buffer for the in-service Logs screen.

The ring buffer is the single source of truth behind ``GET /api/logs`` and
the web ``/logs`` view (``web/src/views/LogsView.tsx``; see ``web/docs/LOGGING.md``).
Each captured record carries:

- ``level``     — normalized level name (``debug``…``critical``);
- ``timestamp`` — ISO-8601 UTC (the record's own ``event_time`` when the
                  structlog pipeline supplied one, wall-clock otherwise);
- ``logger``    — logger name (or ``root`` for un-named stdlib records);
- ``message``   — the rendered event message, truncated to
                  :data:`MAX_MESSAGE_CHARS`.

Capture paths
-------------
Two independent paths feed the same buffer, so the Logs screen stays
populated no matter which pipeline the server ends up with:

1. **structlog events** — :func:`capture_event` is anchored into the
   structlog processor pipeline by :func:`install_log_capture` (after
   ``merge_contextvars`` / ``add_log_level``, before the terminal
   renderer). A guard re-anchors it if a later ``structlog.configure()``
   replaces the pipeline.
2. **standard-library logging** — a :class:`_Handler` attached to the root
   logger mirrors every record that propagates to it, which is how the
   **uvicorn access/error log records** (which never pass through structlog)
   are captured. Because uvicorn's ``dictConfig`` runs *before* the FastAPI
   lifespan, :func:`install_log_capture` is invoked from the lifespan (see
   :mod:`backend.api.app`) so the handler survives uvicorn's re-configuration.

Secrets are never stored: :func:`_scrub` redacts inline credential-looking
patterns (``Bearer …``, ``sk-…``, long hex/base64 blobs) and the whole event
message is replaced with a redaction notice when it carries a suspicious
key (``password``, ``token``, ``secret``, …). The endpoint surfaces the
``secrets_scrubbed`` flag so consumers can verify the guarantee.
"""
from __future__ import annotations

import logging
import re
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from time import time
from typing import Any, Mapping, Sequence, cast

import structlog

__all__ = [
    "DEFAULT_CAPACITY",
    "DEFAULT_LIMIT",
    "MAX_MESSAGE_CHARS",
    "LogRecord",
    "LogBuffer",
    "get_log_buffer",
    "install_log_capture",
    "capture_event",
]

#: Ring buffer capacity — at most this many records are retained.
DEFAULT_CAPACITY = 500
#: Default entry count returned by ``GET /api/logs`` when no ``limit`` is
#: given; never exceeds the buffer capacity.
DEFAULT_LIMIT = 100
#: Per-entry message truncation bound (characters).
MAX_MESSAGE_CHARS = 500

#: Normalized level name → ascending severity.
_LEVEL_ORDER = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
    "critical": 50,
}
#: stdlib numeric level → normalized name.
_STD_LEVEL_TO_NAME = {
    10: "debug",
    20: "info",
    25: "warning",
    30: "error",
    40: "critical",
}

#: Keys whose mere presence marks the whole event as sensitive.
_SECRET_KEY_RE = re.compile(
    r"(secret|password|passwd|credential|authorization|access_token|api_key|"
    r"apikey|refresh_token|client_secret|private_key|token|bearer)",
    re.IGNORECASE,
)

#: Inline patterns redacted from message bodies (best effort).
_SECRET_VALUE_RES = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9]{12,}\b"),
    re.compile(r"(?<![\w=])[A-Za-z0-9+/]{32,}={0,2}(?![\w=])"),  # long hex/base64
)


def get_log_buffer() -> LogBuffer:
    """Return the process-wide :class:`LogBuffer` singleton."""
    global _buffer
    if _buffer is None:
        with _buffer_lock:
            if _buffer is None:
                _buffer = LogBuffer()
    return _buffer


# ── redaction ──────────────────────────────────────────────────────────────


def _scrub(message: str) -> str:
    """Redact credential-looking patterns from a rendered message."""
    out = message
    for pattern in _SECRET_VALUE_RES:
        out = pattern.sub("[redacted]", out)
    return out


def _is_sensitive(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key))


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_timestamp(value: Any, record_time: float | None = None) -> str:
    """Prefer an ISO ``event_time``; fall back to wall-clock UTC."""
    if isinstance(value, str) and value:
        try:
            datetime.fromisoformat(value)
            return value
        except ValueError:
            pass
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if record_time is not None:
        return datetime.fromtimestamp(record_time, tz=timezone.utc).isoformat()
    return _iso_now()


def _normalize_level(value: Any) -> str:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _LEVEL_ORDER:
            return lowered
    if isinstance(value, int):
        return _STD_LEVEL_TO_NAME.get(value, "info")
    return "info"


def _truncate(message: str) -> str:
    return message if len(message) <= MAX_MESSAGE_CHARS else message[:MAX_MESSAGE_CHARS] + "…"


# ── record + buffer ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LogRecord:
    """A single captured log entry (the row of ``GET /api/logs``)."""

    level: str
    timestamp: str
    logger: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "timestamp": self.timestamp,
            "logger": self.logger,
            "message": self.message,
        }


class LogBuffer:
    """Thread-safe bounded ring buffer of :class:`LogRecord`."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = max(1, int(capacity))
        self._entries: deque[LogRecord] = deque(maxlen=self._capacity)
        self._lock = threading.Lock()
        self._total_appended = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def available(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def total_appended(self) -> int:
        with self._lock:
            return self._total_appended

    def append(self, record: LogRecord) -> None:
        with self._lock:
            self._entries.append(record)
            self._total_appended += 1

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def get_entries(
        self, min_level: str = "", limit: int = DEFAULT_LIMIT
    ) -> list[LogRecord]:
        """Newest-first entries at or above ``min_level``, capped at ``limit``."""
        floor = 0
        if min_level:
            name = min_level.strip().lower()
            if name in _LEVEL_ORDER:
                floor = _LEVEL_ORDER[name]
            elif name.isdigit():
                std_name = _STD_LEVEL_TO_NAME.get(int(name))
                if std_name is not None:
                    floor = _LEVEL_ORDER[std_name]
        limit = max(1, min(int(limit), self._capacity))
        with self._lock:
            out = [
                record
                for record in reversed(self._entries)
                if _LEVEL_ORDER.get(record.level, 20) >= floor
            ]
        return out[:limit]


# ── capture paths ───────────────────────────────────────────────────────────


def capture_event(
    logger: Any, method_name: str, event_dict: Mapping[str, Any]
) -> Mapping[str, Any]:
    """structlog processor: copy each event into the ring buffer.

    Runs after ``merge_contextvars`` / ``add_log_level`` and before the
    terminal renderer, so ``event`` is still a plain string. The event is
    *copied* into the buffer; the dict returned is untouched, so normal
    rendering is unaffected. This processor never raises — a capture failure
    must not break logging.
    """
    try:
        if "capture_skipped" in event_dict:  # feedback guard
            return event_dict
        message = event_dict.get("event")
        message = message if isinstance(message, str) else str(message)

        level = _normalize_level(event_dict.get("level"))
        logger = str(event_dict.get("logger_name") or "") or "structlog"

        # Whole-event redaction on suspicious keys (secrets are never stored).
        for key in event_dict:
            if key in ("event", "level", "timestamp", "logger_name", "event_time"):
                continue
            if _is_sensitive(str(key)):
                message = "event redacted: contains a sensitive field"
                break

        get_log_buffer().append(
            LogRecord(
                level=level,
                timestamp=_iso_timestamp(event_dict.get("event_time"), record_time=time()),
                logger=logger,
                message=_truncate(_scrub(message)),
            )
        )
    except Exception:  # noqa: BLE001 — capture must never break logging
        pass
    return event_dict


class _Handler(logging.Handler):
    """Standard-library handler mirroring root-propagated records into the buffer.

    This is how the uvicorn access/error loggers (plain stdlib, never
    structlog) reach the buffer. It ignores its own module's records to avoid
    a capture feedback loop.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            logger = record.name or "root"
            if logger.startswith("backend.observability.log_buffer"):
                return
            level = _STD_LEVEL_TO_NAME.get(record.levelno, "info")
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                message = f"{message} [exception {type(record.exc_info[1]).__name__}]"
            get_log_buffer().append(
                LogRecord(
                    level=level,
                    timestamp=_iso_timestamp(None, record_time=record.created),
                    logger=logger,
                    message=_truncate(_scrub(message)),
                )
            )
        except Exception:  # noqa: BLE001 — capture must never break logging
            pass


_buffer: LogBuffer | None = None
_buffer_lock = threading.Lock()
_stdlib_handler: _Handler | None = None
_installed = False
_nc_configure_guarded = False
_nc_original_configure: Any = None

#: Loggers the stdlib handler is attached to besides the root. uvicorn wires
#: these with their own handlers and ``propagate=False`` after its
#: ``dictConfig`` runs, so a root-only handler would miss their records.
_STDLIB_LOGGER_NAMES = ("uvicorn.access", "uvicorn.error")


def _anchor_structlog() -> None:
    """Insert :func:`capture_event` into the structlog pipeline (idempotent)."""
    config = structlog.get_config()
    # The pipeline is a mutable ``list`` at runtime; typed as Sequence here.
    processors = cast("list[Any]", config["processors"])
    if capture_event in processors:
        return
    anchor_index = -1
    for index, proc in enumerate(processors):
        if getattr(proc, "__name__", "") == "add_log_level":
            anchor_index = index
            break
    if anchor_index >= 0:
        processors.insert(anchor_index + 1, capture_event)
    else:
        processors.insert(0, capture_event)


def _guard_structlog_configure() -> None:
    """Re-anchor the processor if a later ``structlog.configure()`` runs.

    Nothing in this codebase reconfigures structlog, but a future
    ``structlog.configure(...)`` (or ``uvicorn[standard]`` log wiring) would
    otherwise replace the pipeline and silently stop structlog capture. The
    guard wraps the public entry point so the processor is restored after it.
    """
    global _nc_configure_guarded, _nc_original_configure
    if _nc_configure_guarded:
        return
    _nc_original_configure = structlog.configure

    def configure(*args: Any, **kwargs: Any) -> Any:
        result = _nc_original_configure(*args, **kwargs)
        try:
            _anchor_structlog()
        except Exception:  # noqa: BLE001 — re-anchoring must never raise
            pass
        return result

    structlog.configure = configure  # type: ignore[assignment]
    _nc_configure_guarded = True


def install_log_capture() -> None:
    """Idempotently anchor both capture paths.

    Call it from the FastAPI lifespan *startup* — uvicorn runs its own
    ``dictConfig`` during server setup, **before** the app lifespan starts,
    so this is the right moment to re-attach the stdlib handler after that
    reconfiguration. It is safe to call again from tests or at import.
    """
    global _installed, _stdlib_handler

    # structlog pipeline + reconfigure guard.
    _anchor_structlog()
    _guard_structlog_configure()

    # standard-library handler. uvicorn's dictConfig (1) wipes root handlers,
    # and (2) gives ``uvicorn.access`` / ``uvicorn.error`` their own handlers
    # with ``propagate=False`` — so we attach to the root *and* to those two
    # loggers. ``uvicorn.error``'s level defaults to the server's ``log_level``
    # (INFO) and its records flow to us; ``uvicorn.access`` logs at INFO too,
    # but its level is clamped below the server log level only when that level
    # is set — attaching at DEBUG keeps both honest either way.
    if _stdlib_handler is None:
        _stdlib_handler = _Handler(level=logging.DEBUG)
    for name in _STDLIB_LOGGER_NAMES:
        target = logging.getLogger(name)
        if _stdlib_handler not in target.handlers:
            target.addHandler(_stdlib_handler)
    root = logging.getLogger()
    if _stdlib_handler not in root.handlers:
        root.addHandler(_stdlib_handler)

    _installed = True


def reset_for_tests() -> None:
    """Restore pristine state (tests only): empty buffer, no handlers/guards.

    Detaches the stdlib handler and un-patches ``structlog.configure`` so a
    test can re-install cleanly without leaking state into other tests.
    """
    global _installed, _stdlib_handler, _nc_configure_guarded, _nc_original_configure, _buffer
    if _stdlib_handler is not None:
        logging.getLogger().removeHandler(_stdlib_handler)
        for name in _STDLIB_LOGGER_NAMES:
            logging.getLogger(name).removeHandler(_stdlib_handler)
        _stdlib_handler = None
    if _nc_configure_guarded and callable(_nc_original_configure):
        structlog.configure = _nc_original_configure  # type: ignore[assignment]
        _nc_configure_guarded = False
    if _buffer is not None:
        _buffer.clear()
    _installed = False
