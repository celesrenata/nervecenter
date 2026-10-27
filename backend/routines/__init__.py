"""Routine Engine plane (Personal-Ops Recovery §9 — workstream H).

Durable routine schedules + deterministic wall-clock evaluation + the one-time
gentle missed-window note + the deterministic clock-based wake fallback alarm
+ the sleep-record plane (explicit records only — missing data never proves
asleep or awake).

Hard rules (spec §9):

- A **missed routine is a one-time gentle note only** — no streak counters,
  no guilt copy, and the same schedule window is never re-flagged (the window
  marker lives in ``routines.missed_flags`` JSONB; the engine merges new
  markers through :meth:`RoutinesRepository.merge_missed_flags`, which keeps
  the first timestamp per key).
- **Missing sleep data never proves asleep or awake.** A query window with
  fewer than three records reports ``regularity: "unknown"`` — never an
  assertion of state.
- **Wake-up requires a deterministic clock-based fallback alarm.** The alarm
  is server-computed from the routine's schedule (the next local slot in the
  routine's own IANA timezone) and persisted to
  ``routines.deterministic_fallback_alarm`` as ``{"at": iso, "timezone": ...,
  "kind": "clock_fallback", "trigger_level": N}``. It fires through
  NerveCenter's own durable attention path — with *no* dependence on Apple
  Health, device wake, or any device push channel.

Public surface
--------------
- :class:`RoutineService` — orchestration over :class:`RoutinesRepository`
  (schedule evaluation, missed-window handling, wake fallback alarm,
  attention mirroring, check-in/complete, sleep records + regularity,
  the Today projection).
- :func:`compute_wake_fallback_alarm` — the pure, deterministic wake-alarm
  computation (next local slot in the routine's timezone).
- :class:`RoutinesTodayProvider` — Today ``coming_up`` / ``handled``
  contributor (``needs_you`` stays to the attention mirroring — no
  double-counting).
- :func:`register_routines_today_provider` — idempotent registration; the
  integrator calls it at startup, this package never edits
  ``backend/api/endpoints/today.py``.
- :func:`make_wellness_sleep_provider` — factory for the wellness sleep
  seam: an async callable ``(days) -> list[{bed_at, wake_at,
  duration_minutes, source}]`` (ISO strings) built from durable sleep
  records, or ``[]`` when degraded. The integrator binds it to
  ``app.state.wellness_sleep_provider``.

Import note: the Today aggregator is imported lazily (at registration
time), mirroring the bills package — the routines plane itself never
imports ``backend.api`` at module level, so it stays importable in any
order and testable without the full API.
"""

from .service import (
    RoutinesRepository,
    RoutinesService,
    RoutinesTodayProvider,
    compute_wake_fallback_alarm,
    make_wellness_sleep_provider,
    register_routines_today_provider,
)

__all__ = [
    "RoutinesRepository",
    "RoutinesService",
    "RoutinesTodayProvider",
    "compute_wake_fallback_alarm",
    "make_wellness_sleep_provider",
    "register_routines_today_provider",
]
