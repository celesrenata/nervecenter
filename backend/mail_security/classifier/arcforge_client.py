"""Strictly bounded client + pool for the existing ArcForge embedding service (task 3.5).

SECURITY POSTURE (read this before using):

* The ArcForge service is UNAUTHENTICATED, PLAIN HTTP, bound to 0.0.0.0 and reachable
  on the LAN. This client does not and cannot change that: there is no TLS, no
  credential and no integrity protection on the wire. Anything on the LAN can read what
  is sent and can impersonate the service. The client therefore (a) only ever sends
  de-identified :class:`FeatureRecord`-derived feature strings (no subject, body,
  address, URL path or filename can be expressed in that schema), (b) refuses any target
  that is not an explicit loopback/private literal IP, (c) never resolves DNS, and
  (d) validates every response strictly. An authenticated/mTLS endpoint remains an open
  item (design §6); until then this is acceptable for synthetic/authorized data only.
* Targets are always given explicitly by the caller. There is no default target and
  nothing is read from the environment.
* Failure never falls back to CPU or to the baseline and never yields a score: every
  non-``OK`` outcome means "not evaluated" (I-02, R-008). The service itself has no CPU
  fallback by design.
* Requests carry no account or message identifier and the pool caches nothing from
  responses, so no state is shared across accounts (R-008, T-27).
* Errors, results and logs carry only safe reason codes and host labels (``host1`` ...),
  never response bodies, URLs, IPs or feature text.

Standard library only. Nothing here is wired into the worker, policy or pipeline.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import math
import socket
import ssl
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from backend.mail_security.classifier.baseline import feature_strings
from backend.mail_security.classifier.features import FeatureRecord

logger = logging.getLogger(__name__)

#: The service rejects batches above this many items (HTTP 413 ``batch_too_large``).
SERVICE_MAX_BATCH = 32
#: The service runs this many parallel GPU worker streams per host.
SERVICE_WORKER_STREAMS = 4
DEFAULT_MAX_ITEMS = 16
DEFAULT_PER_HOST_CONCURRENCY = 2
#: At most ONE retry, always on a different host.
MAX_RETRIES = 1

_RETRYABLE_OUTCOMES: frozenset[str] = frozenset({"CONNECT_ERROR", "TIMEOUT"})
_CHUNK = 64 * 1024


class Outcome(StrEnum):
    """Typed call outcome. Anything but ``OK`` means NOT EVALUATED (I-02)."""

    OK = "OK"
    NOT_READY = "NOT_READY"
    WEDGED = "WEDGED"
    TIMEOUT = "TIMEOUT"
    TOO_LARGE = "TOO_LARGE"
    BAD_RESPONSE = "BAD_RESPONSE"
    HTTP_ERROR = "HTTP_ERROR"
    CONNECT_ERROR = "CONNECT_ERROR"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    #: Backpressure: the bounded wait queue is full (typed rejection, nothing buffered).
    QUEUE_FULL = "QUEUE_FULL"


class TargetError(ValueError):
    """A target URL was refused. The message is a safe code; it never echoes the input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------- #
# target validation (no DNS, ever)
# --------------------------------------------------------------------------- #
_PRIVATE_NETS = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7")
)


@dataclass(frozen=True)
class Target:
    """A validated service endpoint. ``label`` is the only thing safe to log."""

    scheme: str
    host: str
    port: int
    label: str

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"


def parse_target(url: str, label: str = "host") -> Target:
    """Validate ``url`` as an explicit loopback/private literal-IP http(s) base URL."""
    if not isinstance(url, str) or not url or len(url) > 200:
        raise TargetError("TARGET_INVALID")
    if any(ch.isspace() or ord(ch) < 0x21 or ord(ch) > 0x7E for ch in url):
        raise TargetError("TARGET_INVALID")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise TargetError("TARGET_INVALID") from None
    if parts.scheme not in ("http", "https"):
        raise TargetError("TARGET_SCHEME")
    if "@" in parts.netloc:
        raise TargetError("TARGET_USERINFO")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise TargetError("TARGET_QUERY_OR_FRAGMENT")
    if parts.path not in ("", "/"):
        raise TargetError("TARGET_PATH")
    try:
        port = parts.port
    except ValueError:
        raise TargetError("TARGET_PORT") from None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    if not 1 <= port <= 65535:
        raise TargetError("TARGET_PORT")
    host = parts.hostname
    if not host:
        raise TargetError("TARGET_HOST")
    try:
        ip = ipaddress.ip_address(host)  # strict literal parse: no DNS, no decimal/hex tricks
    except ValueError:
        raise TargetError("TARGET_NOT_LITERAL_IP") from None
    if isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped is not None or ip.scope_id):
        raise TargetError("TARGET_IP_FORM")
    if not any(ip in net for net in _PRIVATE_NETS if net.version == ip.version):
        raise TargetError("TARGET_NOT_PRIVATE")
    return Target(scheme=parts.scheme, host=str(ip), port=port, label=label)


# --------------------------------------------------------------------------- #
# configuration and results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ClientConfig:
    """Client-side bounds. Defaults are conservative for a shared live service."""

    max_items: int = DEFAULT_MAX_ITEMS
    max_chars_per_item: int = 8192
    max_request_bytes: int = 256 * 1024
    max_response_bytes: int = 4 * 1024 * 1024
    max_aux_response_bytes: int = 256 * 1024  # /ready, /v1/models, /metrics
    connect_timeout_s: float = 2.0
    read_timeout_s: float = 10.0
    total_deadline_s: float = 15.0
    norm_tolerance: float = 1e-3

    def __post_init__(self) -> None:
        if not 1 <= self.max_items <= SERVICE_MAX_BATCH:
            raise ValueError("max_items must be within 1..32")
        for name in (
            "max_chars_per_item",
            "max_request_bytes",
            "max_response_bytes",
            "max_aux_response_bytes",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        for name in ("connect_timeout_s", "read_timeout_s", "total_deadline_s", "norm_tolerance"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class ModelIdentity:
    """Model identity AS REPORTED by the service (not independently verified)."""

    model_id: str
    version: str
    dim: int
    device: str


@dataclass(frozen=True)
class CallResult:
    """Common result fields. ``code`` is a safe reason code, never response content."""

    outcome: Outcome
    code: str
    http_status: int | None = None
    host: str = ""
    wall_s: float = 0.0
    #: True only if nothing of a response was received (safe to retry elsewhere).
    retryable: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


@dataclass(frozen=True)
class EmbedResult(CallResult):
    """Embeddings are present ONLY when ``outcome`` is OK (never a partial result)."""

    embeddings: tuple[tuple[float, ...], ...] = ()
    infer_s: float | None = None
    attempts: int = 1


@dataclass(frozen=True)
class ReadyResult(CallResult):
    """Result of GET /ready. ``has_breaker_reason`` avoids storing service text."""

    has_breaker_reason: bool = False


@dataclass(frozen=True)
class ModelsResult(CallResult):
    identity: ModelIdentity | None = None


@dataclass(frozen=True)
class MetricsResult(CallResult):
    #: parsed ``name{labels}`` -> value, restricted to ``arcforge_`` series.
    samples: dict[str, float] = field(default_factory=dict)


def feature_text(record: FeatureRecord) -> str:
    """The single de-identified string sent for one record (ASCII feature tokens only)."""
    if not isinstance(record, FeatureRecord):
        raise TypeError("only FeatureRecord instances may be embedded")
    return " ".join(feature_strings(record))


def parse_prometheus(text: str) -> dict[str, float]:
    """Parse Prometheus text into ``name{labels}`` -> float for ``arcforge_`` series."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith("arcforge_"):
            continue
        key, _, value = line.rpartition(" ")
        try:
            num = float(value)
        except ValueError:
            continue
        if math.isfinite(num) and key:
            out[key.replace(" ", "")] = num
    return out


# --------------------------------------------------------------------------- #
# single-host transport client
# --------------------------------------------------------------------------- #
def _open_socket(host: str, port: int, timeout: float) -> socket.socket:
    """Connect to a validated literal IP WITHOUT any resolver call (no DNS, ever)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((host, port))
    except BaseException:
        sock.close()
        raise
    return sock


class _NumericHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = _open_socket(self.host, self.port, float(self.timeout or 0))


class _NumericHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        raw = _open_socket(self.host, self.port, float(self.timeout or 0))
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)


class _Raw:
    __slots__ = ("body", "content_type", "status")

    def __init__(self, status: int, content_type: str, body: bytes) -> None:
        self.status = status
        self.content_type = content_type
        self.body = body


def _reject_constant(_: str) -> Any:
    raise ValueError("non-finite JSON constant")


def _loads(body: bytes) -> Any:
    return json.loads(body.decode("utf-8"), parse_constant=_reject_constant)


def _safe_float(value: object) -> float | None:
    """float(value) for JSON numbers; None if not a number or not representable."""
    if not _is_number(value):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (OverflowError, ValueError):
        return None


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


class ArcForgeClient:
    """Stateless, bounded HTTP client for ONE target. No retries, no breaker."""

    def __init__(
        self,
        target: Target,
        config: ClientConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.target = target
        self.config = config or ClientConfig()
        self._clock = clock

    # ---- transport ------------------------------------------------------ #
    def _fail(
        self,
        outcome: Outcome,
        code: str,
        start: float,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> CallResult:
        return CallResult(
            outcome, code, status, self.target.label, max(0.0, self._clock() - start), retryable
        )

    def _http(
        self, method: str, path: str, body: bytes | None, max_response: int, start: float
    ) -> _Raw | CallResult:
        cfg = self.config
        deadline = start + cfg.total_deadline_s
        t = self.target
        conn_cls = _NumericHTTPSConnection if t.scheme == "https" else _NumericHTTPConnection
        remaining = deadline - self._clock()
        if remaining <= 0:
            return self._fail(Outcome.TIMEOUT, "DEADLINE", start, retryable=True)
        conn = conn_cls(t.host, t.port, timeout=min(cfg.connect_timeout_s, remaining))
        try:
            try:
                conn.connect()
            except TimeoutError:
                return self._fail(Outcome.TIMEOUT, "CONNECT_TIMEOUT", start, retryable=True)
            except (OSError, http.client.HTTPException):
                return self._fail(Outcome.CONNECT_ERROR, "CONNECT_FAILED", start, retryable=True)
            if conn.sock is not None:
                conn.sock.settimeout(max(0.001, min(cfg.read_timeout_s, deadline - self._clock())))
            headers = {"Connection": "close", "Accept": "application/json, text/plain"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            try:
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
            except TimeoutError:
                return self._fail(Outcome.TIMEOUT, "READ_TIMEOUT", start, retryable=True)
            except (OSError, http.client.HTTPException):
                return self._fail(Outcome.CONNECT_ERROR, "CONNECTION_RESET", start, retryable=True)
            status = resp.status
            ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
            declared = resp.getheader("Content-Length")
            if declared is not None:
                try:
                    if int(declared) > max_response:
                        return self._fail(
                            Outcome.BAD_RESPONSE, "RESPONSE_TOO_LARGE", start, status=status
                        )
                except ValueError:
                    return self._fail(Outcome.BAD_RESPONSE, "BAD_CONTENT_LENGTH", start)
            chunks: list[bytes] = []
            total = 0
            while True:
                if self._clock() >= deadline:
                    return self._fail(Outcome.TIMEOUT, "DEADLINE", start, status=status)
                try:
                    chunk = resp.read(_CHUNK)
                except TimeoutError:
                    return self._fail(Outcome.TIMEOUT, "READ_TIMEOUT", start, status=status)
                except http.client.IncompleteRead:
                    return self._fail(Outcome.BAD_RESPONSE, "TRUNCATED", start, status=status)
                except (OSError, http.client.HTTPException):
                    return self._fail(
                        Outcome.CONNECT_ERROR, "CONNECTION_RESET", start, status=status
                    )
                if not chunk:
                    if declared is not None and total != int(declared):
                        return self._fail(Outcome.BAD_RESPONSE, "TRUNCATED", start, status=status)
                    break
                total += len(chunk)
                if total > max_response:
                    return self._fail(
                        Outcome.BAD_RESPONSE, "RESPONSE_TOO_LARGE", start, status=status
                    )
                chunks.append(chunk)
            return _Raw(status, ctype, b"".join(chunks))
        finally:
            conn.close()

    def _json_body(self, raw: _Raw, start: float) -> Any | CallResult:
        if raw.content_type != "application/json":
            return self._fail(Outcome.BAD_RESPONSE, "BAD_CONTENT_TYPE", start, status=raw.status)
        try:
            return _loads(raw.body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return self._fail(Outcome.BAD_RESPONSE, "BAD_JSON", start, status=raw.status)

    def _http_error(self, raw: _Raw, start: float) -> CallResult:
        retry = raw.status >= 500
        return self._fail(
            Outcome.HTTP_ERROR, f"HTTP_{raw.status}", start, status=raw.status, retryable=retry
        )

    # ---- endpoints ------------------------------------------------------ #
    def ready(self) -> ReadyResult:
        start = self._clock()
        raw = self._http("GET", "/ready", None, self.config.max_aux_response_bytes, start)
        if isinstance(raw, CallResult):
            return ReadyResult(**_fields(raw))
        if raw.status not in (200, 503):
            return ReadyResult(**_fields(self._http_error(raw, start)))
        obj = self._json_body(raw, start)
        if isinstance(obj, CallResult):
            return ReadyResult(**_fields(obj))
        wall = max(0.0, self._clock() - start)
        if (
            not isinstance(obj, dict)
            or not isinstance(obj.get("ready"), bool)
            or not isinstance(obj.get("wedged"), bool)
        ):
            return ReadyResult(
                Outcome.BAD_RESPONSE, "BAD_READY_SHAPE", raw.status, self.target.label, wall
            )
        reason = obj.get("breaker_reason")
        has_reason = isinstance(reason, str) and bool(reason)
        if obj["wedged"]:
            outcome, code = Outcome.WEDGED, "WEDGED"
        elif raw.status == 200 and obj["ready"]:
            outcome, code = Outcome.OK, "READY"
        else:
            outcome, code = Outcome.NOT_READY, "NOT_READY"
        return ReadyResult(
            outcome, code, raw.status, self.target.label, wall, has_breaker_reason=has_reason
        )

    def models(self) -> ModelsResult:
        start = self._clock()
        raw = self._http("GET", "/v1/models", None, self.config.max_aux_response_bytes, start)
        if isinstance(raw, CallResult):
            return ModelsResult(**_fields(raw))
        if raw.status != 200:
            return ModelsResult(**_fields(self._http_error(raw, start)))
        obj = self._json_body(raw, start)
        if isinstance(obj, CallResult):
            return ModelsResult(**_fields(obj))
        wall = max(0.0, self._clock() - start)
        identity = _parse_identity(obj)
        if identity is None:
            return ModelsResult(
                Outcome.BAD_RESPONSE, "BAD_MODELS_SHAPE", raw.status, self.target.label, wall
            )
        return ModelsResult(
            Outcome.OK, "MODELS_OK", raw.status, self.target.label, wall, identity=identity
        )

    def metrics(self) -> MetricsResult:
        start = self._clock()
        raw = self._http("GET", "/metrics", None, self.config.max_aux_response_bytes, start)
        if isinstance(raw, CallResult):
            return MetricsResult(**_fields(raw))
        if raw.status != 200:
            return MetricsResult(**_fields(self._http_error(raw, start)))
        if raw.content_type != "text/plain":
            return MetricsResult(
                Outcome.BAD_RESPONSE, "BAD_CONTENT_TYPE", raw.status, self.target.label
            )
        samples = parse_prometheus(raw.body.decode("utf-8", errors="replace"))
        return MetricsResult(
            Outcome.OK,
            "METRICS_OK",
            raw.status,
            self.target.label,
            max(0.0, self._clock() - start),
            samples=samples,
        )

    def embed(self, records: Sequence[FeatureRecord], identity: ModelIdentity) -> EmbedResult:
        """Embed already-validated FeatureRecords against a pinned identity."""
        cfg = self.config
        start = self._clock()
        texts = [feature_text(r) for r in records]
        if not texts or len(texts) > cfg.max_items:
            return _embed_fail(self._bounds_fail("TOO_MANY_ITEMS" if texts else "EMPTY_BATCH"))
        if any(len(t) > cfg.max_chars_per_item for t in texts):
            return _embed_fail(self._bounds_fail("ITEM_TOO_LONG"))
        body = json.dumps({"model": identity.model_id, "input": texts}).encode("utf-8")
        if len(body) > cfg.max_request_bytes:
            return _embed_fail(self._bounds_fail("REQUEST_TOO_LARGE"))
        raw = self._http("POST", "/v1/embeddings", body, cfg.max_response_bytes, start)
        if isinstance(raw, CallResult):
            return _embed_fail(raw)
        if raw.status == 413:
            return _embed_fail(self._fail(Outcome.TOO_LARGE, "SERVER_413", start, status=413))
        if raw.status != 200:
            return _embed_fail(self._http_error(raw, start))
        obj = self._json_body(raw, start)
        if isinstance(obj, CallResult):
            return _embed_fail(obj)
        wall = max(0.0, self._clock() - start)
        parsed = _validate_embeddings(obj, len(texts), identity, cfg.norm_tolerance)
        if isinstance(parsed, str):
            return EmbedResult(Outcome.BAD_RESPONSE, parsed, raw.status, self.target.label, wall)
        vectors, infer_s = parsed
        return EmbedResult(
            Outcome.OK,
            "OK",
            raw.status,
            self.target.label,
            wall,
            embeddings=vectors,
            infer_s=infer_s,
        )

    def _bounds_fail(self, code: str) -> CallResult:
        return CallResult(Outcome.TOO_LARGE, code, None, self.target.label)


def _fields(r: CallResult) -> dict[str, Any]:
    return {
        "outcome": r.outcome,
        "code": r.code,
        "http_status": r.http_status,
        "host": r.host,
        "wall_s": r.wall_s,
        "retryable": r.retryable,
    }


def _embed_fail(r: CallResult) -> EmbedResult:
    return EmbedResult(**_fields(r))


def _parse_identity(obj: object) -> ModelIdentity | None:
    if not isinstance(obj, dict) or obj.get("object") != "list":
        return None
    data = obj.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        return None
    entry = data[0]
    meta = entry.get("arcforge")
    model_id = entry.get("id")
    if not isinstance(meta, dict) or not isinstance(model_id, str) or not model_id:
        return None
    version, device, dim = meta.get("version"), meta.get("device"), meta.get("dim")
    if not (isinstance(version, str) and isinstance(device, str) and version and device):
        return None
    if not isinstance(dim, int) or isinstance(dim, bool) or not 1 <= dim <= 65536:
        return None
    if len(model_id) > 200 or len(version) > 200 or len(device) > 50:
        return None
    return ModelIdentity(model_id, version, dim, device)


def _validate_embeddings(
    obj: object, n: int, identity: ModelIdentity, tol: float
) -> tuple[tuple[tuple[float, ...], ...], float | None] | str:
    """Return (vectors, infer_s) or a safe reason code. All-or-nothing."""
    if not isinstance(obj, dict):
        return "BAD_SHAPE"
    if obj.get("model") != identity.model_id:
        return "IDENTITY_CHANGED"
    data = obj.get("data")
    if not isinstance(data, list):
        return "BAD_SHAPE"
    if len(data) != n:
        return "BAD_DATA_LENGTH"
    infer_s: float | None = None
    usage = obj.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("arcforge"), dict):
        meta = usage["arcforge"]
        if "dim" in meta and meta["dim"] != identity.dim:
            return "IDENTITY_CHANGED"
        if "infer_s" in meta:
            v = meta["infer_s"]
            fv = _safe_float(v)
            if fv is None or not math.isfinite(fv) or fv < 0:
                return "BAD_INFER_S"
            infer_s = fv
    out: list[tuple[float, ...]] = []
    for i, item in enumerate(data):
        if (
            not isinstance(item, dict)
            or item.get("index") != i
            or isinstance(item.get("index"), bool)
        ):
            return "BAD_INDEX"
        if item.get("model", identity.model_id) != identity.model_id:
            return "IDENTITY_CHANGED"
        emb = item.get("embedding")
        if not isinstance(emb, list) or len(emb) != identity.dim:
            return "BAD_DIM"
        total = 0.0
        for v in emb:
            f = _safe_float(v)
            if f is None:
                return "BAD_VALUE"
            if not math.isfinite(f):
                return "NON_FINITE"
            total += f * f
        if not math.isfinite(total) or abs(math.sqrt(total) - 1.0) > tol:
            return "BAD_NORM"
        out.append(tuple(float(v) for v in emb))
    return tuple(out), infer_s


# --------------------------------------------------------------------------- #
# pool
# --------------------------------------------------------------------------- #
class HostState(StrEnum):
    UNPROBED = "UNPROBED"
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class PoolConfig:
    client: ClientConfig = field(default_factory=ClientConfig)
    per_host_concurrency: int = DEFAULT_PER_HOST_CONCURRENCY
    max_waiting: int = 8
    queue_timeout_s: float = 5.0
    queue_poll_s: float = 0.005
    failure_threshold: int = 3
    cooldown_s: float = 30.0

    def __post_init__(self) -> None:
        if not 1 <= self.per_host_concurrency <= SERVICE_WORKER_STREAMS:
            raise ValueError("per_host_concurrency must be within 1..4")
        if self.max_waiting < 0 or self.failure_threshold < 1:
            raise ValueError("invalid pool bounds")
        if not self.cooldown_s > 0 or not self.queue_timeout_s >= 0 or not self.queue_poll_s > 0:
            raise ValueError("invalid pool timing")


@dataclass(frozen=True)
class HostStatus:
    label: str
    state: HostState
    outstanding: int
    max_outstanding: int
    consecutive_failures: int
    identity: ModelIdentity | None
    last_code: str


class _Host:
    def __init__(self, client: ArcForgeClient) -> None:
        self.client = client
        self.label = client.target.label
        self.state = HostState.UNPROBED
        self.outstanding = 0
        self.max_outstanding = 0
        self.failures = 0
        self.reopen_at = 0.0
        self.probing = False
        self.identity: ModelIdentity | None = None
        self.last_code = "NEW"


class ArcForgePool:
    """Least-outstanding pool with per-host breakers, bounded queue, one different-host retry."""

    def __init__(
        self,
        targets: Sequence[str],
        config: PoolConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not targets:
            raise ValueError("at least one explicit target is required")
        self.config = config or PoolConfig()
        self._clock = clock
        self._sleep = sleep
        parsed = [parse_target(u, f"host{i + 1}") for i, u in enumerate(targets)]
        self._hosts = [_Host(ArcForgeClient(t, self.config.client, clock=clock)) for t in parsed]
        self._lock = threading.Lock()
        self._waiting = 0
        self._identity: ModelIdentity | None = None

    # ---- introspection -------------------------------------------------- #
    @property
    def labels(self) -> list[str]:
        return [h.label for h in self._hosts]

    @property
    def identity(self) -> ModelIdentity | None:
        """The pool-pinned identity (first host probed). Others must match or are quarantined."""
        with self._lock:
            return self._identity

    def client_for(self, label: str) -> ArcForgeClient:
        for h in self._hosts:
            if h.label == label:
                return h.client
        raise KeyError("unknown host label")

    def targets(self) -> list[Target]:
        return [h.client.target for h in self._hosts]

    def status(self) -> list[HostStatus]:
        with self._lock:
            return [
                HostStatus(
                    h.label,
                    h.state,
                    h.outstanding,
                    h.max_outstanding,
                    h.failures,
                    h.identity,
                    h.last_code,
                )
                for h in self._hosts
            ]

    # ---- breaker + health ----------------------------------------------- #
    def _record_failure(self, h: _Host, code: str, *, trip: bool = False) -> None:
        with self._lock:
            h.last_code = code
            if h.state is HostState.QUARANTINED:
                return
            h.failures += 1
            if trip or h.failures >= self.config.failure_threshold or h.state is HostState.OPEN:
                h.state = HostState.OPEN
                h.reopen_at = self._clock() + self.config.cooldown_s

    def _record_success(self, h: _Host) -> None:
        with self._lock:
            h.last_code = "OK"
            if h.state is HostState.QUARANTINED:
                return
            h.failures = 0
            h.state = HostState.CLOSED

    def _quarantine(self, h: _Host, code: str) -> None:
        with self._lock:
            h.state = HostState.QUARANTINED
            h.last_code = code
        logger.warning("arcforge host quarantined label=%s code=%s", h.label, code)

    def _probe(self, h: _Host) -> CallResult:
        """GET /ready then /v1/models; pins/compares identity. Updates breaker state."""
        ready = h.client.ready()
        if not ready.ok:
            self._record_failure(h, ready.code, trip=True)
            return ready
        models = h.client.models()
        if not models.ok or models.identity is None:
            self._record_failure(h, models.code, trip=True)
            return models
        ident = models.identity
        with self._lock:
            if self._identity is None:
                self._identity = ident
            mismatch = ident != self._identity or (h.identity is not None and ident != h.identity)
            if not mismatch and h.identity is None:
                h.identity = ident
        if mismatch:
            self._quarantine(h, "IDENTITY_MISMATCH")
            return CallResult(Outcome.NOT_AVAILABLE, "IDENTITY_MISMATCH", None, h.label)
        self._record_success(h)
        return ready

    def check_all(self) -> list[tuple[str, CallResult]]:
        """Probe every non-quarantined host now (used between benchmark cells)."""
        out: list[tuple[str, CallResult]] = []
        for h in self._hosts:
            if h.state is HostState.QUARANTINED:
                out.append(
                    (h.label, CallResult(Outcome.NOT_AVAILABLE, "QUARANTINED", None, h.label))
                )
                continue
            out.append((h.label, self._probe(h)))
        return out

    # ---- acquisition ---------------------------------------------------- #
    def _try_acquire(self, excluded: set[int], probed: set[int]) -> tuple[str, int | None]:
        """Return ("slot"|"probe"|"wait"|"open"|"none", host index). Holds the lock briefly."""
        cap = self.config.per_host_concurrency
        with self._lock:
            now = self._clock()
            best: int | None = None
            probe: int | None = None
            saturated = False
            any_open = False
            for i, h in enumerate(self._hosts):
                if i in excluded or h.state is HostState.QUARANTINED:
                    continue
                if h.probing:
                    saturated = True  # a probe is in flight: wait for its verdict
                    continue
                if h.state is HostState.OPEN:
                    any_open = True
                    if now >= h.reopen_at and i not in probed and probe is None:
                        probe = i
                    continue
                if h.state is HostState.UNPROBED:
                    if i not in probed and probe is None:
                        probe = i
                    continue
                if h.outstanding >= cap:
                    saturated = True
                    continue
                if best is None or h.outstanding < self._hosts[best].outstanding:
                    best = i
            if probe is not None:  # recover/learn hosts first: cheap and keeps pins fresh
                self._hosts[probe].probing = True
                return "probe", probe
            if best is not None:
                self._take(self._hosts[best])
                return "slot", best
            if saturated:
                return "wait", None
            return ("open" if any_open else "none"), None

    def _take(self, h: _Host) -> None:
        h.outstanding += 1
        h.max_outstanding = max(h.max_outstanding, h.outstanding)

    def _release(self, h: _Host) -> None:
        with self._lock:
            h.outstanding -= 1

    def _acquire(
        self, excluded: set[int], *, block: bool
    ) -> tuple[_Host | None, CallResult | None]:
        probed: set[int] = set()
        waited = False
        wait_started = 0.0
        try:
            while True:
                kind, idx = self._try_acquire(excluded, probed)
                if kind == "slot" and idx is not None:
                    return self._hosts[idx], None
                if kind == "probe" and idx is not None:
                    h = self._hosts[idx]
                    probed.add(idx)
                    try:
                        self._probe(h)
                    finally:
                        with self._lock:
                            h.probing = False
                    continue
                if kind == "wait" and block:
                    if not waited:
                        with self._lock:
                            if self._waiting >= self.config.max_waiting:
                                return None, CallResult(Outcome.QUEUE_FULL, "QUEUE_FULL")
                            self._waiting += 1
                        waited = True
                        wait_started = self._clock()
                    elif self._clock() - wait_started >= self.config.queue_timeout_s:
                        return None, CallResult(Outcome.TIMEOUT, "QUEUE_WAIT_TIMEOUT")
                    self._sleep(self.config.queue_poll_s)
                    continue
                if kind == "open":
                    return None, CallResult(Outcome.CIRCUIT_OPEN, "ALL_HOSTS_OPEN")
                return None, CallResult(Outcome.NOT_AVAILABLE, "NO_HEALTHY_HOST")
        finally:
            if waited:
                with self._lock:
                    self._waiting -= 1

    # ---- public API ----------------------------------------------------- #
    def embed(self, records: Sequence[FeatureRecord]) -> EmbedResult:
        """Embed records on a healthy host. Any non-OK result means NOT EVALUATED."""
        texts_ok = [feature_text(r) for r in records]  # type-checks every record
        cfg = self.config.client
        if not texts_ok or len(texts_ok) > cfg.max_items:
            return _embed_fail(
                CallResult(Outcome.TOO_LARGE, "TOO_MANY_ITEMS" if texts_ok else "EMPTY_BATCH")
            )
        excluded: set[int] = set()
        first: EmbedResult | None = None
        result: EmbedResult | None = None
        for attempt in range(1 + MAX_RETRIES):
            host, rejection = self._acquire(excluded, block=attempt == 0)
            if host is None:
                if first is not None:
                    return first  # no different healthy host: report the original failure
                assert rejection is not None
                return _embed_fail(rejection)
            excluded.add(self._hosts.index(host))
            try:
                ident = host.identity
                if ident is None:  # pragma: no cover - probe always pins before a slot is used
                    return _embed_fail(CallResult(Outcome.NOT_AVAILABLE, "NO_IDENTITY"))
                result = host.client.embed(records, ident)
            finally:
                self._release(host)
            self._account(host, result)
            if result.ok:
                return _with_attempts(result, attempt + 1)
            if first is None:
                first = _with_attempts(result, attempt + 1)
            if not self._retryable(result):
                return first
        assert first is not None and result is not None
        return _with_attempts(result, 1 + MAX_RETRIES)

    @staticmethod
    def _retryable(r: CallResult) -> bool:
        if not r.retryable:
            return False  # partial response seen, or 4xx / validation failure
        if r.outcome in (Outcome.CONNECT_ERROR, Outcome.TIMEOUT):
            return True
        return r.outcome is Outcome.HTTP_ERROR and (r.http_status or 0) >= 500

    def _account(self, h: _Host, r: EmbedResult) -> None:
        if r.ok:
            self._record_success(h)
        elif r.code == "IDENTITY_CHANGED":
            self._quarantine(h, "IDENTITY_CHANGED")
        elif r.outcome in (Outcome.CONNECT_ERROR, Outcome.TIMEOUT, Outcome.BAD_RESPONSE) or (
            r.outcome is Outcome.HTTP_ERROR and (r.http_status or 0) >= 500
        ):
            self._record_failure(h, r.code)
        # 4xx / TOO_LARGE are caller errors: they do not count against the host.


def _with_attempts(r: EmbedResult, attempts: int) -> EmbedResult:
    return EmbedResult(**_fields(r), embeddings=r.embeddings, infer_s=r.infer_s, attempts=attempts)
