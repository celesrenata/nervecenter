"""Durable mail ingest worker (Personal-Ops Recovery §4 — workstream C).

One :class:`MailIngestWorker` polls one mail transport (spec §16: "one task
per enabled connection") and funnels each fetched message through the
*existing* communication ingestion seam — the same
``CommunicationsService.ingest_communication`` + ``get_pipeline().ingest``
path that ``POST /api/communications/ingest`` uses (§4: "and the existing
communication ingestion path").

Per poll, in order:

1. Re-read the connection and the durable cursor from the database — never
   from memory (§4: "workers restart from database state").
2. Resolve the secret from the durable ``secrets`` store and pass it to the
   transport as the *separate* ``credentials`` keyword (§4/§6: never
   persisted, never logged).
3. Store each message body in the object store; the communication record
   keeps only the resulting ``content_ref`` (§7: raw content stays out of
   the knowledge graph).
4. Dedup by (account, message-id/uid) — the durable seen-list embedded in
   the cursor row (``cursor_value["seen"]``) is the secondary guard; the
   transport cursor position is the primary one (pure helpers in
   :mod:`backend.ingest.seen`).
5. Ingest through the seam, then advance the durable cursor — with the
   updated seen list folded back into it — so a restart resumes from the
   database position.
6. Map :class:`~backend.connectors.mail.mail_transport.TransportAuthError`
   → ``auth_status="auth_expired"``,
   :class:`~backend.connectors.mail.mail_transport.TransportProviderError`
   → ``operational_status="error"`` + its carried ``last_error_code``
   (``provider_api_disabled`` / ``insufficient_scope``) *without* touching
   auth, and
   :class:`~backend.connectors.mail.mail_transport.TransportConnectError`
   → ``operational_status="error"`` + ``last_error_code``, then reconnect
   with bounded exponential backoff. Stored/logged failure text is always
   passed through :func:`redact_transport_error` first (stable code +
   redacted message — URL-embedded ``user:pass@`` and token-shaped
   substrings never reach the database or logs).

A first-ever poll (no stored cursor) is the *initial history import*: the
worker records it in an ``import_jobs`` row whose id is kept in the
connection's non-secret ``config`` so a restart resumes the same job (§4:
"explicit initial-history import recorded in ``import_jobs``").
"""

from __future__ import annotations

import asyncio
import hashlib
import structlog
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    # Imported under TYPE_CHECKING only: ``backend.api.models`` is reachable
    # through the app package's ``__init__`` → ``app`` → ``wiring`` → this
    # module, so a module-level import would be a circular import. The
    # :class:`MailConnector` is a plain value object; it is imported lazily at
    # the single construction site (:meth:`MailIngestWorker._connector_for`).
    from backend.api.models import MailConnector

from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
)
from backend.connectors.mail.mail_transport import (
    MailTransport,
    TransportAuthError,
    TransportConnectError,
    TransportProviderError,
)
from backend.connectors.mail.oauth_refresh import MailOAuthRefresher
from backend.ingest.seen import (
    cursor_with_seen,
    is_seen,
    mark_seen,
    seen_keys,
)

log = structlog.get_logger(__name__)

#: Environment toggle (spec §16): ``NERVE_INGEST_ENABLED`` — "0" keeps the
#: plane off, anything else enables it.
ENV_INGEST_ENABLED = "NERVE_INGEST_ENABLED"

#: ``connections.kind`` values that are mail connections. ``graph_mail`` is
#: the canonical Microsoft mail kind (the control plane's ``CONNECTION_KINDS``
#: value); ``outlook`` is kept only as a backward-compat alias for any
#: already-stored row (no new row can carry it — the control plane rejects
#: every kind outside ``CONNECTION_KINDS``).
CONNECTION_KINDS = ("gmail", "imap", "graph_mail", "outlook")

#: ``ingest_cursors.kind`` per source type.
CURSOR_KINDS = {"gmail": "gmail_history", "imap": "imap_seq"}

#: OAuth provider per connection kind (mirrors the control plane's
#: ``OAUTH_PROVIDER_BY_KIND``). ``graph_mail`` is canonical for Microsoft;
#: ``outlook`` stays as a backward-compat alias (same provider).
MAIL_OAUTH_PROVIDERS = {
    "gmail": "google",
    "graph_mail": "microsoft",
    "outlook": "microsoft",
}

#: Client-credential env-var names per provider (deploy contract, mirroring
#: :mod:`backend.connectors.wiring` and :mod:`backend.connectors.registry`).
#: The app keys are *not* per-connection config — ``client_secret`` is a
#: denied config key on the control plane. The worker reads them from the
#: environment first, falling back to ``config`` for tests / existing rows.
_CLIENT_ID_ENV = {
    "google": "NERVE_GOOGLE_CLIENT_ID",
    "microsoft": "NERVE_MICROSOFT_CLIENT_ID",
}
_CLIENT_SECRET_ENV = {
    "google": "NERVE_GOOGLE_CLIENT_SECRET",
    "microsoft": "NERVE_MICROSOFT_CLIENT_SECRET",
}

#: Backoff bounds (seconds): delay = min(base * 2**failures, max).
DEFAULT_BACKOFF_BASE_S = 2.0
DEFAULT_BACKOFF_MAX_S = 120.0

#: Default poll interval (seconds) for an idle mailbox.
DEFAULT_POLL_INTERVAL_S = 30.0


def ingest_enabled(env: dict[str, str] | None = None) -> bool:
    """Return ``True`` when the ingest plane is enabled via
    ``NERVE_INGEST_ENABLED`` (anything but ``"0"`` / empty enables)."""
    import os

    source = dict(env) if env is not None else dict(os.environ)
    return source.get(ENV_INGEST_ENABLED, "").strip() not in ("", "0")


@runtime_checkable
class IngestSeam(Protocol):
    """The existing communication ingestion path (``POST /api/
    communications/ingest``), re-exposed for the worker.

    Production binds the process-wide singletons; tests may inject a
    double. Keeping the seam duck-typed means the worker never imports the
    app package (no import cycle) and the pipeline step degrades exactly
    like the endpoint: a missing content registration means empty text,
    never an error.
    """

    def ingest_communication(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Upsert one normalized communication; returns the seam result
        (``{"communication", "conversation", "created"}``)."""
        ...

    def run_pipeline(self, communication: Communication, *, now: Any) -> Any:
        """Run the §61 detection → follow-up → attention pass."""
        ...

    async def flush_durable(self) -> Exception | None:
        """Optional member: await the pipeline's pending durable mirror
        writes and return the first failure (``None`` = everything
        committed). The worker gates its cursor on this — a truthy
        exception means ``commit_failed`` (P0-D). Doubles of the seam may
        omit it; the worker getattr-guards (treats no-flush as
        committed)."""
        ...


#: Process-wide durable communications store bound at startup
#: (:func:`set_communications_store`). It is the durable
#: :class:`~backend.persistence.repositories.communications_repo.
#: CommunicationsRepository` that BOTH the mail worker's production seam and
#: the ``POST /api/communications/ingest`` endpoint share, so every ingest
#: plane persists people + communications to Postgres (P1-107 step 2). ``None``
#: (the default / no durable engine) keeps the seam in-memory-only — the
#: historical behavior — so unit tests and no-DB deploys are unaffected.
_communications_store: Any = None


def set_communications_store(store: Any) -> None:
    """App seam: bind (or unbind) the durable communications sink (extend-only,
    P1-107 step 2).

    Mirrors :func:`backend.communications.detection.wiring.set_commitment_store`
    — the app startup wiring calls this once with the
    ``RepositoryRegistry.communications`` repository whenever a durable engine
    exists, so the live app persists ingested people + communications. Both
    the worker's :class:`DefaultIngestSeam` and the endpoint read the bound
    store through :func:`get_communications_store`, so they share one durable
    plane. Passing ``None`` restores the pure in-memory default (the no-DB
    degradation)."""
    global _communications_store
    _communications_store = store


def get_communications_store() -> Any:
    """Return the durable communications store bound by
    :func:`set_communications_store` (``None`` when no durable engine)."""
    return _communications_store


async def persist_communication_durably(
    store: Any,
    *,
    person_id: str,
    display_name: str | None,
    identity_kind: str,
    identity_handle: str,
    channel: str,
    direction: str,
    content_ref: str,
    subject: str | None,
    received_at: Any,
    communication_id: str,
) -> None:
    """Durably persist one person (+identity) and one communication (P1-107).

    PERSON FIRST (the FK on ``communications.person_id`` is ``NOT NULL`` +
    ``people.id``, so the person must exist before the communication insert):
    get-or-create the person with the *pinned* deterministic ``person_id`` so
    a replay reuses the same row, upsert the channel identity, then
    get-or-create the communication with the *pinned* deterministic id so a
    replay reuses the same communication row (idempotent, decision (c)). The
    per-(person, channel) conversation is upserted alongside. Raw content is
    never persisted — only the ``content_ref`` pointer (§7). Raises on a
    durable failure so the caller can gate the cursor on it."""
    if await store.get_person(person_id) is None:
        await store.create_person(
            id=person_id,
            display_name=display_name or identity_handle,
        )
    await store.upsert_person_identity(
        person_id=person_id,
        channel=identity_kind,
        handle=identity_handle,
        resolved=True,
    )
    # Get-or-create the communication by its pinned id (idempotent replay).
    existing = await store.get_communications_by_ids([communication_id])
    if not existing:
        await store.create_communication(
            id=communication_id,
            person_id=person_id,
            channel=channel,
            direction=direction,
            content_ref=content_ref,
            subject=subject,
            received_at=received_at,
        )
    await store.upsert_conversation(
        person_id=person_id,
        channel=channel,
        last_message_at=received_at,
    )


class DefaultIngestSeam:
    """Production :class:`IngestSeam` over the process-wide singletons.

    When a durable communications store is bound
    (:func:`set_communications_store`), each ingested message is also
    persisted durably (person + identity + communication + conversation) —
    the P1-107 durable half of the plane. The durable write is captured and
    surfaced through :meth:`flush_durable` so the worker's existing P0-D
    commit-gate blocks the cursor advance on a durable failure.
    """

    def __init__(self, *, communications_store: Any = None) -> None:
        self._service: Any = None
        self._pipeline: Any = None
        self._explicit_store = communications_store
        #: First durable communication-write failure in the current poll, held
        #: until :meth:`flush_durable` surfaces it (then cleared).
        self._durable_failure: Exception | None = None

    @property
    def communications_service(self) -> Any:
        if self._service is None:
            from backend.communications import get_communications_service

            self._service = get_communications_service()
        return self._service

    @property
    def communications_store(self) -> Any:
        """The durable store (explicit injection wins; else the process-wide
        one bound at startup). ``None`` keeps the seam in-memory-only."""
        if self._explicit_store is not None:
            return self._explicit_store
        return get_communications_store()

    @property
    def pipeline(self) -> Any:
        if self._pipeline is None:
            from backend.communications.detection.wiring import get_pipeline

            self._pipeline = get_pipeline()
        return self._pipeline

    def ingest_communication(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.communications_service.ingest_communication(payload)

    def seed_person(
        self,
        *,
        person_id: str,
        kind: str,
        value: str,
        display_name: str | None,
        source: str,
    ) -> None:
        """Seed the in-memory identity index with a pinned person id (P1-107
        step 1) so ``_attribute_person`` links the record to the same person
        the durable write pins."""
        self.communications_service.seed_person_identity(
            person_id=person_id,
            kind=kind,
            value=value,
            display_name=display_name,
            source=source,
        )

    async def persist_durable_communication(self, spec: dict[str, Any]) -> None:
        """Durably persist one message's person + communication (P1-107 step
        2). A no-op when no durable store is bound. A failure is captured (not
        raised) and surfaced by :meth:`flush_durable` so the cursor gate
        blocks."""
        store = self.communications_store
        if store is None:
            return
        try:
            await persist_communication_durably(store, **spec)
        except Exception as exc:  # noqa: BLE001 - surfaced via flush_durable
            if self._durable_failure is None:
                self._durable_failure = exc

    def run_pipeline(self, communication: Communication, *, now: Any) -> Any:
        return self.pipeline.ingest(communication, now=now)

    async def flush_durable(self) -> Exception | None:
        """Settle the pipeline's durable mirrors; surface the first failure.

        The commitment seam and the follow-up / attention mirrors both
        live on the process-wide pipeline / follow-up service, so the
        commit-gate awaits the composed pipeline's
        :meth:`~backend.communications.detection.wiring.CommunicationPipeline.
        flush_durable`. A durable *communication* write failure captured by
        :meth:`persist_durable_communication` is reported first (P1-107 step 3:
        fail before cursor commit if the communication/person persist failed),
        then cleared for the next poll.
        """
        failure, self._durable_failure = self._durable_failure, None
        pipeline_failure = await self.pipeline.flush_durable()
        return failure if failure is not None else pipeline_failure


def default_ingest_seam() -> IngestSeam:
    """The production seam over the process-wide singletons."""
    return DefaultIngestSeam()


def _secret_kind_for(conn: Any) -> str:
    """The durable secret kind a connection authenticates with (§6).

    The scheme follows the connection's kind, not its transient auth
    status: gmail/outlook always authenticate with OAuth access tokens,
    so an expired token must keep resolving ``access_token`` instead of
    falling back to ``app_password`` — which a Gmail/Outlook connection
    never carries, and which turned an expired token into a permanent
    ``connect_failed`` loop.
    """
    config = getattr(conn, "config", None) or {}
    kind = str(config.get("secret_kind") or "").strip()
    if kind:
        return kind
    if str(config.get("auth_method") or "").lower() == "oauth2" or str(
        getattr(conn, "kind", "")
    ).lower() in ("gmail", "graph_mail", "outlook"):
        return "access_token"
    return "app_password"


# -- transport-error redaction (GAP 2: §4 — never log/emit credentials) --------


def redact_transport_error(message: Any, *, limit: int = 256) -> str:
    """Stable, redacted form of a transport exception for durable storage.

    Delegates to the shared :func:`backend.connectors.redact.redact_message`
    so the mail worker, health, calendar, and supervisor planes all use one
    redactor. The returned text carries the message's *shape* with every
    credential-bearing fragment removed: URL ``user:pass@`` userinfo is
    stripped to ``user@`` and token-shaped substrings are replaced by
    ``REDACTED`` markers. The result is truncated to ``limit`` characters
    so the ``last_error_message`` column stays bounded.
    """
    from backend.connectors.redact import redact_message

    return redact_message(message, limit=limit)


def _content_key(connection_id: str, mail_id: str) -> str:
    """Stable, secret-free object-store key for one fetched message."""
    digest = hashlib.sha256(f"{connection_id}:{mail_id}".encode("utf-8")).hexdigest()[:24]
    return f"mail/{connection_id}/{digest}"


def _communication_id(connection_id: str, mail_id: str) -> str:
    """Deterministic communication id so re-ingest is an idempotent update
    (same ``communication_id`` → ``created=False``), never a duplicate row."""
    digest = hashlib.sha256(f"{connection_id}:{mail_id}".encode("utf-8")).hexdigest()[:24]
    return f"comm-mail-{digest}"


def _durable_communication_id(connection_id: str, mail_id: str) -> str:
    """Deterministic durable ``communications.id`` for one message (P1-107).

    Pinned to the (connection, mail-id) pair — the same message re-ingested
    after a crash reuses the same durable row id, so a replay between the
    durable write and the cursor commit is a get-or-create no-op, never a
    duplicate row (idempotency decision (c))."""
    digest = hashlib.sha256(
        f"durable-comm:{connection_id}:{mail_id}".encode("utf-8")
    ).hexdigest()[:24]
    return f"dcomm-mail-{digest}"


def _person_id_for_handle(kind: str, canonical: str) -> str:
    """Deterministic person id derived from a channel-kind + canonical handle
    (P1-107 decision (b)).

    The id is a pure function of the *normalized* identity value, so the
    in-memory service (seeded by the worker via ``ingest_identity`` with this
    explicit ``person_id``) and the durable ``people.id`` (pinned via
    ``create_person(id=...)``) stay the same row for the same sender — and a
    replay after a crash resolves to the same person (no duplicate people).
    """
    digest = hashlib.sha256(
        f"person:{kind}:{canonical}".encode("utf-8")
    ).hexdigest()[:24]
    return f"person-mail-{digest}"


def _parse_sender(raw: str) -> tuple[str, str | None]:
    """Split a raw ``From`` header into ``(address, display_name)``.

    Uses the stdlib RFC-822 address parser; the display name is ``None`` when
    the header carries only a bare address. The address is returned trimmed
    and never empty (falls back to the raw header when parsing yields
    nothing) so an identity value can always be derived."""
    import email.utils

    display, address = email.utils.parseaddr(str(raw or ""))
    address = (address or "").strip() or str(raw or "").strip()
    display = (display or "").strip() or None
    return address, display


# -- single-flight OAuth refresh (GAP 1 race guard) ---------------------------
#
# Two live :class:`MailIngestWorker` instances can exist for the same
# connection: the durable loop worker (supervisor ``_spawn_for``) and the
# one-shot ``sync_once`` worker the control-plane "Sync Now" trigger builds
# (``MailSyncTrigger`` → ``supervisor.sync_once``). Each builds its own
# ``MailOAuthRefresher``, so without a guard both could read the same stored
# ``refresh_token`` and fire two concurrent refresh grants. Providers rotate
# the refresh token on use (GAP 1), so the second grant fails with
# ``invalid_grant`` and the blanket ``except`` in
# :meth:`MailIngestWorker._attempt_token_refresh` would then map the
# connection to ``auth_expired`` even though the *first* refresh persisted a
# perfectly good rotated token. The lock below makes the refresh
# refresh+persist rotation single-flight per connection: the concurrent caller
# waits, then re-reads the vault (which now holds the rotated token) on its
# next transport pass instead of re-refreshing. Process-wide on purpose —
# both worker instances share the same event loop; bounded by the (small)
# number of mail connections, so the registry never grows unbounded.

_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


def _refresh_lock(connection_id: str) -> asyncio.Lock:
    """Return (creating on first use) the single-flight refresh lock for one
    connection. Created on the running loop — see the module note above."""
    lock = _REFRESH_LOCKS.get(connection_id)
    if lock is None:
        lock = asyncio.Lock()
        _REFRESH_LOCKS[connection_id] = lock
    return lock


class MailIngestWorker:
    """Durable ingest loop for one mail connection.

    Construct per connection (the supervisor does); :meth:`start` spawns the
    loop task, :meth:`stop` signals a clean exit. All resumable state lives
    in the database — the instance carries only backoff counters and its
    stop event, so a supervisor restart rebuilds workers from scratch.
    """

    def __init__(
        self,
        *,
        connection_id: str,
        cursor_kind: str,
        connections: Any,
        imports: Any,
        object_store: Any,
        transport: MailTransport,
        seam: IngestSeam | None = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
        sleep_func: Any = asyncio.sleep,
        oauth_refresher: Any | None = None,
    ) -> None:
        self.connection_id = connection_id
        self.cursor_kind = cursor_kind
        self._connections = connections
        self._imports = imports
        self._object_store = object_store
        self._transport = transport
        self._seam = seam if seam is not None else default_ingest_seam()
        self._poll_interval_s = float(poll_interval_s)
        self._backoff_base_s = float(backoff_base_s)
        self._backoff_max_s = float(backoff_max_s)
        self._sleep_func = sleep_func
        #: Optional durable :class:`~backend.connectors.mail.oauth_refresh.
        #: MailOAuthRefresher` — when set, an OAuth transport failure
        #: (``auth_method == "oauth2"``) triggers one refresh attempt before
        #: the auth-expired mapping. Supervisor-wired; tests inject a stub.
        self._oauth_refresher = oauth_refresher
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._failure_count = 0
        self._last_summary: dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the polling loop task (idempotent while running)."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name=f"mail-ingest-{self.connection_id}")

    async def stop(self) -> None:
        """Signal the loop to exit and wait for the task (best-effort)."""
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: B014 - teardown best-effort
            pass
        self._task = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        """A loggable (secret-free) liveness snapshot (spec §16)."""
        return {
            "connection_id": self.connection_id,
            "running": self.is_running,
            "failures": self._failure_count,
            "last": self._last_summary,
        }

    async def _run(self) -> None:
        try:
            while self._stop_event is None or not self._stop_event.is_set():
                delay = await self.poll_once()
                if self._stop_event is not None and self._stop_event.is_set():
                    break
                await self._sleep_func(delay)
        except asyncio.CancelledError:
            raise
        finally:
            self._stop_event = None
            self._task = None

    # -- one poll ----------------------------------------------------------

    async def poll_once(self) -> float:
        """Run one poll cycle; return the delay (seconds) before the next.

        The connection/cursor are re-read from the database every cycle, so
        a restarted worker resumes from the durable position, not memory.
        """
        summary = await self._poll()
        self._last_summary = summary
        status = str(summary.get("status") or "")
        ok = status not in ("auth_expired", "error", "disabled", "missing")
        if ok:
            self._failure_count = 0
            exhausted = bool(summary.get("throttled"))
            return float(self._poll_interval_s / (4 if exhausted else 1))
        self._failure_count += 1
        return self._backoff_delay()

    def _backoff_delay(self) -> float:
        delay = self._backoff_base_s * (2 ** max(0, self._failure_count - 1))
        return min(delay, self._backoff_max_s)

    async def _poll(self) -> dict[str, Any]:
        base = {"connection_id": self.connection_id, "kind": self.cursor_kind}
        conn = await self._connections.get_connection(self.connection_id)
        if conn is None:
            return dict(base, status="missing")
        # Capture the access token this poll is about to present, so a failed
        # OAuth refresh can later tell whether the vault already holds a
        # fresher one (single-flight guard, see ``_attempt_token_refresh``).
        access_token: str | None = None
        if _secret_kind_for(conn) == "access_token":
            access_token = await self._connections.get_secret(
                kind="access_token",
                owner_kind="connection",
                owner_id=self.connection_id,
            )
        if not bool(getattr(conn, "enabled", False)):
            return dict(base, status="disabled")

        cursor_row = await self._imports.get_cursor(self.connection_id, self.cursor_kind)
        cursor: dict[str, Any] = dict(cursor_row.cursor_value or {}) if cursor_row is not None else {}

        try:
            items, next_cursor = await self._run_transport_once(conn, cursor)
        except TransportAuthError as exc:
            # GAP 1: on an OAuth connection, one durable refresh attempt —
            # the rotated ``refresh_token`` is written back to the vault
            # before retrying with the fresh access token.
            retry_ok = False
            if await self._attempt_token_refresh(conn, access_token):
                try:
                    items, next_cursor = await self._run_transport_once(conn, cursor)
                    retry_ok = True
                except Exception:  # noqa: BLE001 - fresh token also failed
                    retry_ok = False
            if retry_ok:
                log.info(
                    "mail_ingest.auth_refreshed",
                    **base,
                )
                # The fresh token was accepted: continue the normal
                # processing path (batch ingest + cursor + healthy mapping).
            else:
                detail = redact_transport_error(str(exc))
                await self._connections.update_connection(
                    self.connection_id,
                    auth_status="auth_expired",
                    operational_status="error",
                    last_error_code="auth_expired",
                    last_error_message=detail,
                )
                log.warning(
                    "mail_ingest.auth_expired",
                    **base,
                    error_code="auth_expired",
                    error=detail,
                )
                return dict(base, status="auth_expired")
        except TransportConnectError as exc:
            detail = redact_transport_error(str(exc))
            await self._connections.update_connection(
                self.connection_id,
                operational_status="error",
                last_error_code="connect_failed",
                last_error_message=detail,
            )
            log.warning(
                "mail_ingest.connect_failed",
                **base,
                error_code="connect_failed",
                error=detail,
            )
            return dict(base, status="error")
        except TransportProviderError as exc:
            # The credential is VALID — the provider is misconfigured (Gmail
            # API disabled on the GCP project, or insufficient OAuth scope).
            # Do NOT flip auth to ``auth_expired`` (re-authenticating would not
            # help and misled diagnosis badly in production). Record the stable
            # code the transport chose and the REDACTED provider reason so
            # diagnostics can show an actionable recovery step.
            detail = redact_transport_error(str(exc))
            await self._connections.update_connection(
                self.connection_id,
                operational_status="error",
                last_error_code=exc.error_code,
                last_error_message=detail,
            )
            log.warning(
                "mail_ingest.provider_error",
                **base,
                error_code=exc.error_code,
                error=detail,
            )
            return dict(base, status="error", error_code=exc.error_code)
        except Exception as exc:  # noqa: BLE001 - one bad poll must not kill the loop
            detail = redact_transport_error(f"{type(exc).__name__}: {exc}")
            await self._connections.update_connection(
                self.connection_id,
                operational_status="error",
                last_error_code="ingest_failed",
                last_error_message=detail,
            )
            # No ``log.exception``: the exception text is credential-bearing
            # (GAP 2) — only its redacted form may reach the logs.
            log.error(
                "mail_ingest.unexpected",
                **base,
                error_code="ingest_failed",
                error=detail,
            )
            return dict(base, status="error")

        if not isinstance(next_cursor, dict):
            next_cursor = dict(cursor)
        ingested, duplicates, seen = await self._process_batch(conn, items, seen_keys(cursor))
        # P0-D commit-gate: the durable cursor (and its seen-list fold)
        # advances only after the pipeline's durable mirror writes have
        # settled. The seam surfaces the first durable failure; a seam
        # double without ``flush_durable`` is treated as committed
        # (getattr-guarded — its durability is its own concern).
        flush = getattr(self._seam, "flush_durable", None)
        durable_failure: Exception | None = None
        if flush is not None:
            try:
                durable_failure = await flush()
            except Exception as exc:  # noqa: BLE001 - the gate is best-effort
                durable_failure = exc
        if durable_failure is not None:
            detail = redact_transport_error(str(durable_failure))
            await self._connections.update_connection(
                self.connection_id,
                operational_status="error",
                last_error_code="commit_failed",
                last_error_message=detail,
            )
            log.warning(
                "mail_ingest.commit_failed",
                **base,
                error_code="commit_failed",
                error=detail,
            )
            # No cursor advance, no seen-list fold: the transport
            # re-delivers the same items and the mirrors' idempotency
            # (durable dedup by provenance key) absorbs what already
            # committed.
            return dict(base, status="error", error_code="commit_failed")
        await self._imports.set_cursor(
            self.connection_id, self.cursor_kind, cursor_with_seen(next_cursor, seen)
        )
        await self._record_import_progress(conn, ingested)
        await self._connections.update_connection(
            self.connection_id,
            auth_status="authenticated",
            operational_status="healthy",
            last_error_code=None,
            last_error_message=None,
        )
        log.info(
            "mail_ingest.poll_ok",
            **base,
            items=len(items),
            ingested=ingested,
            duplicates=duplicates,
        )
        return dict(
            base,
            status="ok",
            items=len(items),
            ingested=ingested,
            duplicates=duplicates,
            throttled=len(items) > 0,
        )

    # -- batch -------------------------------------------------------------

    async def _process_batch(
        self, conn: Any, items: Any, seen: list[str]
    ) -> tuple[int, int, list[str]]:
        """Store + ingest one poll's mail.

        Returns ``(ingested, duplicates, seen)`` where ``seen`` is the
        updated, capped seen-list to fold into the durable cursor via
        :func:`~backend.ingest.seen.cursor_with_seen`.

        Each message's bytes go to the object store first; the graph record
        carries only the ``content_ref`` (§7). The dedup key is
        (normalized account, mail id) per spec §4.
        """
        account = self._account_label(conn).lower()
        ingested = 0
        duplicates = 0
        for item in items:
            mail_id = str(getattr(item, "message_id", "") or "").strip()
            if not mail_id:
                continue
            seen_key = f"{account}:{mail_id}"
            if is_seen(seen, seen_key):
                duplicates += 1
                continue
            seen = mark_seen(seen, seen_key)
            key = _content_key(self.connection_id, mail_id)
            meta = await self._object_store.put(
                key,
                bytes(item.content_bytes or b""),
                content_type="message/rfc822",
                metadata={"account": account, "message_id": mail_id},
            )
            communication_id = _communication_id(self.connection_id, mail_id)
            content_ref = f"content://{getattr(meta, 'key', key)}"
            subject = item.subject_or_first_line()

            # P1-107 step 1 — PERSON RESOLUTION FIRST. Parse the sender into
            # (address, display name), derive a deterministic person id from
            # the normalized email handle, and seed BOTH planes with that id:
            # the in-memory identity index (so ``_attribute_person`` links the
            # record and the read API shows the person) and — in the durable
            # write below — a pinned ``people.id`` (so the FK is satisfiable
            # and the two planes name the same person). The conservative
            # "never guess" rule in ``_attribute_person`` is untouched; the
            # worker seeds the identity instead of relaxing resolution.
            sender_raw = str(item.headers.get("From") or account)
            address, display_name = _parse_sender(sender_raw)
            person_id = self._seed_person(address, display_name)
            if person_id is not None:
                payload_person_id = person_id
            else:
                payload_person_id = None

            payload: dict[str, Any] = {
                "communication_id": communication_id,
                "person_id": payload_person_id,
                "channel": CommunicationChannel.EMAIL,
                "direction": CommunicationDirection.INCOMING,
                "sender": sender_raw,
                "recipients": [account] if account else [],
                "timestamp": item.timestamp,
                "thread_id": item.headers.get("In-Reply-To") or communication_id,
                "content_ref": content_ref,
                "sensitivity": "PERSONAL",
                "source": f"mail-{self.cursor_kind}",
                "classification": {"subject": subject},
            }
            result = self._seam.ingest_communication(payload)
            # P1-107 step 2 — persist the durable person + communication. The
            # pinned durable ids keep a replay idempotent; a failure here is
            # captured and surfaced through the seam's ``flush_durable`` so the
            # P0-D commit-gate blocks the cursor advance (step 3). A seam
            # double without the durable hook (unit tests) is a no-op.
            persist = getattr(self._seam, "persist_durable_communication", None)
            if persist is not None and person_id is not None:
                await persist(
                    {
                        "person_id": person_id,
                        "display_name": display_name,
                        "identity_kind": CommunicationChannel.EMAIL.value,
                        "identity_handle": address,
                        "channel": CommunicationChannel.EMAIL.value,
                        "direction": CommunicationDirection.INCOMING.value,
                        "content_ref": content_ref,
                        "subject": subject,
                        "received_at": item.timestamp,
                        "communication_id": _durable_communication_id(
                            self.connection_id, mail_id
                        ),
                    }
                )
            communication = result["communication"]
            # P0-C: hand detection the body this worker just stored —
            # read it back out of the object store and register it
            # *transiently* with the detection seam (getattr-guarded).
            # Nothing is persisted; the graph record keeps only the
            # ``content_ref`` pointer. Without this the worker path sees
            # no text and yields zero findings / zero durable rows.
            register = getattr(self._seam, "register_content", None)
            if register is not None:
                try:
                    body = await self._object_store.get(key)
                except Exception:  # noqa: BLE001 - missing body → empty text
                    body = b""
                register(
                    str(
                        getattr(communication, "content_ref", None)
                        or payload["content_ref"]
                    ),
                    body.decode("utf-8", "replace"),
                )
            self._seam.run_pipeline(communication, now=communication.timestamp)
            if bool(result.get("created")):
                ingested += 1
            else:
                duplicates += 1
        return ingested, duplicates, seen

    def _seed_person(self, address: str, display_name: str | None) -> str | None:
        """Derive the deterministic person id for a sender and seed the
        in-memory identity index with it (P1-107 step 1).

        Returns the pinned person id so the payload links the record and the
        durable write pins the same ``people.id``; returns ``None`` when the
        seam exposes no seeding hook (unit-test doubles) so the legacy
        unlinked behavior is preserved for those paths.
        """
        from backend.communications.registry import normalize_identity_value

        seed = getattr(self._seam, "seed_person", None)
        if seed is None:
            return None
        try:
            canonical = normalize_identity_value("email", address)
        except ValueError:
            return None
        person_id = _person_id_for_handle("email", canonical)
        seed(
            person_id=person_id,
            kind="email",
            value=address,
            display_name=display_name,
            source=f"mail-{self.cursor_kind}",
        )
        return person_id

    # -- helpers -----------------------------------------------------------

    async def _run_transport_once(self, conn: Any, cursor: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        """Resolve the transient credential and run one transport pass.

        Re-resolving on every call means a post-refresh retry automatically
        picks up the fresh vault-backed access token (never a cached one).
        """
        credentials = await self._resolve_credentials(conn)
        connector = self._connector_for(conn)
        return await self._transport.run(connector, cursor, credentials=credentials)

    async def _attempt_token_refresh(self, conn: Any, rejected_access_token: str | None) -> bool:
        """One durable OAuth refresh after a credential rejection (GAP 1).

        Applies only to OAuth connections (the worker resolves their
        credential under the ``access_token`` secret kind). The app keys
        (``client_id`` / ``client_secret``) are non-secret config — the
        calendar-plane convention; their absence simply disables refresh
        and falls through to the ``auth_expired`` mapping. The rotated
        ``refresh_token`` and the fresh ``access_token`` are written back
        through the vault-backed ``upsert_secret`` path *before* this
        returns, so a restarted worker resumes with the current tokens.
        Returns ``True`` only when a fresh access token was produced (either
        by this call's refresh, or — the single-flight path — when a
        concurrent refresh already persisted one; see the lock below).

        ``rejected_access_token`` is the access token the *failed* transport
        call presented, captured before the poll. It lets the single-flight
        guard distinguish "the vault already holds a newer access token than
        the one I was rejected on" (a concurrent refresh already ran — use
        that, do **not** re-grant) from "the vault still holds the stale
        token" (genuinely expired — refresh).
        """
        if _secret_kind_for(conn) != "access_token":
            return False
        provider_name = MAIL_OAUTH_PROVIDERS.get(str(getattr(conn, "kind", "")).lower())
        if provider_name is None:
            return False
        # The app keys are deploy-time credentials: read them from the
        # environment (deploy contract) first, falling back to the
        # per-connection config so existing rows/tests that carry it keep
        # working. Mirrors the control plane's ``start_oauth`` /
        # ``RegistryTokenExchanger`` convention.
        import os

        config = getattr(conn, "config", None) or {}
        client_id = str(
            os.environ.get(_CLIENT_ID_ENV[provider_name], "").strip()
            or config.get("client_id", "")
        ).strip()
        client_secret = str(
            os.environ.get(_CLIENT_SECRET_ENV[provider_name], "").strip()
            or config.get("client_secret", "")
        ).strip()
        if not client_id or not client_secret:
            log.warning(
                "mail_worker.refresh_disabled_missing_credentials",
                kind=str(getattr(conn, "kind", "")),
            )
            return False
        refresher = self._oauth_refresher or MailOAuthRefresher(
            connections=self._connections
        )
        try:
            # Single-flight per connection (GAP 1 race guard): the refresh
            # reads the stored refresh token, the provider rotates it on use,
            # and the rotated value is persisted inside this lock. Two live
            # worker instances can exist for one connection — the durable
            # loop worker and the one-shot ``sync_once`` worker the control
            # plane's "Sync Now" trigger builds — each with its own
            # refresher, so both can hit the transport with the same
            # access token in the same poll. The first to grab the lock
            # refreshes; the second waits on it and then compares the
            # vault's access token against the one *it* was rejected on.
            # If the vault is already ahead (the concurrent refresh
            # persisted a fresh token), the second skips the exchange —
            # firing a grant with an already-rotated refresh token is
            # exactly the ``invalid_grant`` failure this guard exists to
            # prevent. The lock is released on every path out of the
            # ``async with`` (success or exception), so a failed refresh
            # cannot wedge later attempts.
            async with _refresh_lock(self.connection_id):
                current = await self._connections.get_secret(
                    kind="access_token",
                    owner_kind="connection",
                    owner_id=self.connection_id,
                )
                if (
                    rejected_access_token is not None
                    and current is not None
                    and current != rejected_access_token
                ):
                    # A concurrent refresh already rotated the token pair
                    # while we were waiting on the lock — do not re-grant;
                    # the caller's retry (which re-resolves credentials)
                    # picks up the fresh token.
                    return True
                await refresher.refresh(
                    connection_id=self.connection_id,
                    provider=provider_name,
                    client_id=client_id,
                    client_secret=client_secret,
                )
        except Exception:  # noqa: BLE001 - a failed refresh is not fatal to the loop
            return False
        return True

    async def _resolve_credentials(self, conn: Any) -> dict[str, str]:
        """Decrypt the connection's durable secret into transient credential
        material. The plaintext exists only for the duration of the transport
        call and is never persisted or logged (§4/§6)."""
        kind = _secret_kind_for(conn)
        plaintext = await self._connections.get_secret(
            kind=kind, owner_kind="connection", owner_id=self.connection_id
        )
        if not plaintext:
            raise TransportConnectError(f"no durable secret of kind {kind!r} for connection")
        config = getattr(conn, "config", None) or {}
        if kind == "access_token":
            return {"access_token": plaintext}
        username = str(config.get("username") or config.get("account") or "").strip()
        return {"username": username, "password": plaintext}

    def _connector_for(self, conn: Any) -> MailConnector:
        """Build the transport's :class:`MailConnector` from the connection's
        non-secret ``config`` (the registry's connector description)."""
        from backend.api.models import MailConnector  # noqa: PLC0415 - lazy, breaks the app import cycle

        config = getattr(conn, "config", None) or {}
        mailbox = str(config.get("account") or config.get("mailbox") or "").strip()
        host = str(config.get("imap_host") or config.get("host") or "").strip()
        return MailConnector(
            connector_id=self.connection_id,
            account=str(getattr(conn, "label", "") or mailbox or conn.kind),
            mailbox=mailbox,
            host=host,
            port=int(config.get("port") or 993),
            ssl=bool(config.get("ssl", True)),
            token_ref=f"secrets:{_secret_kind_for(conn)}",
        )

    def _account_label(self, conn: Any) -> str:
        config = getattr(conn, "config", None) or {}
        return str(config.get("account") or config.get("mailbox") or getattr(conn, "label", "") or conn.id)

    async def _record_import_progress(self, conn: Any, ingested: int) -> None:
        """Keep the initial-history import job current in ``import_jobs``.

        The job id is persisted in the connection's non-secret ``config``
        (``import_job_id``) so a restart resumes the same job row instead of
        opening a new one (§4: explicit initial-history import).

        Honest completion (P1-107): this runs only AFTER the P0-D commit-gate
        in ``_poll`` cleared (the durable person + communication writes
        settled), so ``ingested`` here reflects messages that actually
        persisted durably — not merely fetched items. On a successful durable
        poll the job is moved to the terminal ``done`` state (the frozen
        ``VALID_JOB_STATES`` spelling of "completed") instead of sticking in
        ``running``; a later poll with new mail reopens a fresh job row.
        """
        config = dict(getattr(conn, "config", None) or {})
        job_id = str(config.get("import_job_id") or "").strip()
        job = await self._imports.get_import_job(job_id) if job_id else None
        # A finished (``done``/``failed``) job from a previous poll is not
        # reused — open a fresh running job for this poll's progress.
        if job is not None and str(getattr(job, "state", "")) in ("done", "failed"):
            job = None
        if job is None:
            job = await self._imports.create_import_job(
                self.connection_id, self.cursor_kind, state="running"
            )
            config["import_job_id"] = job.id
            await self._connections.update_connection(self.connection_id, config=config)
        cursor_row = await self._imports.get_cursor(self.connection_id, self.cursor_kind)
        progress = dict(cursor_row.cursor_value or {}) if cursor_row is not None else {}
        progress["ingested"] = int(progress.get("ingested", 0) or 0) + ingested
        await self._imports.update_import_job_progress(job.id, progress=progress)
        # Successful durable poll → the import job is complete (§4 explicit
        # initial-history import; the two-axis status model keeps the
        # connection row's own healthy/authenticated axes separate).
        await self._imports.finish_import_job(job.id, state="done")
