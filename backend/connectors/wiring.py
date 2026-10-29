"""Production wiring for the connection control plane (P1-C / F5).

The control plane (:class:`~backend.connectors.registry.ConnectionControlPlane`)
ships with no probes, no token exchanger and no sync triggers registered —
every ``verify`` call degraded to ``probe_unsupported`` and every ``sync`` to
``ingest_unavailable`` for *every* kind. This module closes that gap at app
construction time (spec ``personal-ops-production-activation`` P1-C: "register
probes/triggers/exchangers at control-plane construction"):

- provider login probes — IMAP reuses the frozen :class:`ImapClient` seam
  (``make_imap_probe``); the durable OAuth kinds (``gmail`` / ``graph_mail``)
  get a real bearer-token login probe over :mod:`httpx` (Google userinfo /
  Microsoft Graph ``/me``). A successful probe authenticates the connection
  but — the frozen rule — never advances the operational axis.
- a durable OAuth **token exchanger** that completes the code → token grant at
  the provider (client credentials from the deploy environment, PKCE verifier
  from the row config) so the onboarding callback stores a real token via the
  vault.
- **sync triggers** for the mail ingest kinds (``gmail`` / ``imap`` /
  ``outlook``) that delegate one bounded ingest cycle to the live
  supervisor. Without a supervisor the triggers are not registered — the
  plane then keeps its honest ``ingest_unavailable`` degradation instead of
  silently pretending a sync happened.

Everything is best-effort and secret-free: a failing registration logs a
warning and leaves the corresponding endpoint in its honest degraded state.
Startup never crashes on this plane.
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from .mail.imap_client import ImapClient, ImapImapClient
from .mail.oauth import PROVIDERS, OauthProviderName
from .registry import (
    CONNECTION_KINDS,
    ERR_MISSING_CREDENTIAL,
    ERR_OAUTH_AUTH_REJECTED,
    ERR_OAUTH_INCOMPLETE,
    OAUTH_PROVIDER_BY_KIND,
    ConnectionControlPlane,
    ConnectionControlPlaneError,
    ProbeResultTyped,
    make_imap_probe,
)

log = structlog.get_logger(__name__)

#: The ingest plane's mail kinds (workstream C) — the kinds that get a sync
#: trigger. ``graph_mail`` deliberately does NOT get one: Microsoft OAuth mail
#: is served by the control plane's OAuth flow, not the IMAP/Gmail transports
#: (P1-106: an unservable provider must never look like a healthy worker).
MAIL_TRIGGER_KINDS = ("gmail", "imap", "outlook")

#: Provider base URLs for the login probes (overridable per connection via
#: ``config`` ``google_api_base`` / ``graph_api_base`` for testability).
_GOOGLE_DEFAULT_BASE = "https://oauth2.googleapis.com"
_GRAPH_DEFAULT_BASE = "https://graph.microsoft.com"

#: Deploy-contract environment variables carrying the OAuth app credentials
#: (non-secret config convention — see :mod:`backend.calendar`). Never read
#: from connection rows: ``client_secret`` is a denied config key.
_CLIENT_ID_ENV = {
    "google": "NERVE_GOOGLE_CLIENT_ID",
    "microsoft": "NERVE_MICROSOFT_CLIENT_ID",
}
_CLIENT_SECRET_ENV = {
    "google": "NERVE_GOOGLE_CLIENT_SECRET",
    "microsoft": "NERVE_MICROSOFT_CLIENT_SECRET",
}


# -- transport seams (lazy httpx; no network in tests) ---------------------------


class _HttpxRequestTransport:
    """Bounded :mod:`httpx` GET for the login probes (default transport)."""

    async def request(
        self, url: str, token: str, *, timeout: float = 10.0
    ) -> tuple[int, dict[str, Any]]:
        import httpx  # noqa: PLC0415 - lazy; keeps the module importable without httpx eager

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001 - non-JSON bodies degrade to {}
                body = {}
        return resp.status_code, body if isinstance(body, dict) else {}


class _HttpxTokenExchangeTransport:
    """Bounded :mod:`httpx` POST for the code→token grant (default transport)."""

    async def request(
        self, url: str, form: dict[str, str], *, timeout: float = 15.0
    ) -> tuple[int, dict[str, Any]]:
        import httpx  # noqa: PLC0415 - lazy

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, data=form)
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = {}
        return resp.status_code, body if isinstance(body, dict) else {}


# -- OAuth login probes ---------------------------------------------------------


def make_oauth_probe(kind: str, *, transport: Any | None = None) -> Any:
    """Build the :class:`ProbeSeam` for one durable OAuth connection kind.

    ``gmail`` → Google ``/oauth2/v3/userinfo``; ``graph_mail`` → Microsoft
    Graph ``/me``. ``200`` → authenticated; ``401``/``403`` →
    :data:`ERR_OAUTH_AUTH_REJECTED`; anything else (network / malformed) →
    :data:`ERR_OAUTH_INCOMPLETE` — a stable code, never free-form text, and
    never a credential fragment (the token itself never appears in the
    error message).
    """
    provider_name = OAUTH_PROVIDER_BY_KIND.get(kind)
    if provider_name is None:
        raise ValueError(f"kind {kind!r} is not an OAuth connection kind")

    if provider_name == "google":
        endpoint, base_key, marker = "/oauth2/v3/userinfo", "google_api_base", "email"
        default_base = _GOOGLE_DEFAULT_BASE
    else:  # microsoft
        endpoint, base_key, marker = "/me", "graph_api_base", "mail"
        default_base = _GRAPH_DEFAULT_BASE

    # Bind the transport (injected or the real httpx one) *before* the closure
    # so ``probe`` is pure — the seam object is stable and inspectable.
    _transport = transport if transport is not None else _HttpxRequestTransport()

    class _OAuthProbeSeam:
        def __init__(self) -> None:
            #: The bound transport (injected, or the default httpx one), kept
            #: on the seam instance so callers/tests can inspect the request
            #: log deterministically.
            self._transport = _transport

        async def probe(
            self, config: dict[str, Any], secret: str | None
        ) -> ProbeResultTyped:
            base = str(config.get(base_key) or "").rstrip("/") or default_base
            url = f"{base}{endpoint}"
            if not secret:
                return ProbeResultTyped(
                    False, ERR_MISSING_CREDENTIAL, "no access token stored", ()
                )
            try:
                status_code, body = await self._transport.request(url, secret)
            except Exception:  # noqa: BLE001 - network failure is a probe failure
                return ProbeResultTyped(
                    False,
                    ERR_OAUTH_INCOMPLETE,
                    "provider login endpoint unreachable",
                    (),
                )
            if status_code == 200 and body.get(marker):
                return ProbeResultTyped(True, None, None, ())
            if status_code in (401, 403):
                return ProbeResultTyped(
                    False,
                    ERR_OAUTH_AUTH_REJECTED,
                    "provider rejected the access token (expired or invalid)",
                    (),
                )
            return ProbeResultTyped(
                False,
                ERR_OAUTH_INCOMPLETE,
                f"provider login probe returned HTTP {status_code}",
                (),
            )

    return _OAuthProbeSeam()


def register_connection_probes(
    plane: ConnectionControlPlane,
    *,
    imap_client: ImapClient | None = None,
) -> None:
    """Register the production login probes for every servable connection kind.

    IMAP uses the frozen :class:`ImapClient` seam (production
    :class:`ImapImapClient` unless a stub is injected); the OAuth kinds get
    the bearer-token probes. A registration failure degrades that kind to its
    honest ``probe_unsupported`` state — never crashes startup.
    """
    for kind in CONNECTION_KINDS:
        if kind not in OAUTH_PROVIDER_BY_KIND and kind != "imap":
            continue
        try:
            if kind == "imap":
                plane.register_probe(kind, make_imap_probe(imap_client or ImapImapClient()))
            else:
                plane.register_probe(kind, make_oauth_probe(kind))
        except Exception as exc:  # noqa: BLE001 - startup must not crash
            log.warning(
                "connectors.probe_registration_failed", kind=kind, error=str(exc)
            )


# -- OAuth token exchanger -------------------------------------------------------


class RegistryTokenExchanger:
    """Durable code→token exchanger bound to the shared repository registry.

    The provider's token endpoint is authoritative (the frozen :mod:`backend
    .connectors.mail.oauth` presets); the PKCE ``code_verifier`` and redirect
    URI are re-read from the *row config* (persisted by
    :meth:`ConnectionControlPlane.start_oauth`) so the exchange is bound to
    the exact onboarding flow. Client credentials come from the deploy
    environment — never from connection config (``client_secret`` is a
    denied config key).

    Failures raise :class:`ConnectionControlPlaneError` with the stable
    ``oauth_incomplete`` code (a provider error body is never echoed — it can
    carry the redirect URI, and a missing credential is a distinct,
    actionable ``missing_credential``).
    """

    def __init__(
        self,
        registry: Any,
        *,
        transport: Any | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._transport = transport
        self._env = env

    async def exchange(self, provider: OauthProviderName, code: str, state: str) -> dict[str, str]:
        if not code:
            raise ConnectionControlPlaneError(
                ERR_OAUTH_INCOMPLETE, "no authorization code to exchange"
            )
        # The row is resolved per provider: the plane already verified the
        # callback state against the persisted per-flow state; the exchanger
        # only re-reads the row's non-secret onboarding config (redirect URI +
        # PKCE verifier).
        row = await self._find_oauth_row(provider)
        config = dict(getattr(row, "config", None) or {})
        env = dict(self._env if self._env is not None else os.environ)
        client_id = str(env.get(_CLIENT_ID_ENV[provider], "") or "").strip()
        client_secret = str(env.get(_CLIENT_SECRET_ENV[provider], "") or "").strip()
        if not client_id or not client_secret:
            raise ConnectionControlPlaneError(
                ERR_MISSING_CREDENTIAL,
                "provider app credentials are not configured on this host",
            )
        redirect_uri = str(config.get("oauth_redirect_uri") or "").strip()
        verifier = str(config.get("oauth_code_verifier") or "").strip()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if verifier:
            form["code_verifier"] = verifier
        provider_preset = PROVIDERS[provider]  # frozen preset
        transport = self._transport
        if transport is None:
            transport = _HttpxTokenExchangeTransport()
        try:
            status_code, body = await transport.request(provider_preset.token_url, form)
        except Exception as exc:  # noqa: BLE001 - network failure → stable code
            raise ConnectionControlPlaneError(
                ERR_OAUTH_INCOMPLETE, "token endpoint unreachable"
            ) from exc
        if status_code != 200 or not body.get("access_token"):
            raise ConnectionControlPlaneError(
                ERR_OAUTH_INCOMPLETE, f"token exchange failed (HTTP {status_code})"
            )
        return {
            "access_token": str(body["access_token"]),
            "refresh_token": str(body.get("refresh_token") or ""),
        }

    async def _find_oauth_row(self, provider: str) -> Any:
        """Locate the OAuth connection row whose onboarding flow is live.

        Identified by the persisted ``oauth_state`` marker in config (set by
        :meth:`ConnectionControlPlane.start_oauth`); a stale callback whose
        state was already consumed finds no row → ``oauth_incomplete``.
        """
        rows = await self._registry.connections.list_connections()
        wanted = {k for k, p in OAUTH_PROVIDER_BY_KIND.items() if p == provider}
        for row in rows:
            if str(getattr(row, "kind", "") or "") not in wanted:
                continue
            if str((getattr(row, "config", None) or {}).get("oauth_state") or ""):
                return row
        raise ConnectionControlPlaneError(
            ERR_OAUTH_INCOMPLETE, "no live onboarding flow for this provider"
        )


def register_token_exchanger(
    plane: ConnectionControlPlane,
    *,
    registry: Any,
    transport: Any | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Bind the durable code→token exchanger (best-effort, never raises).

    ``registry`` is the *same* repository registry the plane was built on
    (frozen P1-104: one codec-bound registry, no second one).
    """
    try:
        plane.set_token_exchanger(
            RegistryTokenExchanger(registry, transport=transport, env=env)
        )
    except Exception as exc:  # noqa: BLE001 - startup must not crash
        log.warning("connectors.exchanger_registration_failed", error=str(exc))


# -- sync triggers ---------------------------------------------------------------


class MailSyncTrigger:
    """Delegate one bounded ingest cycle to the live supervisor (workstream C).

    The trigger holds no state of its own: it re-reads the durable row and
    runs a single :meth:`MailIngestSupervisor.sync_once` cycle, whose success
    / failure mapping (cursor advance, healthy / degraded / error) already
    lives in the worker. ``operational_status=syncing`` is managed by the
    plane around the call.
    """

    def __init__(self, supervisor: Any) -> None:
        self._supervisor = supervisor

    async def start(self, connection_row: Any) -> None:
        await self._supervisor.sync_once(str(getattr(connection_row, "id", "")))


def register_sync_triggers(
    plane: ConnectionControlPlane,
    *,
    supervisor: Any | None,
) -> None:
    """Register sync triggers for the mail ingest kinds.

    ``None`` supervisor (ingest plane not enabled) → *no* triggers: the
    plane then answers ``sync`` with its honest ``ingest_unavailable``
    degradation instead of silently succeeding.
    """
    if supervisor is None:
        return
    for kind in MAIL_TRIGGER_KINDS:
        try:
            plane.register_trigger(kind, MailSyncTrigger(supervisor))
        except Exception as exc:  # noqa: BLE001 - startup must not crash
            log.warning(
                "connectors.trigger_registration_failed", kind=kind, error=str(exc)
            )


# -- app-lifespan entrypoint ------------------------------------------------------


def install_connection_wiring(app: Any) -> None:
    """Register every control-plane seam on ``app.state.connection_control_plane``.

    Called from :func:`backend.api.app._install_connection_control_plane`
    after the plane is bound (after :func:`backend.api.wiring.wire_dependencies`
    has started the ingest supervisor, so the triggers can reference it).
    Never raises — a failure leaves the affected endpoint in its honest
    degraded state.
    """
    plane = getattr(app.state, "connection_control_plane", None)
    if plane is None:
        return
    # The exchanger must bind the *same* codec-bound registry the plane was
    # built on (frozen P1-104: one shared secret codec, no second registry).
    registry = getattr(app.state, "repository_registry", None)
    try:
        register_connection_probes(plane)
    except Exception as exc:  # noqa: BLE001 - startup must not crash
        log.warning("connectors.probe_install_failed", error=str(exc))
    if registry is not None:
        register_token_exchanger(plane, registry=registry)
    register_sync_triggers(plane, supervisor=getattr(app.state, "ingest_supervisor", None))
