"""One global, durable limiter for every OpenAI API call (NS-ADR-107).

Owner rule: "we need to take our time when hitting openai". All callers share
this limiter (``app.state.openai_limiter``):

* concurrency (default 1): calls run one at a time, the rest wait in a queue;
* minimum spacing between requests (default 20 s);
* hourly and daily caps on requests and tokens;
* a 429 is honored: ``Retry-After`` when sent, else exponential back-off, and
  the call is retried up to ``max_retries`` times;
* callers wait rather than burst or fail; only a wait longer than
  ``max_wait_s`` gives up with :class:`OpenAIQueueTimeout`.

State is the ``openai_calls`` ledger (durable across restarts: spacing, caps and
Retry-After blocks are recomputed from it). Without a database an in-memory
ledger is used. Rows hold caller, reason code, outcome and token counts only.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class OpenAILimits:
    concurrency: int = 1
    min_spacing_s: float = 20.0
    max_requests_hour: int = 30
    max_requests_day: int = 200
    max_tokens_hour: int = 200_000
    max_tokens_day: int = 1_000_000
    max_wait_s: float = 900.0
    max_retries: int = 4
    base_backoff_s: float = 5.0
    max_backoff_s: float = 300.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> OpenAILimits:
        e = env if env is not None else os.environ
        values: dict[str, Any] = {}
        for f in fields(cls):
            raw = (e.get(f"NERVE_OPENAI_{f.name.upper()}") or "").strip()
            if not raw:
                continue
            try:
                num = float(raw)
            except ValueError:
                continue
            if num < 0:
                continue
            values[f.name] = int(num) if f.type in ("int", int) else num
        values["concurrency"] = max(1, int(values.get("concurrency", 1)))
        return cls(**values)


class OpenAIThrottled(Exception):
    """The API answered 429. ``retry_after`` is seconds when the server said."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("OpenAI rate limited the request")
        self.retry_after = retry_after


class OpenAIQueueTimeout(RuntimeError):
    """Waiting for an OpenAI slot took longer than ``max_wait_s``."""


@dataclass
class CallRecord:
    created_at: datetime
    outcome: str  # ok | throttled | error
    input_tokens: int = 0
    output_tokens: int = 0
    retry_after_s: float | None = None


class Ledger(Protocol):
    async def add(self, *, caller: str, reason: str, outcome: str, **values: Any) -> None: ...

    async def since(self, start: datetime) -> list[CallRecord]: ...


class MemoryLedger:
    def __init__(self, clock: Callable[[], datetime] = utcnow) -> None:
        self._rows: list[CallRecord] = []
        self._clock = clock

    async def add(self, *, caller: str, reason: str, outcome: str, **values: Any) -> None:
        self._rows.append(
            CallRecord(
                created_at=self._clock(),
                outcome=outcome,
                input_tokens=int(values.get("input_tokens") or 0),
                output_tokens=int(values.get("output_tokens") or 0),
                retry_after_s=values.get("retry_after_s"),
            )
        )

    async def since(self, start: datetime) -> list[CallRecord]:
        return [r for r in self._rows if r.created_at >= start]


class DbLedger:
    def __init__(self, factory: Any, clock: Callable[[], datetime] = utcnow) -> None:
        self._factory = factory
        self._clock = clock

    async def add(self, *, caller: str, reason: str, outcome: str, **values: Any) -> None:
        from backend.persistence.models import OpenAICall

        async with self._factory() as session:
            session.add(
                OpenAICall(
                    created_at=self._clock(),
                    caller=caller[:64],
                    reason=reason[:32],
                    outcome=outcome,
                    status_code=values.get("status_code"),
                    input_tokens=int(values.get("input_tokens") or 0),
                    output_tokens=int(values.get("output_tokens") or 0),
                    retry_after_s=values.get("retry_after_s"),
                    wait_ms=int(values.get("wait_ms") or 0),
                )
            )
            await session.commit()

    async def since(self, start: datetime) -> list[CallRecord]:
        from sqlalchemy import select

        from backend.persistence.models import OpenAICall

        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        select(OpenAICall)
                        .where(OpenAICall.created_at >= start)
                        .order_by(OpenAICall.created_at)
                    )
                )
                .scalars()
                .all()
            )
        return [
            CallRecord(
                _aware(r.created_at), r.outcome, r.input_tokens, r.output_tokens, r.retry_after_s
            )
            for r in rows
        ]


class OpenAILimiter:
    def __init__(
        self,
        ledger: Ledger | None = None,
        limits: OpenAILimits | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.limits = limits or OpenAILimits()
        self.ledger = ledger or MemoryLedger(clock)
        self._clock = clock
        self._sleep = sleep
        self._sem = asyncio.Semaphore(self.limits.concurrency)
        self.waiting = 0
        self.in_flight = 0

    def backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None and retry_after >= 0:
            return min(float(retry_after), self.limits.max_backoff_s)
        return min(
            self.limits.base_backoff_s * (2 ** max(0, attempt - 1)), self.limits.max_backoff_s
        )

    async def ready_at(self) -> datetime:
        """Earliest instant the next request may start, from the ledger."""
        now = self._clock()
        lim = self.limits
        day = await self.ledger.since(now - timedelta(days=1))
        hour = [r for r in day if r.created_at >= now - timedelta(hours=1)]
        ready = now
        if day:
            ready = max(ready, day[-1].created_at + timedelta(seconds=lim.min_spacing_s))
        for r in day:
            if r.outcome == "throttled" and r.retry_after_s:
                ready = max(ready, r.created_at + timedelta(seconds=r.retry_after_s))

        def cap(rows: list[CallRecord], window: timedelta, max_req: int, max_tok: int) -> datetime:
            out = now
            sent = [r for r in rows if r.outcome != "throttled"]
            if max_req and len(sent) >= max_req:
                out = max(out, sent[len(sent) - max_req].created_at + window)
            if max_tok:
                total = sum(r.input_tokens + r.output_tokens for r in rows)
                for r in rows:  # drop oldest rows until under the cap
                    if total < max_tok:
                        break
                    total -= r.input_tokens + r.output_tokens
                    out = max(out, r.created_at + window)
            return out

        ready = max(
            ready, cap(hour, timedelta(hours=1), lim.max_requests_hour, lim.max_tokens_hour)
        )
        ready = max(ready, cap(day, timedelta(days=1), lim.max_requests_day, lim.max_tokens_day))
        return ready

    async def run(
        self, call: Callable[[], Awaitable[dict[str, Any]]], *, caller: str, reason: str
    ) -> dict[str, Any]:
        """Run ``call`` under the limiter. ``call`` returns a dict with token counts."""
        start = self._clock()
        deadline = start + timedelta(seconds=self.limits.max_wait_s)
        self.waiting += 1
        try:
            await self._sem.acquire()
        finally:
            self.waiting -= 1
        attempt = 0
        try:
            while True:
                now = self._clock()
                ready = await self.ready_at()
                if ready > now:
                    if ready > deadline:
                        log.warning("openai.limiter.queue_timeout", caller=caller, reason=reason)
                        raise OpenAIQueueTimeout("OpenAI is rate limited; try again later")
                    await self._sleep((ready - now).total_seconds())
                    continue
                attempt += 1
                wait_ms = int((self._clock() - start).total_seconds() * 1000)
                self.in_flight += 1
                try:
                    result = await call()
                except OpenAIThrottled as exc:
                    delay = self.backoff(attempt, exc.retry_after)
                    await self.ledger.add(
                        caller=caller,
                        reason=reason,
                        outcome="throttled",
                        status_code=429,
                        retry_after_s=delay,
                        wait_ms=wait_ms,
                    )
                    log.warning(
                        "openai.limiter.throttled", caller=caller, attempt=attempt, delay_s=delay
                    )
                    if attempt > self.limits.max_retries:
                        raise
                    continue
                except Exception:
                    await self.ledger.add(
                        caller=caller, reason=reason, outcome="error", wait_ms=wait_ms
                    )
                    raise
                finally:
                    self.in_flight -= 1
                await self.ledger.add(
                    caller=caller,
                    reason=reason,
                    outcome="ok",
                    status_code=200,
                    input_tokens=result.get("input_tokens") or 0,
                    output_tokens=result.get("output_tokens") or 0,
                    wait_ms=wait_ms,
                )
                log.info("openai.limiter.call_ok", caller=caller, reason=reason, wait_ms=wait_ms)
                return result
        finally:
            self._sem.release()

    async def usage(self) -> dict[str, Any]:
        now = self._clock()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = await self.ledger.since(min(midnight, now - timedelta(hours=1)))

        def summary(sel: list[CallRecord]) -> dict[str, int]:
            return {
                "requests": sum(1 for r in sel if r.outcome != "throttled"),
                "throttled": sum(1 for r in sel if r.outcome == "throttled"),
                "errors": sum(1 for r in sel if r.outcome == "error"),
                "input_tokens": sum(r.input_tokens for r in sel),
                "output_tokens": sum(r.output_tokens for r in sel),
            }

        ready = await self.ready_at()
        return {
            "queue_depth": self.waiting,
            "in_flight": self.in_flight,
            "next_slot_at": ready.isoformat() if ready > now else None,
            "today": summary([r for r in rows if r.created_at >= midnight]),
            "last_hour": summary([r for r in rows if r.created_at >= now - timedelta(hours=1)]),
            "limits": {f.name: getattr(self.limits, f.name) for f in fields(self.limits)},
        }


def get_limiter(app: Any) -> OpenAILimiter:
    """The single app-wide limiter (durable ledger when a DB is configured)."""
    existing = getattr(app.state, "openai_limiter", None)
    if existing is not None:
        return existing
    engine = getattr(app.state, "db_engine", None)
    ledger: Ledger
    if engine is not None:
        from backend.persistence.connection import make_session_factory

        ledger = DbLedger(make_session_factory(engine))
    else:
        ledger = MemoryLedger()
    limiter = OpenAILimiter(ledger, OpenAILimits.from_env())
    app.state.openai_limiter = limiter
    return limiter
