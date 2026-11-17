"""Deterministic read-only cluster snapshot (NS-ADR-120).

Every read goes through ``InfraService.run_read`` (kubectl read verbs only; Secrets are refused by
the infra policy and never requested here). Paging and per-object fallbacks come from
``backend.pentest.inventory``. Each problem carries a stable ``key``
``<kind>/<namespace>/<workload>/<reason>`` so the same fault keeps its key when pods are replaced.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.pentest.inventory import MAX_NAMESPACES, MAX_OBJECTS, _items, _json, _kubectl

LONGHORN_CRD = "volumes.longhorn.io"
CERT_CRD = "certificates.cert-manager.io"
LONGHORN_NS = "longhorn-system"
CERT_WARN_DAYS = 14
EVENT_WINDOW = timedelta(hours=1)
PENDING_GRACE = timedelta(minutes=5)
RESTART_WINDOW = timedelta(hours=24)
HIGH_RESTARTS = 5
CLUSTER = "_cluster"
_QUIET_WAITING = {"ContainerCreating", "PodInitializing"}
_HASH = re.compile(r"^(.+)-[a-z0-9]{8,10}$")
_POD_SUFFIX = re.compile(r"-[a-z0-9]{5}$")
_JOB_SUFFIX = re.compile(r"-\d{6,}$")
_MAX_EVIDENCE = 400


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def workload_of(pod: dict[str, Any]) -> str:
    """Owning workload name: ReplicaSet hash, Job timestamp and pod suffix are stripped."""
    meta = pod.get("metadata") or {}
    name = str(meta.get("name", ""))
    owners = meta.get("ownerReferences") or []
    owner = next((o for o in owners if o.get("controller")), owners[0] if owners else None)
    if owner:
        base, kind = str(owner.get("name", "")), owner.get("kind")
        if kind == "ReplicaSet":
            m = _HASH.match(base)
            return m.group(1) if m else base
        if kind == "Job":
            return _JOB_SUFFIX.sub("", base)
        return base
    stripped = _POD_SUFFIX.sub("", name)
    m = _HASH.match(stripped)
    return m.group(1) if m else stripped


def _problem(kind: str, namespace: str, name: str, reason: str, severity: str,
             **extra: Any) -> dict[str, Any]:
    key = f"{kind}/{namespace or CLUSTER}/{name}/{reason}"
    return {"key": key, "kind": kind, "namespace": namespace, "name": name, "reason": reason,
            "severity": severity, **extra}


def _last_termination(status: dict[str, Any]) -> dict[str, Any] | None:
    term = (status.get("lastState") or {}).get("terminated")
    if not term:
        return None
    return {"reason": term.get("reason"), "exit_code": term.get("exitCode"),
            "finished_at": term.get("finishedAt")}


def classify_pod(pod: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """One problem for an unhealthy pod, or None."""
    meta, status = pod.get("metadata") or {}, pod.get("status") or {}
    ns, name, phase = meta.get("namespace", ""), meta.get("name", ""), status.get("phase")
    if phase == "Succeeded" or meta.get("deletionTimestamp"):
        return None
    workload = workload_of(pod)
    created = _ts(meta.get("creationTimestamp"))
    old_enough = created is None or now - created >= PENDING_GRACE
    base = {"pod": name, "workload": workload}
    if phase == "Failed":
        reason = str(status.get("reason") or "Failed")
        return _problem("pod", ns, workload, reason, "warning", **base,
                        message=str(status.get("message", ""))[:_MAX_EVIDENCE])
    statuses = (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or [])
    for cs in statuses:
        waiting = (cs.get("state") or {}).get("waiting")
        if waiting and waiting.get("reason") and waiting["reason"] not in _QUIET_WAITING:
            return _problem("pod", ns, workload, str(waiting["reason"]), "critical", **base,
                            container=cs.get("name"), restarts=cs.get("restartCount", 0),
                            last_termination=_last_termination(cs),
                            message=str(waiting.get("message", ""))[:_MAX_EVIDENCE])
    for cs in statuses:
        term = _last_termination(cs)
        finished = _ts((term or {}).get("finished_at"))
        if (cs.get("restartCount", 0) >= HIGH_RESTARTS and finished
                and now - finished <= RESTART_WINDOW):
            return _problem("pod", ns, workload, "HighRestarts", "warning", **base,
                            container=cs.get("name"), restarts=cs.get("restartCount", 0),
                            last_termination=term)
    if phase == "Pending" and old_enough:
        cond = next((c for c in status.get("conditions") or []
                     if c.get("type") == "PodScheduled" and c.get("status") == "False"), {})
        return _problem("pod", ns, workload, "Pending", "warning", **base,
                        scheduler_message=str(cond.get("message", ""))[:_MAX_EVIDENCE])
    if phase == "Running" and old_enough and any(
            c.get("type") == "Ready" and c.get("status") == "False"
            for c in status.get("conditions") or []):
        return _problem("pod", ns, workload, "NotReady", "warning", **base)
    return None


def _merge(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per key; ``pods`` lists every pod behind it."""
    merged: dict[str, dict[str, Any]] = {}
    for p in problems:
        cur = merged.get(p["key"])
        if cur is None:
            merged[p["key"]] = {**p, "pods": [p["pod"]]}
        elif p["pod"] not in cur["pods"] and len(cur["pods"]) < 20:
            cur["pods"].append(p["pod"])
    return list(merged.values())


def classify_pvc(pvc: dict[str, Any]) -> dict[str, Any] | None:
    meta, spec, status = pvc.get("metadata") or {}, pvc.get("spec") or {}, pvc.get("status") or {}
    phase = status.get("phase")
    if phase == "Bound":
        return None
    return _problem("pvc", meta.get("namespace", ""), meta.get("name", ""), str(phase or "Unknown"),
                    "critical" if phase == "Lost" else "warning",
                    storage_class=spec.get("storageClassName"), volume=spec.get("volumeName"))


def classify_pv(pv: dict[str, Any]) -> dict[str, Any] | None:
    meta, spec, status = pv.get("metadata") or {}, pv.get("spec") or {}, pv.get("status") or {}
    phase = status.get("phase")
    if phase in ("Bound", "Available"):
        return None
    claim = spec.get("claimRef") or {}
    return _problem("pv", "", meta.get("name", ""), str(phase or "Unknown"),
                    "critical" if phase == "Failed" else "warning",
                    claim=f"{claim.get('namespace', '')}/{claim.get('name', '')}".strip("/"),
                    storage_class=spec.get("storageClassName"),
                    message=str(status.get("message", ""))[:_MAX_EVIDENCE])


def classify_volume(vol: dict[str, Any], replicas: list[dict[str, Any]]) -> dict[str, Any] | None:
    meta, status = vol.get("metadata") or {}, vol.get("status") or {}
    name = meta.get("name", "")
    robustness, state = str(status.get("robustness") or ""), str(status.get("state") or "")
    unhealthy = robustness in ("degraded", "faulted") or (
        state == "attached" and robustness not in ("healthy", ""))
    if not unhealthy:
        return None
    mine = [r for r in replicas if (r.get("spec") or {}).get("volumeName") == name]
    return _problem("volume", LONGHORN_NS, name, robustness or "unhealthy",
                    "critical" if robustness == "faulted" else "warning",
                    state=state, replicas=[
                        {"name": (r.get("metadata") or {}).get("name"),
                         "node": (r.get("spec") or {}).get("nodeID"),
                         "state": (r.get("status") or {}).get("currentState"),
                         "failed_at": (r.get("spec") or {}).get("failedAt") or None}
                        for r in mine][:10])


_PRESSURE = ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")


def classify_node(node: dict[str, Any]) -> list[dict[str, Any]]:
    name = (node.get("metadata") or {}).get("name", "")
    out: list[dict[str, Any]] = []
    for c in (node.get("status") or {}).get("conditions") or []:
        ctype, cstatus = c.get("type"), c.get("status")
        if ctype == "Ready" and cstatus != "True":
            out.append(_problem("node", "", name, "NotReady", "critical",
                                message=str(c.get("message", ""))[:_MAX_EVIDENCE]))
        elif ctype in _PRESSURE and cstatus == "True":
            out.append(_problem("node", "", name, str(ctype), "warning",
                                message=str(c.get("message", ""))[:_MAX_EVIDENCE]))
    return out


def classify_cert(cert: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    meta, status = cert.get("metadata") or {}, cert.get("status") or {}
    not_after = _ts(status.get("notAfter"))
    if not_after is None:
        return None
    if not_after <= now:
        reason, severity = "Expired", "critical"
    elif not_after - now <= timedelta(days=CERT_WARN_DAYS):
        reason, severity = "Expiring", "warning"
    else:
        return None
    return _problem("cert", meta.get("namespace", ""), meta.get("name", ""), reason, severity,
                    not_after=status.get("notAfter"))


def dedupe_events(events: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Warning events from the last hour, one per (namespace, object, reason)."""
    seen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for ev in events:
        if ev.get("type") != "Warning":
            continue
        when = _ts(ev.get("lastTimestamp") or ev.get("eventTime")
                   or (ev.get("metadata") or {}).get("creationTimestamp"))
        if when is None or now - when > EVENT_WINDOW:
            continue
        obj = ev.get("involvedObject") or {}
        key = (str(obj.get("namespace") or (ev.get("metadata") or {}).get("namespace") or ""),
               str(obj.get("kind", "")), str(obj.get("name", "")), str(ev.get("reason", "")))
        cur = seen.get(key)
        count = int(ev.get("count") or 1)
        if cur is None:
            seen[key] = {"namespace": key[0], "kind": key[1], "object": key[2], "reason": key[3],
                         "message": str(ev.get("message", ""))[:_MAX_EVIDENCE], "count": count,
                         "last": when.isoformat()}
        else:
            cur["count"] += count
    return list(seen.values())[:200]


async def _cluster_items(infra: Any, connector: str, kind: str, warnings: list[str]
                         ) -> list[dict[str, Any]]:
    """Cluster-scoped list; one object at a time when the page is too large."""
    data = _json(await _kubectl(infra, connector, ["get", kind, "-o", "json"]))
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [i for i in data["items"] if isinstance(i, dict)]
    names = await _kubectl(infra, connector, ["get", kind, "-o", "name"])
    out: list[dict[str, Any]] = []
    for line in (names or "").splitlines()[:MAX_OBJECTS]:
        obj = _json(await _kubectl(infra, connector, ["get", line.strip(), "-o", "json"]))
        if isinstance(obj, dict):
            out.append(obj)
    if not out:
        warnings.append(f"{kind} could not be read")
    return out


async def _crd_present(infra: Any, connector: str, crd: str) -> bool:
    return await _kubectl(infra, connector, ["get", "crd", crd, "-o", "name"]) is not None


async def collect_snapshot(infra: Any, connector: str, now: datetime | None = None
                           ) -> dict[str, Any]:
    """Read-only snapshot of the cluster behind ``connector``."""
    now = now or datetime.now(UTC)
    warnings: list[str] = []
    ns_text = await _kubectl(infra, connector, ["get", "namespaces", "-o", "name"])
    namespaces = [ln.strip().split("/", 1)[-1] for ln in (ns_text or "").splitlines()
                  if ln.strip()][:MAX_NAMESPACES]
    if not namespaces:
        warnings.append("namespaces could not be read")
    pods: list[dict[str, Any]] = []
    pvcs: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for ns in namespaces:
        for p in await _items(infra, connector, "pods", ns, warnings):
            problem = classify_pod(p, now)
            if problem:
                pods.append(problem)
        pvcs.extend(c for c in (classify_pvc(i) for i in await _items(
            infra, connector, "persistentvolumeclaims", ns, warnings)) if c)
        events.extend(await _items(infra, connector, "events", ns, warnings))
    unhealthy = _merge(pods)
    pvs = [p for p in (classify_pv(i) for i in await _cluster_items(
        infra, connector, "persistentvolumes", warnings)) if p]
    nodes: list[dict[str, Any]] = []
    for n in await _cluster_items(infra, connector, "nodes", warnings):
        nodes.extend(classify_node(n))
    volumes: list[dict[str, Any]] = []
    if await _crd_present(infra, connector, LONGHORN_CRD):
        replicas = await _items(infra, connector, "replicas.longhorn.io", LONGHORN_NS, warnings)
        volumes = [v for v in (classify_volume(i, replicas) for i in await _items(
            infra, connector, LONGHORN_CRD, LONGHORN_NS, warnings)) if v]
    else:
        warnings.append("Longhorn volumes skipped: CRD volumes.longhorn.io is not installed")
    certs: list[dict[str, Any]] = []
    if await _crd_present(infra, connector, CERT_CRD):
        for ns in namespaces:
            certs.extend(c for c in (classify_cert(i, now) for i in await _items(
                infra, connector, CERT_CRD, ns, warnings)) if c)
    else:
        warnings.append("certificates skipped: CRD certificates.cert-manager.io is not installed")
    return {
        "collected_at": now.isoformat(), "namespaces": len(namespaces),
        "pods_unhealthy": unhealthy,
        "pending_pods": [p for p in unhealthy if p["reason"] == "Pending"],
        "pvcs": pvcs, "pvs": pvs, "longhorn_volumes": volumes, "nodes": nodes,
        "events": dedupe_events(events, now), "certificates": certs, "warnings": warnings,
    }


SECTIONS = ("pods_unhealthy", "pvcs", "pvs", "longhorn_volumes", "nodes", "certificates")


def snapshot_problems(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Every problem in a snapshot (one per key)."""
    seen: dict[str, dict[str, Any]] = {}
    for section in SECTIONS:
        for p in snapshot.get(section) or []:
            seen.setdefault(p["key"], p)
    return list(seen.values())
