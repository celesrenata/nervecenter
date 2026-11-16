"""The one place the OmniRoute keys are resolved (NS-ADR-117).

Two keys exist (NS-ADR-110): the *regular* key (inference, route guard, mail
observer) and the *privileged* (management) key (Skill Hub catalog and the
read-only OmniRoute MCP tools). Each resolves as: value stored in the encrypted
vault (set from Settings) first, else the Kubernetes Secret env var. Clearing
the vault value falls back to the env var.

The vault values are cached in this process (:func:`apply_vault_values`) so
sync and async consumers read the effective key on every request with no I/O.
The cache is refreshed at startup, right after a write through the key
endpoints, and on every agent-runner tick, so a changed key takes effect in
every replica without a restart.

Consumers call :func:`regular_key`, :func:`privileged_key` or
:func:`skillhub_key`; nothing else reads the ``NERVE_OMNIROUTE_*`` key env vars
for these purposes. Values are never logged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime

REGULAR = "regular"
PRIVILEGED = "privileged"
KINDS = (REGULAR, PRIVILEGED)

REGULAR_ENV = "NERVE_OMNIROUTE_API_KEY"
PRIVILEGED_ENV = "NERVE_OMNIROUTE_PRIVILEGED_KEY"
SKILLHUB_ENV = "NERVE_SKILLHUB_KEY"

_ENV = {REGULAR: REGULAR_ENV, PRIVILEGED: PRIVILEGED_ENV}

#: ``secrets`` table address of each vault value.
SECRET_KIND = {REGULAR: "omniroute_regular_key", PRIVILEGED: "omniroute_privileged_key"}
SECRET_OWNER_KIND = "system"
SECRET_OWNER_ID = "omniroute"

_vault_values: dict[str, str] = {}
_vault_changed: dict[str, datetime | None] = {}


def apply_vault_values(
    values: Mapping[str, str], changed: Mapping[str, datetime | None] | None = None
) -> None:
    """Replace the in-process copy of the vault values (blank values are dropped)."""
    _vault_values.clear()
    _vault_values.update({k: v.strip() for k, v in values.items() if k in KINDS and v.strip()})
    _vault_changed.clear()
    _vault_changed.update(dict(changed or {}))


def reset() -> None:
    """Forget every vault value (tests)."""
    apply_vault_values({})


def _env(kind: str) -> str:
    return os.environ.get(_ENV[kind], "").strip()


def key_for(kind: str) -> str | None:
    """The effective key for ``kind``: vault value, else env, else ``None``."""
    return _vault_values.get(kind) or _env(kind) or None


def regular_key() -> str | None:
    return key_for(REGULAR)


def privileged_key() -> str | None:
    return key_for(PRIVILEGED)


def skillhub_key() -> str | None:
    """Skill Hub order (NS-ADR-111): ``NERVE_SKILLHUB_KEY``, privileged, regular."""
    return os.environ.get(SKILLHUB_ENV, "").strip() or privileged_key() or regular_key()


def source_for(kind: str) -> str:
    """``vault``, ``env`` or ``none``; never the value."""
    if _vault_values.get(kind):
        return "vault"
    return "env" if _env(kind) else "none"


def status_for(kind: str) -> dict[str, object]:
    """Set/not-set view of one key for the Settings screen (no value)."""
    changed = _vault_changed.get(kind)
    return {
        "configured": key_for(kind) is not None,
        "source": source_for(kind),
        "vault_set": kind in _vault_values,
        "changed_at": changed.isoformat() if changed else None,
    }
