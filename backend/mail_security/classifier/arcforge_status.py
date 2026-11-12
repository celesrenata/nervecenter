"""ArcForge service status for the operations dashboard (Task 7.7, R-021.5).

Reads ``/ready``, ``/v1/models`` and ``/metrics`` through the hardened
:class:`ArcForgeClient` (literal private IP only, no DNS, bounded responses,
``arcforge_`` series only) and reduces them to an allow-listed summary:

* readiness and the model identity *as reported by the service*;
* service activity: queue depth, active inferences, request/item counters,
  inference count and mean seconds, and whether the device watchdog reports
  a wedged device.

This is ArcForge *service* activity, not a GPU-percent measurement; the
service exposes no device-utilization series. Unconfigured (no
``NERVE_MAIL_SECURITY_ARCFORGE_URL`` in the caller-supplied environment) or invalid targets report
``NOT_CONFIGURED`` and never open a socket. Results are cached briefly so a
dashboard refresh never hammers the shared GPU host.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from backend.mail_security.classifier.arcforge_client import (
    ArcForgeClient,
    ClientConfig,
    TargetError,
    parse_target,
)

ENV_ARCFORGE_URL = "NERVE_MAIL_SECURITY_ARCFORGE_URL"
CACHE_SECONDS = 30.0

#: Allow-listed series -> dashboard key. Labels are summed away.
_SERIES = {
    "arcforge_embedding_queue_depth": "queue_depth",
    "arcforge_embedding_active_inferences": "active_inferences",
    "arcforge_embedding_requests_total": "requests_total",
    "arcforge_embedding_items_total": "items_total",
    "arcforge_embedding_inference_seconds_count": "inference_count",
    "arcforge_embedding_inference_seconds_sum": "inference_seconds_sum",
    "arcforge_device_wedged": "device_wedged",
}
_NAME_RE = re.compile(r"^(arcforge_[a-z0-9_]+)")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:/-]{1,80}$")

_cache_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None


def summarize_samples(samples: Mapping[str, float]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for key, value in samples.items():
        m = _NAME_RE.match(key)
        if m is None or m.group(1) not in _SERIES:
            continue
        name = _SERIES[m.group(1)]
        totals[name] = totals.get(name, 0.0) + float(value)
    count = totals.get("inference_count")
    if count:
        totals["mean_inference_seconds"] = totals.get("inference_seconds_sum", 0.0) / count
    totals.pop("inference_seconds_sum", None)
    if "device_wedged" in totals:
        totals["device_wedged"] = 1.0 if totals["device_wedged"] > 0 else 0.0
    return dict(sorted(totals.items()))


def _safe(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_LABEL.fullmatch(value) else None


MAX_TARGETS = 4


def fetch_status(
    env: Mapping[str, str],
    *,
    client_factory: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """Status for up to :data:`MAX_TARGETS` comma-separated targets.

    Hosts are reported by position label (``arcforge-1``...) only. Any invalid
    entry makes the whole setting ``NOT_CONFIGURED`` (fail closed: no socket).
    """
    raw = str(env.get(ENV_ARCFORGE_URL, "") or "").strip()
    entries = [e.strip() for e in raw.split(",") if e.strip()]
    if not entries:
        return {"status": "NOT_CONFIGURED"}
    if len(entries) > MAX_TARGETS:
        return {"status": "NOT_CONFIGURED", "code": "TOO_MANY_TARGETS"}
    targets = []
    for n, entry in enumerate(entries, start=1):
        try:
            targets.append(parse_target(entry, label=f"arcforge-{n}"))
        except TargetError as exc:
            return {"status": "NOT_CONFIGURED", "code": exc.code}
    factory = client_factory or (
        lambda t: ArcForgeClient(
            t,
            config=ClientConfig(connect_timeout_s=1.0, read_timeout_s=2.0, total_deadline_s=3.0),
        )
    )
    hosts = [{"label": t.label, **_host_status(factory(t))} for t in targets]
    ready = sum(1 for h in hosts if h["status"] == "READY")
    overall = "READY" if ready == len(hosts) else ("DEGRADED" if ready else "UNREACHABLE")
    return {"status": overall, "hosts": hosts}


def _host_status(client: Any) -> dict[str, Any]:
    ready = client.ready()
    if not ready.ok:
        return {"status": "UNREACHABLE", "code": ready.code}
    models = client.models()
    metrics = client.metrics()
    identity = getattr(models, "identity", None) if models.ok else None
    return {
        "status": "BREAKER_OPEN" if getattr(ready, "has_breaker_reason", False) else "READY",
        "model": (
            {
                "model_id": _safe(identity.model_id),
                "version": _safe(identity.version),
                "device": _safe(identity.device),
            }
            if identity is not None
            else None
        ),
        "activity": summarize_samples(metrics.samples) if metrics.ok else None,
        "metrics_code": metrics.code,
    }


def cached_status(
    env: Mapping[str, str],
    *,
    clock: Callable[[], float] = time.monotonic,
    client_factory: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    global _cache
    now = clock()
    with _cache_lock:
        if _cache is not None and now - _cache[0] < CACHE_SECONDS:
            return _cache[1]
    result = fetch_status(env, client_factory=client_factory)
    with _cache_lock:
        _cache = (now, result)
    return result


def reset_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


__all__ = [
    "CACHE_SECONDS",
    "ENV_ARCFORGE_URL",
    "cached_status",
    "fetch_status",
    "reset_cache",
    "summarize_samples",
]
