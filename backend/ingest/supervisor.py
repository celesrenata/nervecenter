"""Mail ingest supervisor (Personal-Ops Recovery §4 / §16 — workstream C).

:func:`install_ingest_supervisor` is the app-lifespan hook: on startup it
builds one :class:`MailIngestWorker` per *enabled* mail connection (spec §4:
"one worker per enabled mail connection") and keeps them running; on
shutdown it stops every worker. The supervisor itself carries no resumable
state — workers re-read their connection + cursor from the database on every
poll, so a restarted process rebuilds the exact same set of workers from
durable state (§4: "workers restart from database state, never memory").

A lightweight reaper restarts any worker task that exits unexpectedly
(crash), so one flaky connection cannot silently go dark (spec §16:
"persist liveness, reap/restart crashes"). Liveness is exposed through
:meth:`MailIngestSupervisor.liveness` for a future ``/api/health`` surface.

A periodic reconcile loop (interval from ``NERVE_INGEST_RECONCILE_S``,
default 30s) keeps the worker set in sync with durable state while the
process runs: newly added *enabled* mail connections start a worker
without a process restart, and workers whose connection was disabled or
removed are stopped. Startup and reconcile share one worker-builder code
path.

Startup tolerance (matching :mod:`backend.api.wiring`): the plane only starts
when *all* of (a) ``NERVE_INGEST_ENABLED`` is on, (b) a durable
``repository_registry`` is wired, and (c) an object store is configured. Any
of those absent → the supervisor is *not* installed (the honest
"not configured" degradation), and no exception escapes startup.
"""

from __future__ import annotations

import asyncio
import structlog
from typing import Any

from backend.connectors.mail.mail_transport import ImapPollClient, GmailClient
from backend.ingest.mail_worker import (
    CURSOR_KINDS,
    MailIngestWorker,
    default_ingest_seam,
    ingest_enabled,
)
from backend.persistence.repositories.registry import RepositoryRegistry

log = structlog.get_logger(__name__)

#: ``connections.kind`` → (cursor kind, transport factory).
_KIND_TO_CURSOR = {
    "gmail": ("gmail_history", "gmail"),
    "imap": ("imap_seq", "imap"),
    "outlook": ("imap_seq", "imap"),
}


def _reconcile_interval_s(env: dict[str, str] | None = None) -> float:
    """Reconcile-loop interval (spec §16: dynamic start/stop of workers)."""
    import os

    source = dict(env) if env is not None else dict(os.environ)
    try:
        return max(1.0, float(source.get("NERVE_INGEST_RECONCILE_S", "30")))
    except (TypeError, ValueError):
        return 30.0


def _build_transport(kind: str, config: dict[str, Any]) -> Any:
    """Construct the transport for a connection kind (gmail vs generic imap)."""
    cursor_kind, _ = _KIND_TO_CURSOR[kind]
    if cursor_kind == "gmail_history":
        base_url = str(config.get("gmail_api_base") or "https://gmail.googleapis.com")
        return GmailClient(base_url=base_url)
    return ImapPollClient()


class MailIngestSupervisor:
    """Owns the one-per-enabled-mail-connection worker set.

    The supervisor holds only the :class:`MailIngestWorker` instances and the
    reaper task. It never holds secrets, cursors, or mail content — those
    live in the database and the object store.
    """

    def __init__(
        self,
        *,
        registry: RepositoryRegistry,
        object_store: Any,
        env: dict[str, str] | None = None,
        seam: Any | None = None,
        poll_interval_s: float = 30.0,
        backoff_base_s: float = 2.0,
        backoff_max_s: float = 120.0,
        reconcile_interval_s: float | None = None,
    ) -> None:
        self._registry = registry
        self._object_store = object_store
        self._env = env
        self._seam = seam if seam is not None else default_ingest_seam()
        self._poll_interval_s = float(poll_interval_s)
        self._backoff_base_s = float(backoff_base_s)
        self._backoff_max_s = float(backoff_max_s)
        self._reconcile_interval_s = (
            float(reconcile_interval_s)
            if reconcile_interval_s is not None
            else _reconcile_interval_s(env)
        )
        self._workers: dict[str, MailIngestWorker] = {}
        self._reaper: asyncio.Task[None] | None = None
        self._reconcile: asyncio.Task[None] | None = None
        self._known: set[str] = set()
        self._started = False

    @property
    def workers(self) -> dict[str, MailIngestWorker]:
        return dict(self._workers)

    def liveness(self) -> list[dict[str, Any]]:
        """Secret-free liveness snapshot per worker (spec §16)."""
        return [w.status() for w in self._workers.values()]

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> int:
        """Spawn one worker per enabled mail connection; return the count.

        Idempotent: calling again while already started is a no-op. The
        connection list is read from the database on each start, so a fresh
        process after a restart rebuilds the worker set from durable state.
        Both background loops (crash reaper + reconcile) run for the whole
        lifetime so new/disabled/removed connections are picked up without
        a process restart.
        """
        if self._started:
            return len(self._workers)
        self._started = True

        await self._build_workers()

        loop = asyncio.get_running_loop()
        self._reaper = loop.create_task(self._reap(), name="mail-ingest-reaper")
        self._reconcile = loop.create_task(
            self._reconcile_loop(), name="mail-ingest-reconcile"
        )
        log.info("mail_ingest.supervisor_started", workers=len(self._workers))
        return len(self._workers)

    async def stop(self) -> None:
        """Stop every worker and both background loops; safe to call when
        not started."""
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        if self._reconcile is not None:
            self._reconcile.cancel()
            self._reconcile = None
        for worker in self._workers.values():
            await worker.stop()
        self._workers.clear()
        self._known.clear()
        self._started = False
        log.info("mail_ingest.supervisor_stopped")

    # -- dynamic reconcile (spec §16: no restart for new connections) -------

    async def reconcile_once(self) -> None:
        """One reconcile pass over durable state (safe to call repeatedly).

        Starts a worker for every *enabled* mail connection that has none
        yet, and stops workers whose connection was disabled or removed.
        A transient database failure is logged, never raised — reconcile
        must not take the plane down.
        """
        try:
            connections = await self._registry.connections.list_connections()
        except Exception as exc:  # noqa: BLE001 - transient DB failure
            log.warning("mail_ingest.reconcile_skipped", error=str(exc))
            return

        enabled: dict[str, Any] = {}
        for conn in connections:
            kind = str(getattr(conn, "kind", "") or "").lower()
            if kind not in _KIND_TO_CURSOR:
                continue
            if bool(getattr(conn, "enabled", False)):
                enabled[conn.id] = conn

        # start workers that are missing
        for conn_id, conn in enabled.items():
            if conn_id in self._workers:
                continue
            await self._spawn_for(conn)
            log.info("mail_ingest.worker_started", connection_id=conn_id)

        # stop workers whose connection is disabled or removed. The reason is
        # decided *before* bookkeeping changes: a row that still exists but is
        # disabled → "disabled"; a row that is gone entirely → "removed".
        seen_ids = {
            conn.id for conn in connections
            if str(getattr(conn, "kind", "") or "").lower() in _KIND_TO_CURSOR
        }
        for conn_id, worker in list(self._workers.items()):
            if conn_id in enabled:
                continue
            reason = "disabled" if conn_id in seen_ids else "removed"
            del self._workers[conn_id]
            self._known.discard(conn_id)
            log.info("mail_ingest.worker_stopped", connection_id=conn_id, reason=reason)
            await worker.stop()

        self._known.update(enabled)

    # -- crash reaping -----------------------------------------------------

    async def _reap(self) -> None:
        """Restart any worker task that exited unexpectedly (crash)."""
        try:
            while self._started:
                for conn_id, worker in list(self._workers.items()):
                    if worker.is_running:
                        continue
                    if conn_id not in self._workers:
                        continue
                    log.warning("mail_ingest.worker_reaped", connection_id=conn_id)
                    worker.start()
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            raise

    async def _reconcile_loop(self) -> None:
        """Periodically reconcile the worker set with durable state."""
        try:
            while self._started:
                await asyncio.sleep(self._reconcile_interval_s)
                if not self._started:
                    break
                try:
                    await self.reconcile_once()
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    log.warning("mail_ingest.reconcile_failed", error=str(exc))
        except asyncio.CancelledError:
            raise

    async def _spawn_for(self, conn: Any) -> None:
        """Build and start the worker for one enabled mail connection.

        Shared by startup and the reconcile loop (one code path, §16).
        """
        kind = str(getattr(conn, "kind", "") or "").lower()
        cursor_kind, _ = _KIND_TO_CURSOR[kind]
        transport = _build_transport(kind, dict(getattr(conn, "config", None) or {}))
        worker = MailIngestWorker(
            connection_id=conn.id,
            cursor_kind=cursor_kind,
            connections=self._registry.connections,
            imports=self._registry.imports,
            object_store=self._object_store,
            transport=transport,
            seam=self._seam,
            poll_interval_s=self._poll_interval_s,
            backoff_base_s=self._backoff_base_s,
            backoff_max_s=self._backoff_max_s,
        )
        self._workers[conn.id] = worker
        worker.start()

    async def _build_workers(self) -> None:
        """Startup: build the initial worker set from durable state.

        Startup tolerance: a failed read is logged, not raised (§16).
        """
        try:
            connections = await self._registry.connections.list_connections(enabled_only=True)
        except Exception as exc:  # noqa: BLE001 - startup must never raise
            log.warning("mail_ingest.build_failed", error=str(exc))
            return
        for conn in connections:
            if str(getattr(conn, "kind", "") or "").lower() not in _KIND_TO_CURSOR:
                continue
            await self._spawn_for(conn)

    def worker_for(self, connection_id: str) -> MailIngestWorker | None:
        return self._workers.get(connection_id)

    async def status(self) -> list[dict[str, Any]]:
        """Secret-free per-connection status for ``/api/health`` (spec §16).

        One entry per mail connection (enabled or disabled): connection
        id, worker state (``running`` / ``stopped``), durable sync
        bookkeeping, and the *redacted* last error (code + truncated
        message). Credentials are never read or returned.
        """

        def _ts(value: Any) -> str | None:
            try:
                return value.isoformat() if value is not None else None
            except Exception:  # noqa: BLE001 - report, never fail health
                return None

        try:
            connections = await self._registry.connections.list_connections()
        except Exception:  # noqa: BLE001 - transient DB failure
            return []

        out: list[dict[str, Any]] = []
        for conn in connections:
            if str(getattr(conn, "kind", "") or "").lower() not in _KIND_TO_CURSOR:
                continue
            worker = self._workers.get(conn.id)
            message = getattr(conn, "last_error_message", None)
            out.append(
                {
                    "connection_id": conn.id,
                    "state": "running" if worker is not None and worker.is_running else "stopped",
                    "last_success_at": _ts(getattr(conn, "last_success_at", None)),
                    "last_attempt_at": _ts(getattr(conn, "last_attempt_at", None)),
                    "last_error_code": getattr(conn, "last_error_code", None),
                    "last_error_message": (str(message)[:256] if message is not None else None),
                }
            )
        return out


# -- app-lifespan hook ---------------------------------------------------------

#: State key where the wiring stores the live supervisor.
SUPERVISOR_STATE_KEY = "ingest_supervisor"


def _object_store_from_env() -> Any | None:
    """Resolve the object store from the environment; ``None`` when not
    configured (no silent in-memory fallback, §14)."""
    from backend.storage.config import create_object_store_from_env

    try:
        return create_object_store_from_env()
    except Exception:  # noqa: BLE001 - absence means "not configured"
        return None


async def install_ingest_supervisor(target: Any) -> MailIngestSupervisor | None:
    """Lifespan hook (spec §16: "start supervisor in app lifespan when
    ``NERVE_INGEST_ENABLED=1``").

    Returns the live supervisor (for tests / a future health endpoint), or
    ``None`` when the plane is not enabled, the durable registry is absent,
    or the object store is unconfigured. Configuration-level absences are
    silent degradation; a start failure after best-effort cleanup re-raises
    (the wiring catches it — startup still never crashes the app).
    ``ingest_enabled()`` reads ``NERVE_INGEST_ENABLED`` from the process
    environment (the deploy contract, §16).
    """
    if not ingest_enabled():
        return None
    registry = getattr(target.state, "repository_registry", None)
    if not isinstance(registry, RepositoryRegistry):
        log.info("mail_ingest.supervisor_skipped", reason="no_durable_registry")
        return None
    object_store = _object_store_from_env()
    if object_store is None:
        log.info("mail_ingest.supervisor_skipped", reason="object_store_not_configured")
        return None
    supervisor = MailIngestSupervisor(registry=registry, object_store=object_store)
    try:
        await supervisor.start()
    except Exception:
        await supervisor.stop()
        raise
    setattr(target.state, SUPERVISOR_STATE_KEY, supervisor)
    return supervisor


async def stop_ingest_supervisor(target: Any) -> None:
    """Lifespan teardown hook: stop every worker (best-effort, never raises)."""
    supervisor = getattr(target.state, SUPERVISOR_STATE_KEY, None)
    if supervisor is None:
        return
    try:
        await supervisor.stop()
    finally:
        setattr(target.state, SUPERVISOR_STATE_KEY, None)


