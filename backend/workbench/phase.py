"""Workbench phase gating and environment-driven settings.

WORKBENCH_PHASE (0-3, default 0) gates which routers are mounted. Values that
are missing, non-integer or out of range fall back to 0 (everything off).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

DEFAULT_PHASE = 0
MIN_PHASE = 0
MAX_PHASE = 3
DEFAULT_UID = 1000
DEFAULT_GID = 1000
DEFAULT_MODEL = "omniroute/local/code"
DEFAULT_SMALL_MODEL = "local/fast"

_TRUE = {"1", "true", "yes", "on"}


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _int(raw: str | None, default: int, low: int, high: int | None = None) -> int:
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    if value < low or (high is not None and value > high):
        return default
    return value


def workbench_phase(env: Mapping[str, str] | None = None) -> int:
    return _int(_env(env).get("WORKBENCH_PHASE"), DEFAULT_PHASE, MIN_PHASE, MAX_PHASE)


def phase_at_least(minimum: int, env: Mapping[str, str] | None = None) -> bool:
    return workbench_phase(env) >= minimum


def ide_enabled(env: Mapping[str, str] | None = None) -> bool:
    raw = _env(env).get("WORKBENCH_IDE_ENABLED", "")
    return raw.strip().lower() in _TRUE


def workbench_uid(env: Mapping[str, str] | None = None) -> int:
    return _int(_env(env).get("WORKBENCH_UID"), DEFAULT_UID, 0)


def workbench_gid(env: Mapping[str, str] | None = None) -> int:
    return _int(_env(env).get("WORKBENCH_GID"), DEFAULT_GID, 0)


def default_model(env: Mapping[str, str] | None = None) -> str:
    return _env(env).get("WORKBENCH_DEFAULT_MODEL", "").strip() or DEFAULT_MODEL


def default_small_model(env: Mapping[str, str] | None = None) -> str:
    return _env(env).get("WORKBENCH_DEFAULT_SMALL_MODEL", "").strip() or DEFAULT_SMALL_MODEL
