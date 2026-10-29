"""Connection control plane — the durable connection-management service.

This is workstream B's service layer, sitting on top of workstream F's frozen
durable boundary (:class:`~backend.persistence.repositories.RepositoryRegistry`).
It owns the **Connection Center** contract (spec ``personal-ops-recovery`` §3):

- the two orthogonal axes ``auth_status`` × ``operational_status`` and the
  frozen five-state UI derivation (``state``);
- the encrypted secret vault (workstream B's :class:`~backend.connectors.vault.FernetVault`)
  behind the durable :class:`~backend.persistence.repositories.connections_repo.SecretCodec`;
- login probes (IMAP reuses the frozen :class:`ImapClient` seam; other kinds
  use an injectable :class:`ProbeSeam`), OAuth flow start, sync/import
  delegation to an injectable :class:`IngestTrigger`, and actionable
  diagnostics per stable error code.

Hard rule (frozen): a successful login probe sets ``auth_status`` but never
moves ``operational_status`` past ``never_synced``. Only a real ingest — via
:meth:`ConnectionControlPlane.record_sync_success` — marks a connection
``healthy``.

All repository access is async; the service is bound onto ``app.state`` by
:mod:`backend.api.app` and consumed by the ``/api/connections`` and
``/api/relay`` routers.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets as _secrets
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import structlog

from ..persistence.models.personal import ConnectionRow
from ..persistence.repositories import RepositoryRegistry
from .mail.imap_client import ImapClient, ImapProbeResult
from .mail.oauth import (
    OauthProviderName,
    build_authorization_url,
    make_pkce_pair,
)
from .vault import VaultError, load_from_env

log = structlog.get_logger(__name__)

# -- frozen constants (spec §3 / §6) ------------------------------------------

#: Connection kinds the control plane manages.
CONNECTION_KINDS = ("gmail", "imap", "graph_mail", "relay_device", "calendar")

#: Secret kinds owned by a connection row (owner_kind="connection").
APP_PASSWORD = "app_password"
RELAY_TOKEN = "relay_token"
ACCESS_TOKEN = "access_token"
REFRESH_TOKEN = "refresh_token"

#: Stable error codes (machine-readable; never free-form text).
ERR_CONNECTION_NOT_FOUND = "connection_not_found"
ERR_KIND_NOT_SUPPORTED = "kind_not_supported"
ERR_BAD_REQUEST = "bad_request"
ERR_VAULT_UNAVAILABLE = "vault_unavailable"
ERR_MISSING_CREDENTIAL = "missing_credential"
ERR_IMAP_AUTH_REJECTED = "imap_auth_rejected"
ERR_IMAP_UNREACHABLE = "imap_unreachable"
ERR_OAUTH_INCOMPLETE = "oauth_incomplete"
ERR_RELAY_TOKEN_INVALID = "relay_token_invalid"
ERR_INGEST_UNAVAILABLE = "ingest_unavailable"
ERR_CONSENT_REQUIRED = "consent_required"
ERR_PROBE_UNSUPPORTED = "probe_unsupported"
ERR_TRIGGER_FAILED = "ingest_failed"

#: Ingest cursor kinds (frozen by spec §6) — mail uses ``imap_seq`` /
#: ``gmail_history``; the relay uses ``relay_seq``.
CURSOR_RELAY = "relay_seq"

#: Bounded size of the relay seen-set kept inside the ``relay_seq`` cursor.
RELAY_SEEN_LIMIT = 500

#: OAuth providers by connection kind (spec §47 secret-free onboarding).
OAUTH_PROVIDER_BY_KIND: dict[str, OauthProviderName] = {
    "gmail": "google",
    "graph_mail": "microsoft",
}

#: Per-kind allow-listed config keys (editable via PATCH). Every other key —
#: especially anything credential-like — is denied.
EDITABLE_CONFIG_KEYS: dict[str, tuple[str, ...]] = {
    "gmail": ("account", "host", "port", "ssl", "oauth_redirect_uri"),
    "imap": ("account", "host", "port", "ssl"),
    "graph_mail": ("account", "host", "port", "ssl", "tenant_id"),
    "relay_device": ("device_name",),
    "calendar": ("account", "host", "port", "ssl"),
}

#: Config keys that are *never* accepted (credential-like / internal).
DENIED_CONFIG_KEYS = (
    "password",
    "token",
    "app_password",
    "secret",
    "client_secret",
    "device_token",
    "access_token",
    "refresh_token",
    "secret_ref",
    "oauth_state",
    "oauth_code_verifier",
)

#: Stable error code → plain-language recovery action (frozen diagnostics map).
DIAGNOSTICS_FIXES: dict[str, str] = {
    ERR_IMAP_AUTH_REJECTED: (
        "The mailbox rejected the stored credential. Re-enter the app password "
        "or re-run the provider sign-in, then verify again."
    ),
    ERR_IMAP_UNREACHABLE: (
        "The IMAP host could not be reached. Check the host/port (993 over TLS "
        "is standard), your network, and any firewall or VPN rules."
    ),
    ERR_OAUTH_INCOMPLETE: (
        "Provider sign-in did not finish. Start the connection's sign-in flow "
        "again and complete it in the browser."
    ),
    ERR_RELAY_TOKEN_INVALID: (
        "The relay pairing token is not recognised. Pair the device again from "
        "the Connection Center to mint a fresh token."
    ),
    ERR_INGEST_UNAVAILABLE: (
        "No importer is registered for this connection kind yet. Enable the "
        "ingest supervisor (workstream C) or register a trigger, then retry."
    ),
    ERR_CONSENT_REQUIRED: (
        "Initial history import needs explicit consent. Re-send the request "
        "with consent set to true."
    ),
    ERR_MISSING_CREDENTIAL: (
        "No credential is stored for this connection. Store an app password or "
        "complete the sign-in flow, then verify."
    ),
    ERR_PROBE_UNSUPPORTED: (
        "This connection kind has no login probe yet. Store its credential and "
        "run a sync instead."
    ),
    ERR_TRIGGER_FAILED: (
        "The importer reported a failure. Check diagnostics for the error "
        "code and retry."
    ),
    ERR_VAULT_UNAVAILABLE: (
        "The secret vault is unavailable (NERVE_SECRET_KEY missing or a stored "
        "secret cannot be decrypted). Restore the key, then re-store the credential."
    ),
    ERR_CONNECTION_NOT_FOUND: (
        "This connection no longer exists. Re-create it from the Connection Center."
    ),
}

AUTH_STATUSES = ("unconfigured", "pairing_pending", "authenticated", "auth_expired", "auth_failed")
OPERATIONAL_STATUSES = ("never_synced", "syncing", "healthy", "degraded", "error")

#: auth values that mean "not configured / absent" (→ state ``not_configured``).
_AUTH_NOT_CONFIGURED = ("unconfigured",)
#: auth values that mean "re-authenticate" (→ state ``auth_required``).
_AUTH_REQUIRED = ("auth_expired", "auth_failed")


class ProbeResultTyped:
    """Outcome of a provider login probe (returned by :class:`ProbeSeam`)."""

    def __init__(
        self,
        success: bool,
        error_code: str | None,
        error_message: str | None,
        capabilities: tuple[str, ...],
    ) -> None:
        self.success = success
        self.error_code = error_code
        self.error_message = error_message
        self.capabilities = capabilities


@runtime_checkable
class ProbeSeam(Protocol):
    """Injectable login-probe seam (gmail / graph / calendar / imap).

    Production and tests inject implementations; no real network happens in
    tests. The IMAP path is backed by the frozen :class:`ImapClient` seam
    (adapted via :func:`make_imap_probe`).
    """

    async def probe(self, config: dict[str, Any], secret: str | None) -> ProbeResultTyped: ...


@runtime_checkable
class IngestTrigger(Protocol):
    """Injectable ingest work delegate (workstream C provides the real one).

    A trigger is registered per connection kind; ``sync_now`` / ``import_now``
    delegate the actual work here and leave ``operational_status=syncing``
    while the trigger runs.
    """

    async def start(self, connection_row: Any) -> None: ...


@runtime_checkable
class TokenExchanger(Protocol):
    """Injectable OAuth code→token exchanger (no network in tests).

    Returns either the bare access token (``str`` — legacy) or the full
    provider token-grant mapping (``{"access_token": …, "refresh_token": …}``)
    when the exchanger completes the code exchange itself (the relay,
    prompt2 §47). :meth:`ConnectionControlPlane.complete_oauth` persists the
    durable ``access_token`` in both shapes — and, when the grant carries a
    ``refresh_token``, that durable kind as well, which the mail worker's
    GAP-1 refresh (``MailOAuthRefresher``) reads back from the vault.
    """

    async def exchange(
        self, provider: OauthProviderName, code: str, state: str
    ) -> str | dict[str, str]: ...


class ConnectionControlPlaneError(Exception):
    """Control-plane failure carrying a stable :attr:`code`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# -- helpers ------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _utc_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _strip_secret_keys(config: dict[str, Any]) -> dict[str, Any]:
    """Return ``config`` minus any credential-like keys (defence in depth)."""
    return {k: v for k, v in config.items() if k not in DENIED_CONFIG_KEYS}


def derive_state(auth_status: str | None, operational_status: str | None, *, has_error: bool) -> str:
    """Frozen two-axis → five-state derivation (spec §3).

    Precedence:

    1. operational ``syncing``                → ``syncing``
       (an in-flight sync is the one state that outranks *every* auth
       axis — even an unconfigured credential; a worker is actively
       trying.)
    2. ``unconfigured`` / absent auth          → ``not_configured``
    3. ``auth_expired`` / ``auth_failed``     → ``auth_required``
    4. authenticated + ``healthy``             → ``connected``
    5. other non-final auth (``pairing_pending``)
                                                 → ``auth_required``
    6. authenticated + (``degraded`` / ``error``
       / ``never_synced`` with a recorded error) → ``degraded``
    7. authenticated + ``never_synced`` with
       no error                                → ``connected``

    A connection that authenticated but has never successfully ingested (no
    ``last_success_at`` and no recorded error) is still **connected** — its
    operational axis is ``never_synced``, not an error.
    """
    auth = (auth_status or "unconfigured").strip()
    op = (operational_status or "never_synced").strip()
    if op == "syncing":
        return "syncing"
    if auth in _AUTH_NOT_CONFIGURED:
        return "not_configured"
    if auth in _AUTH_REQUIRED:
        return "auth_required"
    if auth != "authenticated":
        # pairing_pending / other non-final auth values → surface as auth_required.
        return "auth_required"
    if op == "healthy":
        return "connected"
    if op in ("degraded", "error") or (op == "never_synced" and has_error):
        return "degraded"
    return "connected"


def make_imap_probe(client: ImapClient) -> ProbeSeam:
    """Adapt the frozen :class:`ImapClient` seam into a :class:`ProbeSeam`.

    Maps :class:`ImapProbeResult` onto :class:`ProbeResultTyped` with stable
    error codes (``imap_auth_rejected`` / ``imap_unreachable``). The real
    probe blocks on :mod:`imaplib`, so it runs in a worker thread.
    """

    class _ImapProbeSeam:
        async def probe(self, config: dict[str, Any], secret: str | None) -> ProbeResultTyped:
            host = str(config.get("host", ""))
            try:
                port = int(config.get("port", 993))
            except (TypeError, ValueError):
                port = 993
            ssl = bool(config.get("ssl", True))
            username = str(config.get("account", ""))
            try:
                result = await asyncio.to_thread(
                    client.probe, host, port, ssl, username, secret or ""
                )
            except Exception as exc:  # noqa: BLE001 - probe seam must not crash the plane
                return ProbeResultTyped(False, ERR_IMAP_UNREACHABLE, str(exc), ())
            if result.outcome is ImapProbeResult.OK:
                return ProbeResultTyped(True, None, None, result.capabilities)
            if result.outcome is ImapProbeResult.AUTH_FAILED:
                return ProbeResultTyped(False, ERR_IMAP_AUTH_REJECTED, result.error, ())
            return ProbeResultTyped(False, ERR_IMAP_UNREACHABLE, result.error, ())

    return _ImapProbeSeam()


def _public(row: ConnectionRow) -> dict[str, Any]:
    """Project a row onto the frozen response surface (always both axes)."""
    has_error = bool(row.last_error_code) and row.last_success_at is None
    return {
        "id": row.id,
        "kind": row.kind,
        "label": row.label,
        "enabled": row.enabled,
        "config": _strip_secret_keys(row.config or {}),
        "secret_ref": (row.config or {}).get("secret_ref"),
        "auth_status": row.auth_status,
        "operational_status": row.operational_status,
        "state": derive_state(row.auth_status, row.operational_status, has_error=has_error),
        "last_success_at": _utc_iso(row.last_success_at),
        "last_attempt_at": _utc_iso(row.last_attempt_at),
        "last_error_code": row.last_error_code,
        "last_error_message": row.last_error_message,
        "import_progress": row.import_progress or {},
        "created_at": _utc_iso(row.created_at),
        "updated_at": _utc_iso(row.updated_at),
    }


def _redact(message: str | None) -> str | None:
    """Bound the length of a stored error message (never a credential)."""
    if message is None:
        return None
    return str(message)[:2000]


def _token_equals(a: str, b: str) -> bool:
    """Constant-time token comparison."""
    return _secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _token_grant_parts(token: Any) -> tuple[str, str]:
   """Normalise a :class:`TokenExchanger` result to ``(access, refresh)``.

   A legacy exchanger returns the bare access token (``str``); an exchanger
   that completes the code exchange itself may return the provider's full
   grant mapping. Both shapes are accepted so the frozen ``FakeExchanger``
   contract keeps working; a grant without a refresh token simply yields an
   empty refresh part (access-only onboarding). Token values never appear in
   logs or raised messages — failures carry stable codes only.
   """
   if isinstance(token, str):
       return token.strip(), ""
   if isinstance(token, dict):
       return (
           str(token.get("access_token") or "").strip(),
           str(token.get("refresh_token") or "").strip(),
       )
   return "", ""


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _display_from_handle(handle: str) -> str:
    text = (handle or "").strip()
    if not text:
        return "Unknown"
    return text.split("@")[0].split("/")[0].split("|")[0].strip() or text


# -- the control plane ---------------------------------------------------------


class ConnectionControlPlane:
    """Durable connection-management control plane.

    The constructor takes the frozen :class:`RepositoryRegistry` plus an
    optional :class:`~backend.connectors.vault.FernetVault` (defaults to
    :func:`load_from_env`) and an optional ``imap_client`` seam. Everything is
    swappable for tests.
    """

    def __init__(
        self,
        registry: RepositoryRegistry,
        *,
        vault: Any | None = None,
        imap_client: ImapClient | None = None,
    ) -> None:
        self._registry = registry
        if vault is None:
            try:
                vault = load_from_env()
            except VaultError as exc:
                # No durable key: the plane is still usable for credential-less
                # config (no secrets are ever persisted); the vault seam stays
                # unset so secret operations fail with a stable code.
                log.warning("control_plane.vault_absent", code=exc.code)
                vault = None
        self._vault = vault
        self._imap_client = imap_client
        self._triggers: dict[str, IngestTrigger] = {}
        self._probes: dict[str, ProbeSeam] = {}
        self._token_exchanger: TokenExchanger | None = None

    # -- injection seams ------------------------------------------------------

    def register_trigger(self, kind: str, trigger: IngestTrigger) -> None:
        """Register an :class:`IngestTrigger` for a connection kind (workstream C)."""
        self._triggers[kind] = trigger

    def register_probe(self, kind: str, probe: ProbeSeam) -> None:
        """Register an injectable :class:`ProbeSeam` for a connection kind."""
        self._probes[kind] = probe

    def set_token_exchanger(self, exchanger: TokenExchanger) -> None:
        """Set the injectable :class:`TokenExchanger` for OAuth completion."""
        self._token_exchanger = exchanger

    # -- introspection ---------------------------------------------------------

    @property
    def registry_configured(self) -> bool:
        """``True`` when the durable registry is present (no degradation)."""
        return self._registry is not None

    # -- CRUD -------------------------------------------------------------------

    async def create_connection(
        self,
        *,
        kind: str,
        label: str | None = None,
        config: dict[str, Any] | None = None,
        auth_status: str = "unconfigured",
        enabled: bool = True,
    ) -> ConnectionRow:
        """Create a connection row (config is non-secret only)."""
        if kind not in CONNECTION_KINDS:
            raise ConnectionControlPlaneError(
                ERR_KIND_NOT_SUPPORTED, f"kind {kind!r} is not supported"
            )
        clean = _strip_secret_keys(config or {})
        row = await self._registry.connections.create_connection(
            kind=kind,
            label=label,
            config=clean,
            auth_status=auth_status if auth_status in AUTH_STATUSES else "unconfigured",
            operational_status="never_synced",
            enabled=enabled,
        )
        log.info("control_plane.connection_created", kind=kind, id=row.id)
        return row

    async def store_secret(self, *, connection_id: str, kind: str, plaintext: str) -> None:
        """Encrypt and store a credential via the vault (owner_kind=connection)."""
        if self._vault is None:
            raise ConnectionControlPlaneError(ERR_VAULT_UNAVAILABLE, "vault is not configured")
        row = await self._registry.connections.get_connection(connection_id)
        if row is None:
            raise ConnectionControlPlaneError(
                ERR_CONNECTION_NOT_FOUND, f"connection {connection_id} not found"
            )
        await self._registry.connections.upsert_secret(
            kind=kind, owner_kind="connection", owner_id=connection_id, plaintext=plaintext
        )
        # Remember which secret_ref this connection uses (config, non-secret).
        if row.config.get("secret_ref") != kind:
            new_config = dict(row.config)
            new_config["secret_ref"] = kind
            await self._registry.connections.update_connection(connection_id, config=new_config)

    async def _load_secret(self, row: ConnectionRow, kind: str) -> str | None:
        return await self._registry.connections.get_secret(
            kind=kind, owner_kind="connection", owner_id=row.id
        )

    async def list_connections(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        rows = await self._registry.connections.list_connections(enabled_only=enabled_only)
        return [_public(r) for r in rows]

    async def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        row = await self._registry.connections.get_connection(connection_id)
        return _public(row) if row is not None else None

    async def update_connection(
        self,
        connection_id: str,
        *,
        label: str | None = None,
        config: dict[str, Any] | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        row = await self._registry.connections.get_connection(connection_id)
        if row is None:
            raise ConnectionControlPlaneError(
                ERR_CONNECTION_NOT_FOUND, f"connection {connection_id} not found"
            )
        fields: dict[str, Any] = {}
        if label is not None:
            fields["label"] = label
        if enabled is not None:
            fields["enabled"] = enabled
        if config is not None:
            clean = _strip_secret_keys(config)
            merged = dict(row.config)
            merged.update(clean)
            fields["config"] = merged
        updated = await self._registry.connections.update_connection(connection_id, **fields)
        return _public(updated)

    async def delete_connection(self, connection_id: str) -> bool:
        """Remove the connection and purge every secret it owned."""
        row = await self._registry.connections.get_connection(connection_id)
        if row is None:
            return False
        # Purge every secret kind that may have been stored for this owner.
        for kind in (APP_PASSWORD, ACCESS_TOKEN, REFRESH_TOKEN, RELAY_TOKEN):
            await self._registry.connections.delete_secret(
                kind=kind, owner_kind="connection", owner_id=connection_id
            )
        # Disable in place (the durable layer has no hard delete this wave).
        await self._registry.connections.update_connection(connection_id, enabled=False)
        log.info("control_plane.connection_removed", id=connection_id)
        return True

    # -- login probe / verify ----------------------------------------------------

    async def _probe_for(self, row: ConnectionRow) -> ProbeSeam | None:
        if row.kind in self._probes:
            return self._probes[row.kind]
        if row.kind == "imap" and self._imap_client is not None:
            return make_imap_probe(self._imap_client)
        return None

    async def _run_probe(self, row: ConnectionRow) -> ProbeResultTyped:
        probe = await self._probe_for(row)
        if probe is None:
            return ProbeResultTyped(False, ERR_PROBE_UNSUPPORTED, "no probe for this kind", ())
        secret: str | None = None
        ref = (row.config or {}).get("secret_ref")
        if row.kind == "imap":
            secret = await self._load_secret(row, APP_PASSWORD)
        elif row.kind in OAUTH_PROVIDER_BY_KIND:
            secret = await self._load_secret(row, ACCESS_TOKEN)
        elif ref:
            secret = await self._load_secret(row, str(ref))
        return await probe.probe(row.config or {}, secret)

    async def verify(self, connection_id: str) -> dict[str, Any]:
        """Run the provider login probe.

        On success sets ``auth_status=authenticated`` + ``last_attempt_at``.
        On failure sets a stable ``last_error_code`` + redacted
        ``last_error_message``. ``operational_status`` is *never* advanced by a
        probe — only a real ingest marks the connection healthy.
        """
        row = await self._require_row(connection_id)
        result = await self._run_probe(row)
        now = _now()
        if result.success:
            await self._registry.connections.update_connection(
                connection_id,
                auth_status="authenticated",
                last_attempt_at=now,
                last_error_code=None,
                last_error_message=None,
            )
            updated = await self._registry.connections.get_connection(connection_id)
            public = _public(updated)
            return {**public, "ok": True, "detail": "login probe succeeded"}
        # Failure: keep the existing operational axis untouched; record the
        # stable error code + redacted message and mark auth as failed.
        await self._registry.connections.update_connection(
            connection_id,
            auth_status="auth_failed",
            last_attempt_at=now,
            last_error_code=result.error_code,
            last_error_message=_redact(result.error_message),
        )
        updated = await self._registry.connections.get_connection(connection_id)
        return {
            **_public(updated),
            "ok": False,
            "error_code": result.error_code,
            "error_message": _redact(result.error_message),
        }

    # -- sync / import / reconnect ------------------------------------------------

    async def _require_row(self, connection_id: str) -> ConnectionRow:
        row = await self._registry.connections.get_connection(connection_id)
        if row is None:
            raise ConnectionControlPlaneError(
                ERR_CONNECTION_NOT_FOUND, f"connection {connection_id} not found"
            )
        return row

    async def _start_sync(self, connection_id: str, *, import_kind: str) -> dict[str, Any]:
        row = await self._require_row(connection_id)
        trigger = self._triggers.get(row.kind)
        if trigger is None:
            # Honest failure: no importer registered for this kind yet.
            now = _now()
            await self._registry.connections.update_connection(
                connection_id,
                operational_status="error",
                last_attempt_at=now,
                last_error_code=ERR_INGEST_UNAVAILABLE,
                last_error_message="no ingest trigger registered for this connection kind",
            )
            updated = await self._registry.connections.get_connection(connection_id)
            return {
                **_public(updated),
                "ok": False,
                "error_code": ERR_INGEST_UNAVAILABLE,
            }
        now = _now()
        await self._registry.connections.update_connection(
            connection_id, operational_status="syncing", last_attempt_at=now
        )
        job = await self._registry.imports.create_import_job(
            connection_id, import_kind, state="pending"
        )
        await self._registry.imports.update_import_job_progress(job.id, state="running")
        try:
            await trigger.start(row)
        except VaultError as exc:
            await self.record_sync_failure(connection_id, exc.code, exc.message)
            await self._registry.imports.finish_import_job(job.id, state="failed", error_code=exc.code)
            updated = await self._registry.connections.get_connection(connection_id)
            return {**_public(updated), "ok": False, "error_code": exc.code}
        except Exception as exc:  # noqa: BLE001 - trigger failures become a stable code
            code = getattr(exc, "code", None) or ERR_TRIGGER_FAILED
            await self.record_sync_failure(connection_id, code, str(exc))
            await self._registry.imports.finish_import_job(job.id, state="failed", error_code=code)
            updated = await self._registry.connections.get_connection(connection_id)
            return {**_public(updated), "ok": False, "error_code": code}
        updated = await self._registry.connections.get_connection(connection_id)
        return {**_public(updated), "ok": True, "import_job_id": job.id}

    async def sync_now(self, connection_id: str) -> dict[str, Any]:
        """Mark operational ``syncing``, open an import job, delegate to the trigger."""
        return await self._start_sync(connection_id, import_kind="sync")

    async def import_now(self, connection_id: str) -> dict[str, Any]:
        """Explicit initial-history import (recorded in ``import_jobs``)."""
        return await self._start_sync(connection_id, import_kind="history")

    async def reconnect(self, connection_id: str) -> dict[str, Any]:
        """Re-verify the login then re-sync."""
        verify = await self.verify(connection_id)
        if verify.get("ok") is False:
            # Probe failed → report the auth-side failure, no sync attempted.
            return {**verify, "reconnect": True, "error_code": verify.get("error_code")}
        # Probe succeeded → kick a sync.
        sync = await self.sync_now(connection_id)
        return {**sync, "reconnect": True}

    # -- sync outcome (workstream C calls these per batch) ------------------------

    async def record_sync_success(
        self,
        connection_id: str,
        *,
        cursor_value: dict[str, Any] | None = None,
        ingested_count: int = 0,
    ) -> dict[str, Any]:
        """The **only** path that marks a connection ``operational=healthy``.

        Sets ``last_success_at=now`` and advances the ``imap_seq`` /
        ``relay_seq`` cursor via the imports repository.
        """
        row = await self._require_row(connection_id)
        now = _now()
        await self._registry.connections.update_connection(
            connection_id,
            operational_status="healthy",
            last_success_at=now,
            last_error_code=None,
            last_error_message=None,
        )
        if cursor_value is not None:
            kind = CURSOR_RELAY if row.kind == "relay_device" else "imap_seq"
            try:
                await self._registry.imports.set_cursor(connection_id, kind, cursor_value)
            except ValueError:
                await self._registry.imports.set_cursor(connection_id, "imap_seq", cursor_value)
        updated = await self._registry.connections.get_connection(connection_id)
        return _public(updated)

    async def record_sync_failure(
        self, connection_id: str, error_code: str, error_message: str
    ) -> dict[str, Any]:
        """Record a failed ingest: ``degraded`` on first failure, ``error`` on repeat."""
        row = await self._require_row(connection_id)
        repeated = row.operational_status in ("degraded", "error")
        new_status = "error" if repeated else "degraded"
        now = _now()
        await self._registry.connections.update_connection(
            connection_id,
            operational_status=new_status,
            last_attempt_at=now,
            last_error_code=error_code,
            last_error_message=_redact(error_message),
        )
        updated = await self._registry.connections.get_connection(connection_id)
        return _public(updated)

    # -- permissions ------------------------------------------------------------

    def permissions_for(self, kind: str) -> dict[str, Any]:
        """GET: the effective editable/denied config surface for a kind."""
        if kind not in CONNECTION_KINDS:
            raise ConnectionControlPlaneError(
                ERR_KIND_NOT_SUPPORTED, f"kind {kind!r} is not supported"
            )
        return {
            "kind": kind,
            "editable_keys": list(EDITABLE_CONFIG_KEYS.get(kind, ())),
            "denied_keys": list(DENIED_CONFIG_KEYS),
        }

    async def patch_permissions(self, connection_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """PATCH: apply allow-listed config keys only (secrets are denied)."""
        row = await self._require_row(connection_id)
        allowed = set(EDITABLE_CONFIG_KEYS.get(row.kind, ()))
        clean: dict[str, Any] = {}
        for key, value in (fields or {}).items():
            if key in allowed:
                clean[key] = value
        if clean:
            merged = dict(row.config)
            merged.update(clean)
            await self._registry.connections.update_connection(connection_id, config=merged)
            row = await self._registry.connections.get_connection(connection_id)
        return {
            "ok": True,
            "applied_keys": sorted(clean.keys()),
            "ignored_keys": sorted(set((fields or {}).keys()) - set(clean.keys())),
            **_public(row),
        }

    # -- OAuth flow (gmail / graph_mail) -------------------------------------------

    async def start_oauth(self, connection_id: str) -> dict[str, Any]:
        """Begin a provider sign-in: return ``{authorization_url, state, provider, code_verifier}``.

        The per-flow ``state`` and PKCE ``code_verifier`` are persisted in the
        connection's non-secret ``config`` so :meth:`complete_oauth` can bind
        the callback to this exact flow (CSRF guard, P1-106). Both are denied
        config keys — never echoed back from the public projection.
        """
        row = await self._require_row(connection_id)
        provider_name = OAUTH_PROVIDER_BY_KIND.get(row.kind)
        if provider_name is None:
            raise ConnectionControlPlaneError(
                ERR_OAUTH_INCOMPLETE, f"kind {row.kind!r} does not use OAuth"
            )
        config = row.config or {}
        client_id = str(config.get("client_id", ""))
        redirect_uri = str(config.get("oauth_redirect_uri", ""))
        state = _secrets.token_urlsafe(24)
        verifier, challenge = make_pkce_pair()
        # Persist the per-flow state + PKCE verifier (config, non-secret) for
        # later verification; the challenge goes in the authorize URL.
        merged = dict(config)
        merged["oauth_state"] = state
        merged["oauth_code_verifier"] = verifier
        await self._registry.connections.update_connection(connection_id, config=merged)
        url = build_authorization_url(
            provider_name,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=challenge,
        )
        return {
            "authorization_url": url,
            "state": state,
            "code_verifier": verifier,
            "provider": provider_name,
        }

    async def complete_oauth(self, connection_id: str, code: str, state: str) -> dict[str, Any]:
        """Verify the callback state, exchange the code, store the token.

        The callback ``state`` is compared in constant time against the
        per-flow state persisted by :meth:`start_oauth`; a mismatch is a CSRF
        attempt and is rejected with ``oauth_incomplete`` *before* any token
        exchange happens. A consumed state is removed on success, so the same
        code+state pair can only ever be exchanged once.
        """
        row = await self._require_row(connection_id)
        provider_name = OAUTH_PROVIDER_BY_KIND.get(row.kind)
        if provider_name is None:
            raise ConnectionControlPlaneError(
                ERR_OAUTH_INCOMPLETE, f"kind {row.kind!r} does not use OAuth"
            )
        expected_state = str((row.config or {}).get("oauth_state") or "")
        # An empty persisted state means no flow was started (or it was
        # already consumed) — reject without touching the exchanger.
        if not expected_state or not _token_equals(expected_state, state):
            raise ConnectionControlPlaneError(
                ERR_OAUTH_INCOMPLETE, "callback state does not match the started flow"
            )
        if self._token_exchanger is None:
            raise ConnectionControlPlaneError(
                ERR_OAUTH_INCOMPLETE, "no token exchanger registered"
            )
        token = await self._token_exchanger.exchange(provider_name, code, state)
        if not token:
            raise ConnectionControlPlaneError(ERR_OAUTH_INCOMPLETE, "token exchange returned empty")
        # Consume the one-shot state so a replayed callback cannot re-exchange.
        config = dict(row.config or {})
        config.pop("oauth_state", None)
        config.pop("oauth_code_verifier", None)
        await self._registry.connections.update_connection(connection_id, config=config)
        # Persist the durable token kinds. The legacy string exchanger stores
        # only an access token; a grant mapping also carries a refresh token,
        # which the mail worker's GAP-1 refresh reads back from the vault.
        # Both are written under their durable kinds (vault-encrypted), so a
        # restarted worker resumes with the current tokens — the refresh token
        # is never lost on rotation.
        access, refresh = _token_grant_parts(token)
        if not access:
            raise ConnectionControlPlaneError(
                ERR_OAUTH_INCOMPLETE, "token exchange returned no access token"
            )
        await self.store_secret(
            connection_id=connection_id, kind=ACCESS_TOKEN, plaintext=access
        )
        if refresh:
            await self.store_secret(
                connection_id=connection_id, kind=REFRESH_TOKEN, plaintext=refresh
            )
        await self._registry.connections.update_connection(
            connection_id, auth_status="authenticated", last_attempt_at=_now()
        )
        updated = await self._registry.connections.get_connection(connection_id)
        return {**_public(updated), "ok": True, "detail": "OAuth token stored"}

    # -- diagnostics -----------------------------------------------------------------

    async def diagnostics(self, connection_id: str) -> dict[str, Any]:
        """Actionable plain-language fix per error code + timestamps + job state."""
        row = await self._require_row(connection_id)
        code = row.last_error_code
        fix = DIAGNOSTICS_FIXES.get(code or "", "No errors recorded for this connection.")
        job_state = None
        last_job_id = (row.import_progress or {}).get("last_job_id")
        if last_job_id:
            job = await self._registry.imports.get_import_job(str(last_job_id))
            if job is not None:
                job_state = {
                    "id": job.id,
                    "kind": job.kind,
                    "state": job.state,
                    "progress": job.progress or {},
                    "error_code": job.error_code,
                    "started_at": _utc_iso(job.started_at),
                    "finished_at": _utc_iso(job.finished_at),
                }
        return {
            **_public(row),
            "diagnosis": {
                "error_code": code,
                "fix": fix,
                "last_attempt_at": _utc_iso(row.last_attempt_at),
                "last_success_at": _utc_iso(row.last_success_at),
                "import_job": job_state,
            },
        }

    # -- relay plane -------------------------------------------------------------------

    async def relay_pair(self, *, device_name: str | None = None) -> dict[str, Any]:
        """POST /api/relay/pair — mint a pairing code + device token (returned once)."""
        device_id = f"relay-{_secrets.token_hex(8)}"
        token = _secrets.token_urlsafe(24)
        row = await self._registry.connections.create_connection(
            kind="relay_device",
            label=device_name or "Apple Messages relay",
            config={"device_name": device_name or "Apple Messages relay"},
            auth_status="pairing_pending",
            operational_status="never_synced",
            enabled=True,
        )
        if self._vault is None:
            raise ConnectionControlPlaneError(
                ERR_VAULT_UNAVAILABLE, "vault is not configured; cannot mint a relay token"
            )
        await self._registry.connections.upsert_secret(
            kind=RELAY_TOKEN, owner_kind="connection", owner_id=row.id, plaintext=token
        )
        await self._registry.connections.update_connection(
            row.id,
            config={
                "device_id": device_id,
                "device_name": device_name or "Apple Messages relay",
            },
        )
        # Token is returned exactly once (this response); it is never read back
        # into a subsequent response or log.
        return {"device_id": device_id, "token": token, "connection_id": row.id}

    async def relay_touch(self, *, device_id: str, stored_count: int = 0) -> dict[str, Any] | None:
        """Update last-seen / last-message / pending-backlog bookkeeping.

        ``pending_backlog`` is the number of messages received in the most
        recent batch that have not yet been acknowledged as processed.
        """
        conn = await self._find_relay_connection(device_id)
        if conn is None:
            return None
        now = _now()
        config = dict(conn.config or {})
        config["last_seen_at"] = now.isoformat()
        if stored_count:
            config["last_message_at"] = now.isoformat()
        config["pending_backlog"] = int(stored_count)
        await self._registry.connections.update_connection(conn.id, config=config)
        updated = await self._registry.connections.get_connection(conn.id)
        return _public(updated)

    async def relay_register(self, *, device_id: str, token: str) -> dict[str, Any]:
        """POST /api/relay/register — authenticate with the pairing token."""
        conn = await self._find_relay_connection(device_id)
        if conn is None:
            raise ConnectionControlPlaneError(
                ERR_RELAY_TOKEN_INVALID, f"relay device {device_id!r} is unknown"
            )
        stored = await self._registry.connections.get_secret(
            kind=RELAY_TOKEN, owner_kind="connection", owner_id=conn.id
        )
        if stored is None or not _token_equals(token, stored):
            raise ConnectionControlPlaneError(
                ERR_RELAY_TOKEN_INVALID, "pairing token does not match"
            )
        now = _now()
        await self._registry.connections.update_connection(
            conn.id, auth_status="authenticated", last_attempt_at=now
            # operational intentionally stays never_synced
        )
        updated = await self._registry.connections.get_connection(conn.id)
        return {**_public(updated), "ok": True, "detail": "relay device registered"}

    async def relay_verify_token(self, *, device_id: str, token: str) -> bool:
        """Constant-time check that ``token`` is the device's registered relay token.

        Returns ``False`` (never raises, never distinguishes the reason) for an
        empty token, an unknown device, or a token that does not match the
        stored :data:`RELAY_TOKEN` credential — so the caller can map every
        failure mode to a single generic 401 without leaking which check failed.
        """
        if not token:
            return False
        row = await self._find_relay_connection(device_id)
        if row is None:
            return False
        stored = await self._registry.connections.get_secret(
            kind=RELAY_TOKEN, owner_kind="connection", owner_id=row.id
        )
        if stored is None:
            return False
        return _token_equals(token, stored)

    async def _find_relay_connection(self, device_id: str) -> ConnectionRow | None:
        rows = await self._registry.connections.list_connections()
        for row in rows:
            if row.kind == "relay_device" and (row.config or {}).get("device_id") == device_id:
                return row
        return None

    async def relay_ingest_batch(
        self, *, device_id: str, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """POST /api/relay/messages — durable batch ingest with (device_id, message_id) dedup."""
        conn = await self._find_relay_connection(device_id)
        if conn is None:
            raise ConnectionControlPlaneError(
                ERR_RELAY_TOKEN_INVALID, f"relay device {device_id!r} is unknown"
            )
        if conn.auth_status != "authenticated":
            raise ConnectionControlPlaneError(
                ERR_RELAY_TOKEN_INVALID, "relay device is not registered"
            )
        cursor_row = await self._registry.imports.get_cursor(conn.id, CURSOR_RELAY)
        cursor_value = dict((cursor_row.cursor_value or {}) if cursor_row is not None else {})
        seen: list[str] = list(cursor_value.get("seen", []))
        stored = 0
        skipped = 0
        for message in messages or []:
            message_id = str(message.get("message_id", "")).strip()
            if not message_id:
                continue
            dedup_key = f"{device_id}:{message_id}"
            if dedup_key in seen:
                skipped += 1
                continue
            stored += 1
            await self._relay_store_message(conn, device_id, message, seen)
        # Persist the bounded seen-set (drop the oldest beyond the limit).
        seen = seen[-RELAY_SEEN_LIMIT:]
        await self._registry.imports.set_cursor(
            conn.id, CURSOR_RELAY, {"seen": seen, "last_ingest_at": _now().isoformat()}
        )
        await self.record_sync_success(conn.id, cursor_value={"seen": seen})
        await self.relay_touch(device_id=device_id, stored_count=stored)
        return {"ok": True, "stored": stored, "skipped": skipped}

    async def _relay_store_message(
        self, conn: ConnectionRow, device_id: str, message: dict[str, Any], seen: list[str]
    ) -> None:
        message_id = str(message.get("message_id", "")).strip()
        seen.append(f"{device_id}:{message_id}")
        channel = str(message.get("channel", "imessage"))
        direction = str(message.get("direction", "inbound"))
        body = str(message.get("body", ""))
        peer = str(message.get("peer_identifier", "")).strip()
        peer_name = message.get("peer_name")
        received_at = _parse_dt(message.get("received_at")) or _now()
        # Stable, non-empty content_ref (object-storage pattern; in-memory OK).
        digest = hashlib.sha256(f"{device_id}:{message_id}:{body}".encode("utf-8")).hexdigest()
        content_ref = f"relay://{device_id}/msg/{digest}"
        person = await self._resolve_person(conn, channel, peer, peer_name)
        await self._registry.communications.create_communication(
            person_id=person.id,
            channel=channel,
            direction=direction,
            content_ref=content_ref,
            subject=None,
            sensitivity="PERSONAL",
            received_at=received_at,
            flags={
                "source": "personal-relay",
                "device_id": device_id,
                "message_id": message_id,
            },
        )
        await self._registry.communications.upsert_conversation(
            person_id=person.id, channel=channel, last_message_at=received_at
        )

    async def _resolve_person(self, conn: ConnectionRow, channel: str, peer: str, peer_name: Any):
        """Create/resolve a person for a relay peer (durable backstop)."""
        handle = (peer or "").strip() or "unknown"
        key = f"{channel}:{handle}"
        # Re-read the row: the ``conn`` captured at the start of an ingest
        # batch is stale — an earlier message in the same batch may already
        # have registered this peer under a person id.
        fresh = await self._registry.connections.get_connection(conn.id) or conn
        import_progress = fresh.import_progress or {}
        person_map: dict[str, str] = dict(import_progress.get("relay_persons", {}))
        existing_id = person_map.get(key)
        if existing_id:
            existing = await self._registry.communications.get_person(existing_id)
            if existing is not None:
                return existing
        display = str(peer_name or "").strip() or _display_from_handle(handle)
        person = await self._registry.communications.create_person(display_name=display)
        await self._registry.communications.upsert_person_identity(
            person_id=person.id, channel=channel, handle=handle, resolved=bool(peer_name)
        )
        person_map[key] = person.id
        new_progress = dict(fresh.import_progress or {})
        new_progress["relay_persons"] = person_map
        await self._registry.connections.update_connection(conn.id, import_progress=new_progress)
        return person

    async def relay_health(self) -> list[dict[str, Any]]:
        """GET /api/relay/health — per-device auth/operational + backlog counters."""
        rows = await self._registry.connections.list_connections()
        out: list[dict[str, Any]] = []
        for row in rows:
            if row.kind != "relay_device":
                continue
            config = row.config or {}
            out.append(
                {
                    "device_id": config.get("device_id"),
                    "device_name": config.get("device_name"),
                    "connection_id": row.id,
                    "auth_status": row.auth_status,
                    "operational_status": row.operational_status,
                    "state": _public(row)["state"],
                    "last_seen_at": _utc_iso(config.get("last_seen_at")),
                    "last_message_at": _utc_iso(config.get("last_message_at")),
                    "pending_backlog": int(config.get("pending_backlog", 0) or 0),
                }
            )
        return out

    async def relay_import_request(self, *, device_id: str, consent: bool) -> dict[str, Any]:
        """POST /api/relay/import-request — initial history import (consent required)."""
        if consent is not True:
            raise ConnectionControlPlaneError(
                ERR_CONSENT_REQUIRED, "initial history import requires explicit consent"
            )
        conn = await self._find_relay_connection(device_id)
        if conn is None:
            raise ConnectionControlPlaneError(
                ERR_CONNECTION_NOT_FOUND, f"relay device {device_id!r} is unknown"
            )
        job = await self._registry.imports.create_import_job(conn.id, "history", state="pending")
        await self._registry.imports.update_import_job_progress(job.id, progress={"consent": True})
        # Remember the job id so diagnostics can surface it.
        import_progress = dict(conn.import_progress or {})
        import_progress["last_job_id"] = job.id
        await self._registry.connections.update_connection(conn.id, import_progress=import_progress)
        return {"ok": True, "import_job_id": job.id, "state": "pending", "consent": True}


__all__ = [
    "AUTH_STATUSES",
    "CONNECTION_KINDS",
    "ConnectionControlPlane",
    "ConnectionControlPlaneError",
    "DIAGNOSTICS_FIXES",
    "IngestTrigger",
    "OPERATIONAL_STATUSES",
    "ProbeResultTyped",
    "ProbeSeam",
    "TokenExchanger",
    "derive_state",
    "make_imap_probe",
]
