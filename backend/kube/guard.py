"""Guard for plans proposed by the kube agent (NS-ADR-120).

Applied to plans with ``guard == "kube"`` when they are proposed and again before each step
runs. Rules: changes name their namespace; known-down namespaces are never changed; protected
namespaces only under an active per-issue override; Secrets are never touched; steps that can
lose data need a verified backup chain and (for repairs) a restore fallback; every plan carries an
``issue_key``. Refusals raise ``GuardError`` with a stable code and never echo argument values.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import yaml

from backend.infra.policy import KUBECTL_READ_VERBS, _mentions_secrets, exec_is_mutating
from backend.kube.settings import KubeOverrideStore, KubeSettingsStore, classify_namespace

_NS_FLAGS = ("-n", "--namespace")
_VALUE_FLAGS = ("-n", "--namespace", "-c", "--container")
#: Resource names (first part of ``kind/name``, before any ``.group``) that are cluster-scoped.
_CLUSTER_RESOURCES = frozenset({
    "ns", "namespace", "namespaces", "node", "nodes", "pv", "persistentvolume",
    "persistentvolumes", "storageclass", "storageclasses", "sc", "crd", "customresourcedefinition",
    "customresourcedefinitions", "clusterrole", "clusterroles", "clusterrolebinding",
    "clusterrolebindings", "apiservice", "apiservices", "priorityclass", "priorityclasses",
    "ingressclass", "ingressclasses", "mutatingwebhookconfiguration",
    "mutatingwebhookconfigurations", "validatingwebhookconfiguration",
    "validatingwebhookconfigurations", "csidriver", "csidrivers", "csinode", "csinodes",
    "runtimeclass", "runtimeclasses", "podsecuritypolicy", "podsecuritypolicies",
})
_CLUSTER_KINDS = frozenset({
    "Namespace", "Node", "PersistentVolume", "StorageClass", "CustomResourceDefinition",
    "ClusterRole", "ClusterRoleBinding", "APIService", "PriorityClass", "IngressClass",
    "MutatingWebhookConfiguration", "ValidatingWebhookConfiguration", "CSIDriver", "CSINode",
    "RuntimeClass", "PodSecurityPolicy",
})
_DATA_DELETE = frozenset({"pvc", "pvcs", "persistentvolumeclaim", "persistentvolumeclaims",
                          "pv", "pvs", "persistentvolume", "persistentvolumes"})
_NFS_MKDIR = re.compile(r"^mkdir -p /[A-Za-z0-9._/-]{1,200}/[a-z0-9-]{1,40}/backups$")
DATA_KINDS = ("repair", "restore")


class GuardError(ValueError):
    """A refusal with a stable reason code (never echoes argument values)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _is_read_like(args: list[str]) -> bool:
    verb = args[0]
    if verb in KUBECTL_READ_VERBS or verb == "wait":
        return True
    return verb == "rollout" and len(args) > 1 and args[1] in ("status", "history")


def _manifest_docs(stdin: Any) -> list[dict[str, Any]]:
    if stdin is None:
        return []
    try:
        docs = [d for d in yaml.safe_load_all(str(stdin)) if d is not None]
    except yaml.YAMLError as exc:
        raise GuardError("bad_manifest") from exc
    out: list[dict[str, Any]] = []
    for d in docs:
        if not isinstance(d, dict):
            raise GuardError("bad_manifest")
        if d.get("kind") == "List" and isinstance(d.get("items"), list):
            out.extend(i for i in d["items"] if isinstance(i, dict))
        else:
            out.append(d)
    return out


def _flag_namespaces(args: list[str]) -> list[str]:
    found: list[str] = []
    for i, a in enumerate(args):
        if a in _NS_FLAGS and i + 1 < len(args):
            found.append(args[i + 1])
        elif a.startswith("--namespace="):
            found.append(a.split("=", 1)[1])
        elif a.startswith("-n") and len(a) > 2 and not a.startswith("--"):
            found.append(a[2:])
    return found


def _resource_tokens(args: list[str]) -> list[str]:
    """Non-flag tokens (and the head of ``kind/name`` or ``a,b``) after the verb."""
    out: list[str] = []
    skip = False
    for a in args[1:]:
        if skip:
            skip = False
            continue
        if a in _VALUE_FLAGS:
            skip = True
            continue
        if a.startswith("-"):
            continue
        for part in a.lower().split(","):
            out.append(part.split("/", 1)[0])
    return out


def namespaces_of(step: dict[str, Any]) -> list[str]:
    """Every namespace a kubectl step names (flags, then stdin manifests).

    Raises GuardError for ``-A`` on a changing step, cluster-scoped changes and changes that
    name no namespace. Read-like steps return whatever they name and are never refused here.
    """
    args = list(step.get("args") or [])
    if not args:
        raise GuardError("bad_step")
    read_like = _is_read_like(args)
    found = _flag_namespaces(args)
    if "-A" in args or "--all-namespaces" in args:
        if not read_like:
            raise GuardError("all_namespaces_forbidden")
        return found
    docs = _manifest_docs(step.get("stdin"))
    if not read_like:
        for tok in _resource_tokens(args):
            if tok.split(".", 1)[0] in _CLUSTER_RESOURCES:
                raise GuardError("cluster_scope_forbidden")
    for doc in docs:
        if doc.get("kind") in _CLUSTER_KINDS:
            if not read_like:
                raise GuardError("cluster_scope_forbidden")
            continue
        ns = (doc.get("metadata") or {}).get("namespace")
        if ns:
            found.append(str(ns))
        elif not read_like and not _flag_namespaces(args):
            raise GuardError("namespace_required")
    if not read_like and not found:
        raise GuardError("namespace_required")
    return sorted(set(found))


def _mentions_secret_doc(docs: list[dict[str, Any]]) -> bool:
    return any(str(d.get("kind", "")).lower() == "secret" for d in docs)


def is_data_step(step: dict[str, Any]) -> bool:
    """repair/restore steps, mutating exec and deletes of pvc/pv/Longhorn volumes."""
    if step.get("kind") in DATA_KINDS:
        return True
    args = step.get("args") or []
    if not args:
        return False
    if args[0] == "exec" and exec_is_mutating(args):
        return True
    if args[0] == "delete":
        for tok in _resource_tokens(args):
            if tok in _DATA_DELETE or tok.startswith("volumes.longhorn.io"):
                return True
    return False


def _closure(steps: list[dict[str, Any]], start: int) -> set[int]:
    """Indexes reachable from ``start`` through ``requires`` (excluding ``start``)."""
    seen: set[int] = set()
    todo = list(steps[start].get("requires") or [])
    while todo:
        i = todo.pop()
        if i in seen or not 0 <= i < len(steps):
            continue
        seen.add(i)
        todo.extend(steps[i].get("requires") or [])
    return seen


def _verified_backups(steps: list[dict[str, Any]], index: int) -> set[int]:
    """backup_verify steps in the chain of ``index`` that themselves require a backup step."""
    good: set[int] = set()
    for j in _closure(steps, index):
        if steps[j].get("kind") == "backup_verify" and any(
                steps[k].get("kind") == "backup" for k in _closure(steps, j)):
            good.add(j)
    return good


class KubeGuard:
    def __init__(self, settings_store: KubeSettingsStore, override_store: KubeOverrideStore
                 ) -> None:
        self._settings = settings_store
        self._overrides = override_store

    async def check_step(self, step: dict[str, Any], plan: dict[str, Any],
                         now: datetime | None = None) -> None:
        """Rules 1-4 for one step (propose time and again just before it runs)."""
        if "args" not in step:
            # SSH steps: only the runbook's mkdir of a backup directory.
            if not _NFS_MKDIR.match(str(step.get("command") or "")):
                raise GuardError("ssh_not_allowed")
            return
        args = list(step["args"])
        if any(_mentions_secrets(a) for a in args[1:]) or _mentions_secret_doc(
                _manifest_docs(step.get("stdin"))):
            raise GuardError("secrets_forbidden")
        namespaces = namespaces_of(step)
        if _is_read_like(args):
            return
        settings = await self._settings.get()
        issue_key = str(plan.get("issue_key") or "")
        active = await self._overrides.active(now)
        for ns in namespaces:
            verdict = classify_namespace(ns, settings, active, issue_key)
            if verdict == "known_down":
                raise GuardError("known_down")
            if verdict == "protected":
                raise GuardError("protected_namespace_no_override")

    async def check_step_now(self, plan: dict[str, Any], index: int,
                             now: datetime | None = None) -> None:
        """Re-check one step with the current settings and overrides (run time)."""
        steps = plan.get("steps") or []
        if not 0 <= index < len(steps):
            raise GuardError("bad_step")
        await self.check_step(steps[index], plan, now)

    async def check_plan(self, steps: list[dict[str, Any]], issue_key: Any,
                         now: datetime | None = None) -> None:
        """All rules for a plan at propose time."""
        if not isinstance(issue_key, str) or not 1 <= len(issue_key) <= 300:
            raise GuardError("issue_key_required")
        plan = {"steps": steps, "issue_key": issue_key}
        for step in steps:
            await self.check_step(step, plan, now)
        for i, step in enumerate(steps):
            if is_data_step(step) and not _verified_backups(steps, i):
                raise GuardError("backup_chain_missing")
        for i, step in enumerate(steps):
            if step.get("kind") != "repair":
                continue
            proofs = _verified_backups(steps, i)
            if not any(
                    s.get("kind") == "restore" and i in (s.get("on_failure_of") or [])
                    and proofs & _verified_backups(steps, j)
                    for j, s in enumerate(steps)):
                raise GuardError("restore_path_missing")


__all__ = ["GuardError", "KubeGuard", "is_data_step", "namespaces_of"]
