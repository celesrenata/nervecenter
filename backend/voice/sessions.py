"""In-memory voice session registry: server-side mute with a grace period."""
from __future__ import annotations

import time
from collections.abc import Callable

MUTE_GRACE_SECONDS = 30.0
SESSION_TTL_SECONDS = 2 * 3600.0


class VoiceSessionRegistry:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # (sub, session_id) -> [muted_at | None, last_seen]
        self._sessions: dict[tuple[str, str], list[float | None]] = {}

    def _prune(self, now: float) -> None:
        stale = [k for k, v in self._sessions.items() if now - (v[1] or 0) > SESSION_TTL_SECONDS]
        for key in stale:
            del self._sessions[key]

    def set_muted(self, sub: str, session_id: str, muted: bool, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        self._prune(now)
        entry = self._sessions.setdefault((sub, session_id), [None, now])
        entry[1] = now
        if not muted:
            entry[0] = None
        elif entry[0] is None:
            entry[0] = now

    def should_synthesize(self, sub: str, session_id: str, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        self._prune(now)
        entry = self._sessions.get((sub, session_id))
        if entry is None or entry[0] is None:
            return True
        entry[1] = now
        return now - entry[0] <= MUTE_GRACE_SECONDS
