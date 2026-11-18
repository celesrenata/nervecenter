"""Workbench IDE manager (Req 5, 6.6, 7.4-7.6, 7.11, 7.12, 8.7, 9.6).

Pod spec builder, upstream resolution, idle selection, event emission and
the ``IdeManager`` (start, stop, list, rediscovery, idle reaper).
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from prometheus_client import Counter

from backend.workbench.kubeclient import KubeClient, KubeError
from backend.workbench.phase import workbench_gid, workbench_uid

log = structlog.get_logger(__name__)

NAMESPACE = "workbench"
CODE_SERVER_PORT = 8080
PROJECT_MOUNT = "/home/coder/project"
IPC_SOCKET_PATH = "/tmp/menagerie/ipc.sock"
IDLE_LIMIT = timedelta(minutes=60)
EVENT_TYPES = frozenset({"started", "stopped", "failed", "idle_stopped"})
KEY_SECRET_KEY = "OMNIROUTE_API_KEY"

DEFAULT_CPU_REQUEST = "250m"
DEFAULT_MEMORY_REQUEST = "1Gi"
DEFAULT_CPU_LIMIT = "2"
DEFAULT_MEMORY_LIMIT = "3Gi"

_DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_SLUG_LABEL = "workbench/project"
_KIND_LABEL = "workbench/kind"

ide_events_total = Counter(
    "nervcenter_workbench_ide_events_total",
    "Workbench IDE lifecycle events",
    ["event_type"],
)


class AmbiguousSlug(Exception):
    """The slug exists in both kinds (HTTP 409 ``ambiguous_slug``)."""


class InvalidImage(ValueError):
    """The IDE image is not pinned by digest."""


@dataclass(frozen=True)
class IdeProject:
    slug: str
    kind: str
    push_approved: bool = False


@dataclass(frozen=True)
class IdeConfig:
    image: str
    push_approved: bool = False
    uid: int = 1000
    gid: int = 1000
    cpu_request: str = DEFAULT_CPU_REQUEST
    memory_request: str = DEFAULT_MEMORY_REQUEST
    cpu_limit: str = DEFAULT_CPU_LIMIT
    memory_limit: str = DEFAULT_MEMORY_LIMIT

    @classmethod
    def from_env(
        cls,
        image: str,
        push_approved: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> IdeConfig:
        e = os.environ if env is None else env

        def pick(name: str, default: str) -> str:
            return e.get(name, "").strip() or default

        return cls(
            image=image,
            push_approved=push_approved,
            uid=workbench_uid(e),
            gid=workbench_gid(e),
            cpu_request=pick("WORKBENCH_IDE_CPU_REQUEST", DEFAULT_CPU_REQUEST),
            memory_request=pick("WORKBENCH_IDE_MEMORY_REQUEST", DEFAULT_MEMORY_REQUEST),
            cpu_limit=pick("WORKBENCH_IDE_CPU_LIMIT", DEFAULT_CPU_LIMIT),
            memory_limit=pick("WORKBENCH_IDE_MEMORY_LIMIT", DEFAULT_MEMORY_LIMIT),
        )


@dataclass(frozen=True)
class IdeInstance:
    slug: str
    kind: str
    pod_name: str
    pod_ip: str | None
    last_traffic_at: datetime


def pvc_name_for(kind: str) -> str:
    return f"wb-{kind}-projects"


def _is_zero(quantity: str) -> bool:
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)", quantity or "")
    return m is None or float(m.group(1)) == 0


def build_pod_spec(project: IdeProject, config: IdeConfig, key_secret_name: str) -> dict[str, Any]:
    """Build the IDE pod manifest. Pure; raises ``InvalidImage`` for unpinned images."""
    if not _DIGEST_RE.match(config.image or ""):
        raise InvalidImage("IDE image must be pinned by digest (@sha256:...)")
    for q in (config.cpu_request, config.memory_request, config.cpu_limit, config.memory_limit):
        if _is_zero(q):
            raise ValueError("cpu and memory requests and limits must be non-zero")

    slug, kind = project.slug, project.kind
    container = {
        "name": "code-server",
        "image": config.image,
        "args": [
            "--auth", "none",
            "--bind-addr", f"0.0.0.0:{CODE_SERVER_PORT}",
            PROJECT_MOUNT,
        ],
        "ports": [{"name": "http", "containerPort": CODE_SERVER_PORT}],
        "env": [
            {"name": "ROO_CODE_IPC_SOCKET_PATH", "value": IPC_SOCKET_PATH},
            {
                "name": "WORKBENCH_PUSH_APPROVED",
                "value": "true" if config.push_approved else "false",
            },
            {
                "name": KEY_SECRET_KEY,
                "valueFrom": {"secretKeyRef": {"name": key_secret_name, "key": KEY_SECRET_KEY}},
            },
        ],
        "resources": {
            "requests": {"cpu": config.cpu_request, "memory": config.memory_request},
            "limits": {"cpu": config.cpu_limit, "memory": config.memory_limit},
        },
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": config.uid,
            "runAsGroup": config.gid,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "readOnlyRootFilesystem": True,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "volumeMounts": [
            {"name": "project", "mountPath": PROJECT_MOUNT, "subPath": slug},
            {"name": "tmp", "mountPath": "/tmp"},
            {"name": "local", "mountPath": "/home/coder/.local"},
            {"name": "config", "mountPath": "/home/coder/.config"},
            {"name": "cache", "mountPath": "/home/coder/.cache"},
            {"name": "roo", "mountPath": "/home/coder/.roo"},
            {"name": "agents", "mountPath": "/home/coder/.agents"},
            {"name": "menagerie-settings", "mountPath": "/etc/menagerie", "readOnly": True},
        ],
    }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "namespace": NAMESPACE,
            "generateName": "ide-",
            "labels": {"app": "ide", _SLUG_LABEL: slug, _KIND_LABEL: kind},
        },
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": config.uid,
                "runAsGroup": config.gid,
                "fsGroup": config.gid,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [container],
            "volumes": [
                {
                    "name": "project",
                    "persistentVolumeClaim": {"claimName": pvc_name_for(kind)},
                },
                {"name": "tmp", "emptyDir": {}},
                {"name": "local", "emptyDir": {}},
                {"name": "config", "emptyDir": {}},
                {"name": "cache", "emptyDir": {}},
                {"name": "roo", "emptyDir": {}},
                {"name": "agents", "emptyDir": {}},
                {
                    "name": "menagerie-settings",
                    "configMap": {"name": "workbench-menagerie-settings", "optional": True},
                },
            ],
        },
    }


def resolve_upstream(slug: str, instances: Iterable[IdeInstance]) -> IdeInstance | None:
    """Return the instance for ``slug`` only, or None. Raises ``AmbiguousSlug``
    when instances of both kinds carry the slug."""
    matches = [i for i in instances if i.slug == slug]
    if len({i.kind for i in matches}) > 1:
        raise AmbiguousSlug(slug)
    return matches[0] if matches else None


def select_idle(instances: Iterable[IdeInstance], now: datetime) -> list[IdeInstance]:
    """Instances whose idle time is at least 60 minutes."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    out = []
    for i in instances:
        last = i.last_traffic_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if now - last >= IDLE_LIMIT:
            out.append(i)
    return out


def emit_event(event_type: str, project_id: str, ide_id: str) -> None:
    """One structured log entry and one counter increment. No file data."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown IDE event type: {event_type}")
    ide_events_total.labels(event_type).inc()
    log.info(
        f"ide.{event_type}",
        event_type=f"ide.{event_type}",
        project_id=project_id,
        ide_id=ide_id,
        at=datetime.now(UTC).isoformat(),
    )

MAX_RUNNING = 2
START_TIMEOUT_S = 120.0
STOP_TIMEOUT_S = 30.0
REDISCOVER_TIMEOUT_S = 30.0
REAPER_INTERVAL_S = 15.0
_ACTIVE_PHASES = frozenset({"Pending", "Running"})


class IdeLimitReached(Exception):
    """Two IDE instances are already running (HTTP 409/429)."""

    def __init__(self, limit: int = MAX_RUNNING) -> None:
        super().__init__(f"IDE limit of {limit} running instances reached")
        self.limit = limit


class IdeStartFailed(Exception):
    """The IDE did not become Ready; carries the last pod event message."""

    def __init__(self, slug: str, message: str) -> None:
        super().__init__(f"IDE for {slug} failed to start: {message}")
        self.slug = slug
        self.message = message


class IdeNotFound(Exception):
    """No IDE instance exists for the slug."""


KeyIssuer = Callable[[str], Awaitable[str]]
Clock = Callable[[], datetime]


async def default_key_issuer(slug: str) -> str:
    """Issue the OmniRoute client key via the existing key machinery
    (``backend.inference.key_resolver``). Raises if none is configured."""
    import base64
    import os

    from backend.inference import key_resolver as kr
    from backend.workbench.kubeclient import KubeClient

    key = None
    name = os.environ.get("WORKBENCH_OMNIROUTE_KEY_SECRET", "workbench-omniroute-key")
    try:
        kube = KubeClient()
        try:
            data = (await kube.get_secret(name)).get("data") or {}
        finally:
            await kube.aclose()
        raw = data.get("OMNIROUTE_API_KEY")
        key = base64.b64decode(raw).decode().strip() if raw else None
    except Exception:  # noqa: BLE001 - fall back to the configured client key
        key = None
    key = key or kr.regular_key()
    if not key:
        raise RuntimeError("no OmniRoute client key is configured")
    return key


def _pod_ready(pod: Mapping[str, Any]) -> bool:
    status = pod.get("status") or {}
    if status.get("phase") != "Running":
        return False
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in status.get("conditions") or []
    )


def _pod_active(pod: Mapping[str, Any]) -> bool:
    meta = pod.get("metadata") or {}
    if meta.get("deletionTimestamp"):
        return False
    return (pod.get("status") or {}).get("phase") in _ACTIVE_PHASES


class IdeManager:
    def __init__(
        self,
        kube: KubeClient,
        config: IdeConfig,
        *,
        key_issuer: KeyIssuer = default_key_issuer,
        clock: Clock | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        session_factory: Any = None,
        max_running: int = MAX_RUNNING,
        start_timeout: float = START_TIMEOUT_S,
        stop_timeout: float = STOP_TIMEOUT_S,
        rediscover_timeout: float = REDISCOVER_TIMEOUT_S,
        reaper_interval: float = REAPER_INTERVAL_S,
        poll_interval: float = 1.0,
    ) -> None:
        self._kube = kube
        self._config = config
        self._issue_key = key_issuer
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._session_factory = session_factory
        self._max_running = max_running
        self._start_timeout = start_timeout
        self._stop_timeout = stop_timeout
        self._rediscover_timeout = rediscover_timeout
        self._reaper_interval = reaper_interval
        self._poll = poll_interval
        self._registry: dict[tuple[str, str], IdeInstance] = {}
        self._lock = asyncio.Lock()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _instance(pod: Mapping[str, Any], last: datetime) -> IdeInstance:
        meta = pod.get("metadata") or {}
        labels = meta.get("labels") or {}
        return IdeInstance(
            slug=labels.get(_SLUG_LABEL, ""),
            kind=labels.get(_KIND_LABEL, ""),
            pod_name=meta.get("name", ""),
            pod_ip=(pod.get("status") or {}).get("podIP"),
            last_traffic_at=last,
        )

    async def _active_pods(self) -> list[dict[str, Any]]:
        pods = await self._kube.list_pods("app=ide")
        return [p for p in pods if _pod_active(p)]

    def _instances_from(self, pods: Iterable[dict[str, Any]]) -> list[IdeInstance]:
        now = self._clock()
        out = []
        for pod in pods:
            inst = self._instance(pod, now)
            known = self._registry.get((inst.slug, inst.kind))
            if known is not None and known.pod_name == inst.pod_name:
                inst = IdeInstance(
                    inst.slug, inst.kind, inst.pod_name, inst.pod_ip, known.last_traffic_at
                )
            out.append(inst)
        return out

    async def _last_event_message(self, pod_name: str) -> str:
        try:
            events = await self._kube.list_events(f"involvedObject.name={pod_name}")
        except KubeError:
            return "no pod event available"
        msgs = [e.get("message") for e in events if e.get("message")]
        return str(msgs[-1]) if msgs else "no pod event available"

    async def _delete_quiet(self, kind: str, name: str) -> None:
        fn = self._kube.delete_pod if kind == "pod" else self._kube.delete_secret
        try:
            await fn(name)
        except KubeError as exc:
            if exc.status != 404:
                log.warning("ide.cleanup_failed", kind=kind, name=name, status=exc.status)

    # -- traffic persistence (workbench_ide) -------------------------------
    async def _project_id(self, session: Any, slug: str, kind: str) -> str | None:
        from sqlalchemy import select

        from backend.persistence.models.workbench import WorkbenchProject

        res = await session.execute(
            select(WorkbenchProject.id).where(
                WorkbenchProject.slug == slug, WorkbenchProject.kind == kind
            )
        )
        return res.scalar_one_or_none()

    async def _persist(self, inst: IdeInstance, state: str | None, *, started: bool) -> None:
        if self._session_factory is None:
            return
        from backend.persistence.models.workbench import WorkbenchIde

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    pid = await self._project_id(session, inst.slug, inst.kind)
                    if pid is None:
                        return
                    row = await session.get(WorkbenchIde, pid)
                    if row is None:
                        row = WorkbenchIde(project_id=pid)
                        session.add(row)
                    row.pod_name = inst.pod_name if state else None
                    row.state = state
                    row.last_traffic_at = inst.last_traffic_at
                    if started:
                        row.started_at = self._clock()
        except Exception:  # noqa: BLE001 - DB is secondary to the cluster
            log.warning("ide.persist_failed", slug=inst.slug)

    async def _stored_traffic(self, slug: str, kind: str) -> datetime | None:
        if self._session_factory is None:
            return None
        from backend.persistence.models.workbench import WorkbenchIde

        try:
            async with self._session_factory() as session:
                pid = await self._project_id(session, slug, kind)
                if pid is None:
                    return None
                row = await session.get(WorkbenchIde, pid)
                return row.last_traffic_at if row else None
        except Exception:  # noqa: BLE001
            return None

    async def record_traffic(self, slug: str) -> None:
        """Update last traffic time for ``slug`` (the proxy throttles to 30 s)."""
        for key, inst in list(self._registry.items()):
            if inst.slug == slug:
                new = IdeInstance(
                    inst.slug, inst.kind, inst.pod_name, inst.pod_ip, self._clock()
                )
                self._registry[key] = new
                await self._persist(new, "running", started=False)

    # -- public API --------------------------------------------------------
    async def list(self) -> list[IdeInstance]:
        pods = await self._active_pods()
        instances = self._instances_from(pods)
        self._registry = {(i.slug, i.kind): i for i in instances}
        return instances

    async def get(self, slug: str) -> IdeInstance | None:
        """Instance for ``slug``; raises ``AmbiguousSlug`` if both kinds exist."""
        return resolve_upstream(slug, await self.list())

    async def start(self, project: IdeProject) -> IdeInstance:
        async with self._lock:
            instances = self._instances_from(await self._active_pods())
            existing = resolve_upstream(project.slug, instances)
            if existing is not None:
                if existing.kind != project.kind:
                    raise AmbiguousSlug(project.slug)
                self._registry[(existing.slug, existing.kind)] = existing
                return existing
            if len(instances) >= self._max_running:
                raise IdeLimitReached(self._max_running)
            return await self._start_new(project)

    async def _start_new(self, project: IdeProject) -> IdeInstance:
        slug = project.slug
        secret_name = f"ide-key-{slug}-{secrets.token_hex(3)}"
        try:
            key = await self._issue_key(slug)
            config = self._config
            if project.push_approved and not config.push_approved:
                config = replace(config, push_approved=True)
            spec = build_pod_spec(project, config, secret_name)
            pod = await self._kube.create_pod(spec)
        except Exception as exc:
            emit_event("failed", slug, "")
            if isinstance(exc, KubeError | InvalidImage | ValueError):
                raise IdeStartFailed(slug, str(exc)) from exc
            raise
        pod_name = pod["metadata"]["name"]
        pod_uid = pod["metadata"].get("uid", "")
        try:
            await self._kube.create_secret(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {
                        "name": secret_name,
                        "namespace": NAMESPACE,
                        "labels": {"app": "ide", _SLUG_LABEL: slug},
                        "ownerReferences": [
                            {
                                "apiVersion": "v1",
                                "kind": "Pod",
                                "name": pod_name,
                                "uid": pod_uid,
                            }
                        ],
                    },
                    "type": "Opaque",
                    "stringData": {KEY_SECRET_KEY: key},
                }
            )
        except KubeError as exc:
            await self._delete_quiet("pod", pod_name)
            await self._delete_quiet("secret", secret_name)
            emit_event("failed", slug, pod_name)
            raise IdeStartFailed(slug, str(exc)) from exc
        deadline = self._clock() + timedelta(seconds=self._start_timeout)
        while True:
            try:
                current = await self._kube.get_pod(pod_name)
            except KubeError:
                current = {}
            if current and _pod_ready(current):
                inst = self._instance(current, self._clock())
                self._registry[(inst.slug, inst.kind)] = inst
                await self._persist(inst, "running", started=True)
                emit_event("started", slug, pod_name)
                return inst
            failed = (current.get("status") or {}).get("phase") == "Failed"
            if failed or self._clock() >= deadline:
                break
            await self._sleep(self._poll)
        message = await self._last_event_message(pod_name)
        await self._delete_quiet("pod", pod_name)
        await self._delete_quiet("secret", secret_name)
        self._registry.pop((slug, project.kind), None)
        emit_event("failed", slug, pod_name)
        raise IdeStartFailed(slug, message)

    async def _delete_instance(self, inst: IdeInstance) -> None:
        await asyncio.wait_for(self._delete_quiet("pod", inst.pod_name), self._stop_timeout)
        self._registry.pop((inst.slug, inst.kind), None)
        await self._persist(
            IdeInstance(inst.slug, inst.kind, inst.pod_name, None, inst.last_traffic_at),
            None,
            started=False,
        )

    async def stop(self, slug: str) -> None:
        """Delete the pod (project files are untouched; the Secret is
        garbage-collected through its ownerReference)."""
        inst = await self.get(slug)
        if inst is None:
            raise IdeNotFound(slug)
        await self._delete_instance(inst)
        emit_event("stopped", slug, inst.pod_name)

    async def rediscover(self) -> list[IdeInstance]:
        """Rebuild the registry from the cluster (source of truth) within 30 s."""

        async def _run() -> list[IdeInstance]:
            instances = self._instances_from(await self._active_pods())
            rebuilt: list[IdeInstance] = []
            for inst in instances:
                stored = await self._stored_traffic(inst.slug, inst.kind)
                if stored is not None:
                    inst = IdeInstance(
                        inst.slug, inst.kind, inst.pod_name, inst.pod_ip, stored
                    )
                rebuilt.append(inst)
            self._registry = {(i.slug, i.kind): i for i in rebuilt}
            return rebuilt

        return await asyncio.wait_for(_run(), self._rediscover_timeout)

    async def reap_once(self) -> list[IdeInstance]:
        """Delete pods idle for 60 minutes or more; emit ``idle_stopped``."""
        instances = await self.list()
        reaped = []
        for inst in select_idle(instances, self._clock()):
            try:
                await self._delete_instance(inst)
            except Exception:  # noqa: BLE001 - retry on next tick
                log.warning("ide.reap_failed", slug=inst.slug)
                continue
            emit_event("idle_stopped", inst.slug, inst.pod_name)
            reaped.append(inst)
        return reaped

    async def run_reaper(self) -> None:
        """Background loop; ticks every 15 s until cancelled."""
        while True:
            try:
                await self.reap_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("ide.reaper_tick_failed")
            await self._sleep(self._reaper_interval)
