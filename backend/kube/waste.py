"""Read-only waste report for the kube agent (NS-ADR-120).

Reads ``kubectl top`` (metrics-server) and joins it with pod requests from ``get pods -o json``
(paged per namespace like the collector). Reports idle and over-sized pods, unused PVCs, orphaned
PVs, restart rates and the CPU burned by crash-looping pods. The crash-loop figure is an
estimate (restarts x last container uptime x CPU request) and is labelled as one. Unused images
cannot be measured through kubectl and are listed under ``not_measured``. Prometheus is optional:
a missing or failing Prometheus is "not used", never an error.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from backend.kube.collector import _ts
from backend.pentest.inventory import MAX_NAMESPACES, _items, _json, _kubectl

log = structlog.get_logger(__name__)

IDLE_RATIO = 0.10
OVERSIZED_RATIO = 0.25
OVERSIZED_MIN_CPU_M = 200.0
OVERSIZED_MIN_MEM_MI = 256.0
TOP_N = 25
PROM_TIMEOUT_S = 8.0
ZERO_TRAFFIC_QUERY = "sum by (service) (increase(traefik_service_requests_total[7d])) == 0"
_MEM = {"Ki": 1 / 1024, "Mi": 1.0, "Gi": 1024.0, "Ti": 1024.0 * 1024, "K": 1000 / 1048576,
        "M": 1e6 / 1048576, "G": 1e9 / 1048576, "": 1 / 1048576}
_MEM_RE = re.compile(r"^([0-9.]+)([A-Za-z]*)$")


def cpu_millicores(value: Any) -> float:
    """``250m`` -> 250, ``2`` -> 2000, ``1500000n`` -> 1.5. Unparseable -> 0."""
    text = str(value or "").strip()
    try:
        if text.endswith("n"):
            return float(text[:-1]) / 1_000_000
        if text.endswith("u"):
            return float(text[:-1]) / 1_000
        if text.endswith("m"):
            return float(text[:-1])
        return float(text) * 1000 if text else 0.0
    except ValueError:
        return 0.0


def mem_mib(value: Any) -> float:
    """``128Mi`` -> 128, ``1Gi`` -> 1024, plain bytes accepted. Unparseable -> 0."""
    m = _MEM_RE.match(str(value or "").strip())
    if not m or m.group(2) not in _MEM:
        return 0.0
    try:
        return float(m.group(1)) * _MEM[m.group(2)]
    except ValueError:
        return 0.0


def parse_top_pods(text: str | None) -> dict[tuple[str, str], tuple[float, float]]:
    """``top pods -A --no-headers`` -> {(namespace, pod): (cpu millicores, memory MiB)}."""
    out: dict[tuple[str, str], tuple[float, float]] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 4:
            out[(parts[0], parts[1])] = (cpu_millicores(parts[2]), mem_mib(parts[3]))
    return out


def parse_top_nodes(text: str | None) -> list[dict[str, Any]]:
    """``top nodes --no-headers``: name, cpu, cpu%, memory, memory%."""
    nodes: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 5:
            nodes.append({"name": parts[0], "cpu_m": round(cpu_millicores(parts[1])),
                          "cpu_percent": parts[2], "mem_mib": round(mem_mib(parts[3])),
                          "mem_percent": parts[4]})
    return nodes


def _requests(pod: dict[str, Any]) -> tuple[float, float]:
    cpu = mem = 0.0
    for c in (pod.get("spec") or {}).get("containers") or []:
        req = (c.get("resources") or {}).get("requests") or {}
        cpu += cpu_millicores(req.get("cpu"))
        mem += mem_mib(req.get("memory"))
    return cpu, mem


def _claims(pod: dict[str, Any]) -> set[str]:
    return {str((v.get("persistentVolumeClaim") or {}).get("claimName"))
            for v in (pod.get("spec") or {}).get("volumes") or []
            if v.get("persistentVolumeClaim")}


def _restart_stats(pod: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    restarts = sum(int(s.get("restartCount") or 0) for s in statuses)
    if restarts <= 0:
        return None
    started = _ts((pod.get("status") or {}).get("startTime")) or _ts(
        (pod.get("metadata") or {}).get("creationTimestamp"))
    hours = max(((now - started).total_seconds() / 3600) if started else 1.0, 1.0)
    crash = False
    uptime_s = 0.0
    for s in statuses:
        if (((s.get("state") or {}).get("waiting") or {}).get("reason")) == "CrashLoopBackOff":
            crash = True
        term = (s.get("lastState") or {}).get("terminated") or {}
        begin, end = _ts(term.get("startedAt")), _ts(term.get("finishedAt"))
        if begin and end:
            uptime_s = max(uptime_s, (end - begin).total_seconds())
    cpu_req, _ = _requests(pod)
    meta = pod.get("metadata") or {}
    return {"namespace": meta.get("namespace", ""), "pod": meta.get("name", ""),
            "restarts": restarts, "restarts_per_hour": round(restarts / hours, 2),
            "crash_looping": crash,
            "estimated_cpu_core_seconds": round(restarts * uptime_s * cpu_req / 1000, 1)
            if crash else 0.0}


async def _prometheus(url: str | None, transport: httpx.AsyncBaseTransport | None
                      ) -> dict[str, Any]:
    if not url:
        return {"used": False, "reason": "no prometheus_url set"}
    try:
        async with httpx.AsyncClient(timeout=PROM_TIMEOUT_S, transport=transport) as client:
            resp = await client.get(f"{url.rstrip('/')}/api/v1/query",
                                    params={"query": ZERO_TRAFFIC_QUERY})
        data = resp.json() if resp.status_code == 200 else None
        if not isinstance(data, dict) or data.get("status") != "success":
            return {"used": False, "reason": "prometheus did not answer the query"}
        rows = (data.get("data") or {}).get("result") or []
        return {"used": True, "zero_traffic_services": sorted(
            str((r.get("metric") or {}).get("service", ""))[:120] for r in rows)[:TOP_N]}
    except (httpx.HTTPError, ValueError) as exc:
        log.info("kube.waste.prometheus_unavailable", error=type(exc).__name__)
        return {"used": False, "reason": "prometheus is not reachable"}


async def collect_waste(infra: Any, connector: str, prometheus_url: str | None = None, *,
                        now: datetime | None = None,
                        transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Read-only waste report; every list is capped at 25 items, counts are complete."""
    now = now or datetime.now(UTC)
    warnings: list[str] = []
    top_text = await _kubectl(infra, connector, ["top", "pods", "-A", "--no-headers"])
    usage = parse_top_pods(top_text)
    if top_text is None:
        warnings.append("kubectl top pods failed: metrics-server is missing or unreachable")
    nodes = parse_top_nodes(await _kubectl(infra, connector, ["top", "nodes", "--no-headers"]))
    ns_text = await _kubectl(infra, connector, ["get", "namespaces", "-o", "name"])
    namespaces = [ln.strip().split("/", 1)[-1] for ln in (ns_text or "").splitlines()
                  if ln.strip()][:MAX_NAMESPACES]
    if not namespaces:
        warnings.append("namespaces could not be read")
    idle: list[dict[str, Any]] = []
    oversized: list[dict[str, Any]] = []
    restarts: list[dict[str, Any]] = []
    unused_pvcs: list[dict[str, Any]] = []
    for ns in namespaces:
        pods = await _items(infra, connector, "pods", ns, warnings)
        pvcs = await _items(infra, connector, "persistentvolumeclaims", ns, warnings)
        used = set().union(*(_claims(p) for p in pods)) if pods else set()
        for pvc in pvcs:
            meta = pvc.get("metadata") or {}
            if (pvc.get("status") or {}).get("phase") == "Bound" and meta.get("name") not in used:
                unused_pvcs.append({
                    "namespace": ns, "pvc": meta.get("name", ""),
                    "size": ((pvc.get("status") or {}).get("capacity") or {}).get("storage")})
        for pod in pods:
            meta = pod.get("metadata") or {}
            name = str(meta.get("name", ""))
            stats = _restart_stats(pod, now)
            if stats:
                restarts.append(stats)
            if (pod.get("status") or {}).get("phase") != "Running" or (ns, name) not in usage:
                continue
            cpu_req, mem_req = _requests(pod)
            cpu_use, mem_use = usage[(ns, name)]
            row = {"namespace": ns, "pod": name, "cpu_request_m": round(cpu_req),
                   "cpu_use_m": round(cpu_use), "mem_request_mib": round(mem_req),
                   "mem_use_mib": round(mem_use)}
            if cpu_req > 0 and cpu_use < IDLE_RATIO * cpu_req and (
                    mem_req <= 0 or mem_use < IDLE_RATIO * mem_req):
                idle.append(row)
            elif (cpu_req >= OVERSIZED_MIN_CPU_M and cpu_use < OVERSIZED_RATIO * cpu_req) or (
                    mem_req >= OVERSIZED_MIN_MEM_MI and mem_use < OVERSIZED_RATIO * mem_req):
                oversized.append(row)
    pv_text = await _kubectl(infra, connector, ["get", "persistentvolumes", "-o", "json"])
    if pv_text is None:
        warnings.append("persistentvolumes could not be read")
    pvs = [{"pv": (p.get("metadata") or {}).get("name", ""),
            "phase": (p.get("status") or {}).get("phase"),
            "size": ((p.get("spec") or {}).get("capacity") or {}).get("storage")}
           for p in _pv_items(pv_text)
           if (p.get("status") or {}).get("phase") in ("Released", "Available")]
    restarts.sort(key=lambda r: r["restarts_per_hour"], reverse=True)
    burn = sorted((r for r in restarts if r["crash_looping"]),
                  key=lambda r: r["estimated_cpu_core_seconds"], reverse=True)
    idle.sort(key=lambda r: r["cpu_request_m"], reverse=True)
    oversized.sort(key=lambda r: r["cpu_request_m"], reverse=True)
    return {
        "collected_at": now.isoformat(),
        "metrics_available": top_text is not None,
        "nodes": nodes,
        "idle_pods": {"count": len(idle), "items": idle[:TOP_N]},
        "oversized_pods": {"count": len(oversized), "items": oversized[:TOP_N]},
        "unused_pvcs": {"count": len(unused_pvcs), "items": unused_pvcs[:TOP_N]},
        "orphaned_pvs": {"count": len(pvs), "items": pvs[:TOP_N]},
        "restarting_pods": {"count": len(restarts), "items": restarts[:TOP_N]},
        "crash_loop_burn": {"estimate": True, "count": len(burn), "items": burn[:TOP_N],
                            "formula": "restarts x last container uptime x CPU request"},
        "prometheus": await _prometheus(prometheus_url, transport),
        "not_measured": ["unused images (kubectl cannot list node image usage)"],
        "warnings": warnings[:10],
    }


def _pv_items(text: str | None) -> list[dict[str, Any]]:
    data = _json(text)
    items = data.get("items") if isinstance(data, dict) else None
    return [i for i in items or [] if isinstance(i, dict)]


__all__ = ["collect_waste", "cpu_millicores", "mem_mib", "parse_top_nodes", "parse_top_pods"]
