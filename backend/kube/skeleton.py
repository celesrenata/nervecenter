"""Runbook skeletons for the agent tools (NS-ADR-120): kind + parameters -> plan steps.

One place maps the ``kube_runbook`` tool's arguments to the builders in ``runbooks``. The same
call expands a ``runbook`` object inside ``propose_plan`` (the manifests are long, the agent has
three tool rounds, so the system fills in the steps from the same arguments). Parameters are
whitelisted per kind; an unknown parameter or a builder ``ValueError`` is a ``RunbookError``.
"""
from __future__ import annotations

from typing import Any

from backend.kube import runbooks as rb

_BACKUP_KW = ("host", "user", "secret_name", "secret_key", "image")
#: kind -> (builder, required params, optional params)
KINDS: dict[str, tuple[Any, tuple[str, ...], tuple[str, ...]]] = {
    "backup": (rb.backup_steps, ("dbtype", "namespace", "workload"), _BACKUP_KW),
    "mysql_repair": (rb.mysql_repair_steps, ("namespace", "pod", "workload"), _BACKUP_KW),
    "redis_repair": (rb.redis_repair_steps, ("namespace", "pod", "workload", "data_pvc"),
                     ("aof_path", *_BACKUP_KW)),
    "postgres": (rb.postgres_steps, ("namespace", "workload"), _BACKUP_KW),
    "nextcloud_upgrade": (rb.nextcloud_upgrade_steps,
                          ("namespace", "pod", "db_workload", "config_pvc"),
                          ("db_type", "db_namespace", *_BACKUP_KW)),
    "crashloop": (rb.crashloop_steps, ("namespace", "workload"), ("workload_kind",)),
    "imagepull": (rb.imagepull_steps, ("namespace", "workload"),
                  ("workload_kind", "container", "fixed_image")),
    "pvc": (rb.pvc_steps, ("namespace", "pvc", "volume"), ("pod", "delete_pvc")),
}
_KEEP_STAMP = ("backup", "mysql_repair", "redis_repair", "postgres", "nextcloud_upgrade", "pvc")


class RunbookError(ValueError):
    pass


def build_steps(runbook: Any, settings: dict[str, Any], connector: str,
                ssh_connector: str | None = None) -> list[dict[str, Any]]:
    """Steps for ``{"kind": ..., <params>, "stamp": "<14 digits>"}``; raises ``RunbookError``."""
    if not isinstance(runbook, dict):
        raise RunbookError("runbook must be an object")
    kind = str(runbook.get("kind") or "")
    if kind not in KINDS:
        raise RunbookError(f"kind must be one of {', '.join(sorted(KINDS))}")
    builder, required, optional = KINDS[kind]
    allowed = {"kind", "stamp", "ssh_connector", *required, *optional}
    extra = sorted(str(k) for k in runbook if k not in allowed)
    if extra:
        raise RunbookError(f"unknown parameter {extra[0][:40]}")
    missing = [p for p in required if runbook.get(p) in (None, "")]
    if missing:
        raise RunbookError(f"{missing[0]} is required for {kind}")
    params = {k: runbook[k] for k in (*required, *optional) if runbook.get(k) not in (None, "")}
    kwargs: dict[str, Any] = {"connector": connector}
    if kind in _KEEP_STAMP:
        kwargs["stamp"] = runbook.get("stamp") or rb.new_stamp()
    if kind in ("backup", "mysql_repair", "redis_repair", "postgres", "nextcloud_upgrade"):
        kwargs["ssh_connector"] = ssh_connector
    try:
        positional = [params.pop(p) for p in _POSITIONAL[kind]]
        if kind in _WITH_SETTINGS:
            positional.append(settings)
        return builder(*positional, **kwargs, **params)
    except (ValueError, TypeError, KeyError) as exc:
        raise RunbookError(str(exc)[:200] or "invalid runbook") from exc


#: Positional parameters of each builder (the rest go by keyword).
_POSITIONAL = {
    "backup": ("dbtype", "namespace", "workload"),
    "mysql_repair": ("namespace", "pod", "workload"),
    "redis_repair": ("namespace", "pod", "workload"),
    "postgres": ("namespace", "workload"),
    "nextcloud_upgrade": ("namespace", "pod"),
    "crashloop": ("namespace", "workload"),
    "imagepull": ("namespace", "workload"),
    "pvc": ("namespace", "pvc", "volume"),
}
_WITH_SETTINGS = ("backup", "mysql_repair", "redis_repair", "postgres", "nextcloud_upgrade")


def summarize(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Step list without the manifests (``stdin`` becomes its length)."""
    out: list[dict[str, Any]] = []
    for i, step in enumerate(steps):
        row = {k: v for k, v in step.items() if k != "stdin"}
        row["index"] = i
        if step.get("stdin"):
            row["stdin_chars"] = len(step["stdin"])
        out.append(row)
    return out


__all__ = ["KINDS", "RunbookError", "build_steps", "summarize"]
