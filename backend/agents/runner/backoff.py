"""Adaptive back-off for the agent runner (NS-ADR-099).

The runner shares inference with interactive features, so it slows itself
when inference looks congested. Signals: rolling latency, throttled/queue
held responses (HTTP 429/503/timeouts), and OmniRoute health. States:
``normal`` -> ``degraded`` -> ``backed_off``. ``background`` agents are
stretched first and harder; ``time_critical`` agents only slow down in
``backed_off``, and mildly. Recovery steps down one state at a time after
a clear period.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

NORMAL = "normal"
DEGRADED = "degraded"
BACKED_OFF = "backed_off"
_ORDER = (NORMAL, DEGRADED, BACKED_OFF)

#: Interval stretch factors per (state, priority).
STRETCH: dict[str, dict[str, float]] = {
    NORMAL: {"background": 1.0, "time_critical": 1.0},
    DEGRADED: {"background": 2.0, "time_critical": 1.0},
    BACKED_OFF: {"background": 6.0, "time_critical": 1.5},
}


@dataclass(frozen=True)
class BackoffConfig:
    window: timedelta = timedelta(minutes=10)
    degraded_latency_ms: float = 20_000.0
    backed_off_latency_ms: float = 60_000.0
    degraded_throttles: int = 1
    backed_off_throttles: int = 3
    recover_after: timedelta = timedelta(minutes=5)


class BackoffController:
    """Tracks inference pressure and reports the current stretch factors."""

    def __init__(self, config: BackoffConfig | None = None) -> None:
        self.config = config or BackoffConfig()
        self._latency: deque[tuple[datetime, float]] = deque()
        self._throttles: deque[datetime] = deque()
        self._health_ok: bool | None = None
        self._state = NORMAL
        self._since: datetime | None = None
        self._last_signal_level = NORMAL

    # -- signal intake -------------------------------------------------------

    def record_latency(self, now: datetime, latency_ms: float) -> None:
        self._latency.append((now, float(latency_ms)))

    def record_throttle(self, now: datetime) -> None:
        self._throttles.append(now)

    def record_health(self, ok: bool | None) -> None:
        """``None`` means the probe could not tell (no signal)."""
        self._health_ok = ok

    # -- evaluation ----------------------------------------------------------

    def _prune(self, now: datetime) -> None:
        cutoff = now - self.config.window
        while self._latency and self._latency[0][0] < cutoff:
            self._latency.popleft()
        while self._throttles and self._throttles[0] < cutoff:
            self._throttles.popleft()

    def _signal_level(self) -> str:
        cfg = self.config
        level = NORMAL
        if self._health_ok is False:
            level = BACKED_OFF
        throttles = len(self._throttles)
        if throttles >= cfg.backed_off_throttles:
            level = BACKED_OFF
        elif throttles >= cfg.degraded_throttles and level == NORMAL:
            level = DEGRADED
        if self._latency:
            values = sorted(v for _, v in self._latency)
            median = values[len(values) // 2]
            if median >= cfg.backed_off_latency_ms:
                level = BACKED_OFF
            elif median >= cfg.degraded_latency_ms and level == NORMAL:
                level = DEGRADED
        return level

    def evaluate(self, now: datetime) -> str:
        """Update and return the state. Escalates at once, recovers stepwise."""
        self._prune(now)
        level = self._signal_level()
        self._last_signal_level = level
        current = _ORDER.index(self._state)
        target = _ORDER.index(level)
        if target > current:
            self._state, self._since = level, now
        elif target < current:
            if self._since is None or now - self._since >= self.config.recover_after:
                self._state, self._since = _ORDER[current - 1], now
        elif target > 0:
            # Still under pressure at this level: restart the clear timer.
            self._since = now
        return self._state

    @property
    def state(self) -> str:
        return self._state

    def stretch(self, priority: str) -> float:
        table = STRETCH[self._state]
        return table.get(priority, table["background"])

    def snapshot(self, now: datetime) -> dict[str, object]:
        self._prune(now)
        return {
            "state": self._state,
            "stretch": dict(STRETCH[self._state]),
            "throttles_in_window": len(self._throttles),
            "latency_samples": len(self._latency),
            "health_ok": self._health_ok,
            "since": self._since.isoformat() if self._since else None,
        }
