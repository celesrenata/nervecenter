"""In-memory live log of agent runs (what the owner watches while an agent works).

Each agent keeps its last few runs; each run is an ordered list of events
(run started, skills chosen, model rounds, tool calls and results, finish).
Event numbers increase per agent across runs, so a client polls with
``after=<last seq>`` and receives only new events. Nothing here is persisted:
the run's final output is still recorded by the store as before.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

RUNS_KEPT = 5
EVENTS_PER_RUN = 500
TEXT_MAX = 4000


def _clip(text: str, limit: int = TEXT_MAX) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


@dataclass
class LiveRun:
    run_key: int
    started_at: str
    trigger: str
    status: str = "running"
    finished_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def meta(self) -> dict[str, Any]:
        return {
            "run_key": self.run_key,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "trigger": self.trigger,
            "status": self.status,
            "events": len(self.events),
        }


class LiveLog:
    def __init__(self) -> None:
        self._runs: dict[str, deque[LiveRun]] = {}
        self._seq: dict[str, int] = {}

    def _next(self, agent_id: str) -> int:
        self._seq[agent_id] = self._seq.get(agent_id, 0) + 1
        return self._seq[agent_id]

    def start(self, agent_id: str, trigger: str) -> LiveRun:
        runs = self._runs.setdefault(agent_id, deque(maxlen=RUNS_KEPT))
        run = LiveRun(run_key=self._next(agent_id), started_at=_now(), trigger=trigger)
        runs.append(run)
        self.emit(agent_id, run, "run_started", f"Run started ({trigger})")
        return run

    def emit(
        self, agent_id: str, run: LiveRun, kind: str, text: str, data: dict[str, Any] | None = None
    ) -> None:
        if len(run.events) >= EVENTS_PER_RUN:
            return
        run.events.append(
            {
                "seq": self._next(agent_id),
                "run_key": run.run_key,
                "at": _now(),
                "kind": kind,
                "text": _clip(text),
                **({"data": data} if data else {}),
            }
        )

    def finish(self, agent_id: str, run: LiveRun, status: str, text: str) -> None:
        self.emit(agent_id, run, "run_finished", text, {"status": status})
        run.status = status
        run.finished_at = _now()

    def view(self, agent_id: str, after: int = 0, run_key: int | None = None) -> dict[str, Any]:
        runs = list(self._runs.get(agent_id, ()))
        chosen = next((r for r in runs if r.run_key == run_key), None) if run_key else None
        if chosen is None and runs:
            chosen = runs[-1]
        events = [e for e in chosen.events if e["seq"] > after] if chosen else []
        return {
            "running": any(r.status == "running" for r in runs),
            "run": chosen.meta() if chosen else None,
            "runs": [r.meta() for r in reversed(runs)],
            "events": events,
            "last_seq": self._seq.get(agent_id, 0),
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def clip(text: str, limit: int = TEXT_MAX) -> str:
    return _clip(text, limit)
