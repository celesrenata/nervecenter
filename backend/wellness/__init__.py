"""Private Wellness Journal plane (Personal-Ops Recovery §10 — workstream I).

Mood check-ins, sleep duration/consistency (consuming workstream H's sleep
data through an injectable seam), energy, stress, journal entries, custom
observations, deterministic trends & correlations, and clinician export on
explicit request — private, user-controlled, local-only.

**Privacy (normative, spec §10):** this package has *no external-model
path* — it imports nothing from ``backend.inference``,
``backend.omniroute``, or any transport/cloud module, and no code path in
it reads wellness rows toward inference or cloud. Trends are
deterministic statistics (means, least-squares slope, Pearson r) with a
whitelisted, data-describing output vocabulary — never diagnosis language,
never auto-disclosure to other agents, services, or models. Enforced by the
import-graph test in ``tests/backend/test_wellness.py``.

Exports
-------
- :class:`WellnessService` — check-in upsert, list/trends/export, control.
- :class:`TrendEngine` — deterministic statistics only (7/30/90-day).
- :class:`CollectionBlockedError` — check-in rejected while disabled/paused.
"""

from __future__ import annotations

from .service import CollectionBlockedError, PRIVACY_STATEMENT, WellnessService
from .trends import TrendEngine

__all__ = [
    "CollectionBlockedError",
    "PRIVACY_STATEMENT",
    "TrendEngine",
    "WellnessService",
]
