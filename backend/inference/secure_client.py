"""One shared client for model calls that carry personal data (NS-ADR-108).

Owner rule: "there is a secure header we can flag it with, secure means it
doesn't hit conversation history". This reuses the live-proven NS-ADR-073
pattern (``backend/connectors/omniroute_observer.py``):

* the model is an OmniRoute ``secure/*`` combo (OmniRoute's per-combo isolation
  flag: no memory, skills, styles or injected context, whole tree);
* every request sends ``x-omniroute-no-memory: true`` and
  ``x-omniroute-compression: off``;
* before every call the route guard (``LocalOnlyRouteResolver`` with
  ``require_secure_combo=True``) re-reads the registry and checks the combo is
  flagged secure and every leaf, fallbacks included, is local. Any deny raises
  :class:`SecureRouteDenied` with a reason code and nothing is sent.

Capacity (owner's numbers, NS-ADR-109): each secure route has its own
concurrency semaphore, timeout sized to the call's ``max_tokens`` and tenacity
retries with exponential back-off and jitter on rejection, queue-full, 429,
502/503/504 and timeouts, honoring ``Retry-After``; every throttle is reported
to the runner's adaptive back-off through ``on_throttle``. Work is routed by
kind: planner for long reasoning and research, coder for code and general
work, reader for extraction and classification (or small ``max_tokens``).

Default: every call through :class:`SecureTransport` is sensitive. Only a call
that passes ``sensitive=False`` explicitly takes the plain local-first path
(NS-ADR-107). In fixture mode (tests, offline dev) nothing leaves the process,
so the guard is skipped.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import structlog
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt
from tenacity.wait import wait_random_exponential

from backend.inference.local_first import local_model

log = structlog.get_logger(__name__)

SECURE_HEADERS: dict[str, str] = {
    "x-omniroute-no-memory": "true",
    "x-omniroute-compression": "off",
}
SECURE_MODEL_ENV = "NERVE_SECURE_MODEL"
ROUTES_ENV = "NERVE_SECURE_ROUTES"
PLANNER, CODER, READER = "secure/planner", "secure/coder", "secure/reader"
#: General work goes to the coder (27B, 4 sessions); the planner (1 session,
#: ~10 tok/s) is kept for long reasoning and research.
DEFAULT_SECURE_MODEL = CODER

#: Work kind -> secure route.
KIND_ROUTES = {
    "research": PLANNER,
    "reasoning": PLANNER,
    "plan": PLANNER,
    "code": CODER,
    "general": CODER,
    "extract": READER,
    "classify": READER,
}
#: Calls with a completion budget at or below this go to the reader.
SMALL_MAX_TOKENS = 512


@dataclass(frozen=True)
class RouteCapacity:
    """Per-route limits. Defaults are the owner's numbers (NS-ADR-109)."""

    concurrency: int
    tokens_per_s: float
    max_tokens: int
    attempts: int
    backoff_base_s: float
    backoff_max_s: float
    overhead_s: float = 30.0
    max_timeout_s: float = 600.0  # OmniRoute secure combos: targetTimeoutMs 600000

    def timeout_ms(self, max_tokens: int | None) -> int:
        budget = max_tokens or self.max_tokens
        seconds = self.overhead_s + 1.25 * budget / max(self.tokens_per_s, 0.1)
        return int(min(seconds, self.max_timeout_s) * 1000)


DEFAULT_ROUTES: dict[str, RouteCapacity] = {
    # 1 session, ~10 tok/s, 763B, "expect it to be bitchy": patient, never fan out.
    PLANNER: RouteCapacity(1, 10.0, 4096, attempts=6, backoff_base_s=10.0, backoff_max_s=180.0),
    # RTX 5090, 27B, ~270 tok/s, 4 sessions: "we can be a lot meaner here".
    CODER: RouteCapacity(4, 270.0, 8192, attempts=4, backoff_base_s=1.0, backoff_max_s=20.0),
    # 8B, 3 sessions, ~130 tok/s each: parallel small jobs.
    READER: RouteCapacity(3, 130.0, 2048, attempts=4, backoff_base_s=1.0, backoff_max_s=30.0),
}
#: Unknown secure routes get the most cautious profile.
FALLBACK_ROUTE = DEFAULT_ROUTES[PLANNER]

_RETRY_MARKERS = ("http_429", "http_502", "http_503", "http_504", "timeout", "queue", "reject")


def load_routes(env: dict[str, str] | None = None) -> dict[str, RouteCapacity]:
    """Defaults overridden by ``NERVE_SECURE_ROUTES`` JSON, e.g.
    ``{"secure/coder": {"concurrency": 2}}``. Bad entries are ignored."""
    routes = dict(DEFAULT_ROUTES)
    raw = ((env if env is not None else os.environ).get(ROUTES_ENV) or "").strip()
    if not raw:
        return routes
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("inference.secure.routes_invalid")
        return routes
    if not isinstance(data, dict):
        return routes
    for name, values in data.items():
        if not is_secure(name) or not isinstance(values, dict):
            continue
        base = routes.get(name, FALLBACK_ROUTE)
        clean = {}
        for key, value in values.items():
            if (
                key in RouteCapacity.__dataclass_fields__
                and isinstance(value, (int, float))
                and value > 0
            ):
                clean[key] = type(getattr(base, key))(value)
        routes[name] = replace(base, **clean)
    return routes


_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}
_SEM_LOCK = threading.Lock()


def route_semaphore(model: str, capacity: RouteCapacity) -> threading.BoundedSemaphore:
    """One process-wide semaphore per secure route, shared by every caller."""
    with _SEM_LOCK:
        sem = _SEMAPHORES.get(model)
        if sem is None:
            sem = _SEMAPHORES[model] = threading.BoundedSemaphore(max(1, capacity.concurrency))
        return sem


def is_retryable(exc: BaseException) -> bool:
    """Rejection, queue-full, 429, 502/503/504 and timeouts. Never a guard deny."""
    if isinstance(exc, SecureRouteDenied):
        return False
    reasons = getattr(exc, "reasons", None)
    text = " ".join(str(v) for v in reasons.values()) if isinstance(reasons, dict) else ""
    text = f"{text} {type(exc).__name__}".lower()
    return any(marker in text for marker in _RETRY_MARKERS)


Guard = Callable[[str], Awaitable[Any]]  # alias -> RouteDecision


class SecureRouteDenied(RuntimeError):
    """The route guard denied the secure combo. ``reason`` is a log-safe code."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"secure route denied: {reason}")
        self.reason = reason


def is_secure(model: str | None) -> bool:
    return bool(model) and str(model).strip().startswith("secure/")


def secure_model(
    requested: str | None,
    env: dict[str, str] | None = None,
    *,
    kind: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """Pick the secure route: explicit kind, then a requested secure combo,
    then size (small completion budget -> reader), then the configured default."""
    if kind and kind in KIND_ROUTES:
        return KIND_ROUTES[kind]
    if is_secure(requested):
        return str(requested).strip()
    if max_tokens is not None and max_tokens <= SMALL_MAX_TOKENS:
        return READER
    raw = ((env if env is not None else os.environ).get(SECURE_MODEL_ENV) or "").strip()
    return raw if is_secure(raw) else DEFAULT_SECURE_MODEL


def build_guard(base_url: str | None, api_key: str | Callable[[], str | None] | None) -> Guard:
    """The NS-ADR-073 route guard bound to the OmniRoute registry."""
    from backend.inference.omniroute_registry import HttpRegistryTransport
    from backend.mail_security.route_guard import LocalOnlyRouteResolver, ResolverLimits

    resolver = LocalOnlyRouteResolver(
        HttpRegistryTransport(base_url or "", api_key or ""),  # str or live callable
        omniroute_base_url=base_url or "",
        require_secure_combo=True,
        # Live /api/combos takes ~1.6 s and sometimes >5 s; the 5 s default
        # denied asks with REGISTRY_TIMEOUT.
        limits=ResolverLimits(timeout_seconds=15.0),
    )
    return resolver.resolve


def _run_sync(coro: Awaitable[Any]) -> Any:
    """Run ``coro`` to completion from sync code, inside or outside a loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["value"] = asyncio.run(coro)  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


class SecureTransport:
    """Secure combo + no-memory headers + route guard + per-route capacity and retries."""

    def __init__(
        self,
        inner: Any,
        caller: str,
        guard: Guard | None = None,
        *,
        on_throttle: Callable[[datetime], None] | None = None,
        routes: dict[str, RouteCapacity] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._inner = inner
        self._caller = caller
        self._guard = (
            guard
            if guard is not None
            else build_guard(
                getattr(inner, "base_url", None),
                # A live key source (NS-ADR-117) when the inner transport has one.
                getattr(inner, "current_key", None) or getattr(inner, "api_key", None),
            )
        )
        self.on_throttle = on_throttle
        self._routes = routes if routes is not None else load_routes()
        self._sleep = sleep

    def check(self, model: str) -> None:
        """Run the route guard; raises :class:`SecureRouteDenied` on any deny."""
        try:
            decision = _run_sync(self._guard(model))
        except Exception:  # noqa: BLE001 - fail closed, never surface exception text
            log.warning(
                "inference.secure.denied", caller=self._caller, model=model, reason="GUARD_ERROR"
            )
            raise SecureRouteDenied("GUARD_ERROR") from None
        if not getattr(decision, "allowed", False):
            reason = str(getattr(getattr(decision, "reason", None), "value", "DENIED"))
            log.warning("inference.secure.denied", caller=self._caller, model=model, reason=reason)
            raise SecureRouteDenied(reason)

    def capacity(self, model: str) -> RouteCapacity:
        return self._routes.get(model, FALLBACK_ROUTE)

    def _wait(self, capacity: RouteCapacity) -> Callable[[RetryCallState], float]:
        jitter = wait_random_exponential(
            multiplier=capacity.backoff_base_s, max=capacity.backoff_max_s
        )

        def wait(state: RetryCallState) -> float:
            exc = state.outcome.exception() if state.outcome else None
            retry_after = getattr(exc, "retry_after", None)
            if isinstance(retry_after, (int, float)) and retry_after >= 0:
                return min(float(retry_after), capacity.backoff_max_s)
            return jitter(state)

        return wait

    def _on_retry(self, model: str) -> Callable[[RetryCallState], None]:
        def before_sleep(state: RetryCallState) -> None:
            log.warning(
                "inference.secure.retry",
                caller=self._caller,
                model=model,
                attempt=state.attempt_number,
                error=type(state.outcome.exception()).__name__ if state.outcome else None,
            )
            if self.on_throttle is not None:
                try:
                    self.on_throttle(datetime.now(UTC))
                except Exception:  # noqa: BLE001 - back-off feed must not break the call
                    pass

        return before_sleep

    def generate(
        self,
        model: str,
        prompt: str,
        parameters: dict[str, Any] | None = None,
        timeout: int | None = None,
        *,
        sensitive: bool = True,
        kind: str | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not sensitive:
            chosen = local_model(model, caller=self._caller)
            return self._inner.generate(chosen, prompt, parameters, timeout, **kwargs)
        chosen = secure_model(model, kind=kind, max_tokens=max_tokens)
        capacity = self.capacity(chosen)
        budget = max_tokens or capacity.max_tokens
        call_timeout = timeout or capacity.timeout_ms(budget)
        headers = {**(kwargs.pop("headers", None) or {}), **SECURE_HEADERS}
        fixture = getattr(self._inner, "fixture_mode", False)
        sem = route_semaphore(chosen, capacity)

        def attempt() -> dict[str, Any]:
            if not fixture:
                self.check(chosen)  # re-resolved on every attempt
            with sem:
                return self._inner.generate(
                    chosen,
                    prompt,
                    parameters,
                    call_timeout,
                    headers=headers,
                    max_tokens=budget,
                    **kwargs,
                )

        retrying = Retrying(
            stop=stop_after_attempt(max(1, capacity.attempts)),
            wait=self._wait(capacity),
            retry=retry_if_exception(is_retryable),
            before_sleep=self._on_retry(chosen),
            reraise=True,
            **({"sleep": self._sleep} if self._sleep is not None else {}),
        )
        return retrying(attempt)

    def generate_stream(
        self,
        model: str,
        prompt: str,
        parameters: dict[str, Any] | None = None,
        timeout: int | None = None,
        *,
        sensitive: bool = True,
        kind: str | None = None,
        max_tokens: int | None = None,
        attempts: int | None = None,
        **kwargs: Any,
    ) -> Iterator[str]:
        """Streaming twin of :meth:`generate`: same route choice, headers and
        guard. The guard runs before every attempt, the route semaphore is held
        for the whole stream, and retries happen only before the first chunk.
        ``attempts`` overrides the route's attempt count (the streaming ask runs
        its own reported retry loop and passes 1)."""
        if not sensitive:
            chosen = local_model(model, caller=self._caller)
            yield from self._inner.generate_stream(
                chosen,
                prompt,
                headers=kwargs.get("headers"),
                max_tokens=max_tokens,
                timeout=timeout,
            )
            return
        chosen = secure_model(model, kind=kind, max_tokens=max_tokens)
        capacity = self.capacity(chosen)
        budget = max_tokens or capacity.max_tokens
        call_timeout = timeout or capacity.timeout_ms(budget)
        headers = {**(kwargs.pop("headers", None) or {}), **SECURE_HEADERS}
        fixture = getattr(self._inner, "fixture_mode", False)
        sem = route_semaphore(chosen, capacity)

        def open_stream() -> tuple[Iterator[str], bool, str]:
            if not fixture:
                self.check(chosen)  # re-resolved on every attempt
            stream = iter(
                self._inner.generate_stream(
                    chosen, prompt, headers=headers, max_tokens=budget, timeout=call_timeout
                )
            )
            try:
                return stream, True, next(stream)
            except StopIteration:
                return stream, False, ""

        retrying = Retrying(
            stop=stop_after_attempt(max(1, attempts or capacity.attempts)),
            wait=self._wait(capacity),
            retry=retry_if_exception(is_retryable),
            before_sleep=self._on_retry(chosen),
            reraise=True,
            **({"sleep": self._sleep} if self._sleep is not None else {}),
        )
        with sem:
            stream, has_first, first = retrying(open_stream)
            if has_first:
                yield first
            yield from stream

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def secure_transport(
    caller: str,
    inner: Any = None,
    *,
    on_throttle: Callable[[datetime], None] | None = None,
) -> SecureTransport:
    """The one factory every personal-data call site uses."""
    if inner is None:
        from backend.inference.omniroute import OmniRouteTransport

        inner = OmniRouteTransport.from_env()
    return SecureTransport(inner, caller, on_throttle=on_throttle)
