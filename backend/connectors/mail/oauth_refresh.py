"""Durable OAuth token refresh for mail connectors (Personal-Ops Recovery §4).

The onboarding code exchange (prompt2 §47) is performed by the relay; once
the backend holds a vault-backed refresh token it must refresh *itself* — and
providers rotate refresh tokens on use. This module owns that refresh:

- :func:`exchange_refresh_token` — the pure wire exchange (``POST
  grant_type=refresh_token`` to the provider token endpoint), following the
  calendar provider pattern (:mod:`backend.calendar.providers`): injectable
  transport, zero sockets in tests, stable machine-readable error codes.
- :class:`MailOAuthRefresher` — the durable wrapper (GAP 1): it loads the
  stored ``refresh_token`` secret from the vault-backed connections seam,
  performs the exchange, and writes the **rotated** ``refresh_token`` *and*
  the fresh ``access_token`` back through the existing ``upsert_secret``
  path, so a restarted worker never reuses a stale token.

Secret hygiene (§4: workers never log/emit plaintext credentials):

- token values never appear in raised messages — failures carry stable
  codes plus the provider status / error code only;
- persistence goes through the vault codec (``upsert_secret`` encrypts);
  nothing credential-shaped is written to the non-secret ``config``.

Wire transport: the default endpoint transport is a thin :mod:`httpx`
adapter (one async request per refresh — bounded, no pooling); tests and
callers that need determinism inject their own
``TokenEndpointTransport``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .oauth import OauthProviderName, provider

#: Durable secret kinds (same values as
#: ``backend.connectors.registry.REFRESH_TOKEN`` / ``ACCESS_TOKEN``; defined
#: locally to keep the mail subpackage importable without the control plane).
REFRESH_TOKEN_KIND = "refresh_token"
ACCESS_TOKEN_KIND = "access_token"

#: Stable error codes (machine-readable; never free-form text).
ERR_NO_REFRESH_TOKEN = "oauth_refresh_token_missing"
ERR_REFRESH_DENIED = "oauth_refresh_denied"
ERR_BAD_TOKEN_RESPONSE = "oauth_bad_token_response"
ERR_TOKEN_UNREACHABLE = "oauth_token_unreachable"


class MailOAuthRefreshError(Exception):
    """Refresh failed. Carries a stable ``code`` for durable error mapping;
    the message never contains a token value (only status / provider
    error code)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RefreshedTokens:
    """The token pair a refresh exchange produced.

    ``refresh_token`` is the *rotated* value: providers that do not rotate
    omit the field, in which case the original token stays valid and is
    returned unchanged (and re-persisted, so the durable row stays current).
    ``expires_in`` is the provider's access-token lifetime in seconds.
    """

    access_token: str
    refresh_token: str
    expires_in: float | None = None


# -- token-endpoint transport (the only seam a refresh talks to) -------------


@runtime_checkable
class TokenResponse(Protocol):
    """Duck-typed token-endpoint response (``status_code`` + ``json()`` —
    the calendar plane's :class:`HttpResponse` satisfies it)."""

    status_code: int

    def json(self) -> Any:
        """Parse the response body as JSON."""
        ...


@runtime_checkable
class TokenEndpointTransport(Protocol):
    """The single seam the refresh talks to (the calendar plane's
    :class:`HttpTransport` satisfies it); tests inject a fake."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> TokenResponse:
        """One HTTP request; returns a :class:`TokenResponse`."""
        ...


# -- the pure wire exchange ---------------------------------------------------


async def exchange_refresh_token(
    *,
    provider_name: OauthProviderName,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    transport: TokenEndpointTransport,
) -> RefreshedTokens:
    """POST ``grant_type=refresh_token`` and return the fresh token pair.

    Mirrors ``GoogleCalendarProvider._ensure_token`` (same body shape, same
    injectable transport). Failures raise :class:`MailOAuthRefreshError`
    with a stable code; token values are never echoed in messages.
    """
    if not refresh_token:
        raise MailOAuthRefreshError(
            ERR_NO_REFRESH_TOKEN, "no refresh token supplied to exchange"
        )
    endpoint = provider(provider_name).token_url
    payload = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    try:
        response = await transport.request(
            "POST",
            endpoint,
            headers={"Content-Type": "application/json"},
            body=payload,
        )
    except MailOAuthRefreshError:
        raise
    except Exception as exc:  # noqa: BLE001 - map every network failure to a stable code
        raise MailOAuthRefreshError(
            ERR_TOKEN_UNREACHABLE,
            f"token endpoint unreachable ({type(exc).__name__})",
        ) from exc

    if response.status_code >= 300:
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("error") or "")
        except Exception:  # noqa: BLE001 - a non-JSON error body is not special
            detail = ""
        if response.status_code in (400, 401, 403):
            # invalid_grant / invalid_client / revoked → the stored
            # credential no longer works; the connection needs re-auth.
            raise MailOAuthRefreshError(
                ERR_REFRESH_DENIED,
                f"token refresh denied (status {response.status_code}"
                + (f", error={detail}" if detail else "")
                + ")",
            )
        raise MailOAuthRefreshError(
            ERR_TOKEN_UNREACHABLE,
            f"token refresh failed (status {response.status_code})",
        )

    try:
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        raise MailOAuthRefreshError(
            ERR_BAD_TOKEN_RESPONSE, "token endpoint returned a non-JSON body"
        ) from exc
    if not isinstance(data, dict):
        raise MailOAuthRefreshError(
            ERR_BAD_TOKEN_RESPONSE, "token endpoint returned an unexpected body"
        )
    access = str(data.get("access_token") or "")
    if not access:
        raise MailOAuthRefreshError(
            ERR_BAD_TOKEN_RESPONSE, "token response has no access_token"
        )
    expires_in: float | None = None
    raw_expires = data.get("expires_in")
    if raw_expires is not None:
        try:
            expires_in = float(raw_expires)
        except (TypeError, ValueError):
            expires_in = None
    # Providers that do not rotate omit ``refresh_token`` — the original
    # stays valid, so the durable store keeps it (never goes stale-by-loss).
    return RefreshedTokens(
        access_token=access,
        refresh_token=str(data.get("refresh_token") or "") or refresh_token,
        expires_in=expires_in,
    )


def stored_refresh_token(stored_secret: str | None) -> str:
    """Extract the refresh token from a durable secret payload.

    Accepts a bare token (the mail-plane convention) or the calendar-plane
    JSON shape ``{"refresh_token": …}`` — both are valid vault contents for
    an OAuth connection. Returns ``""`` when nothing usable is stored.
    """
    if not stored_secret:
        return ""
    text = str(stored_secret).strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return text
        if isinstance(payload, dict):
            value = payload.get("refresh_token")
            return str(value).strip() if value else ""
    return text


# -- the durable wrapper (GAP 1) ----------------------------------------------


@runtime_checkable
class SecretStoreSeam(Protocol):
    """The durable secret seam the refresher persists through (the
    vault-backed :class:`ConnectionsRepository` satisfies it —
    ``upsert_secret`` encrypts via the codec; the durable row stores only
    the opaque payload, never plaintext)."""

    async def get_secret(
        self, *, kind: str, owner_kind: str, owner_id: str
    ) -> str | None:
        """Decrypt and return the secret plaintext, or ``None`` if absent."""
        ...

    async def upsert_secret(
        self, *, kind: str, owner_kind: str, owner_id: str, plaintext: str
    ) -> Any:
        """Encrypt ``plaintext`` via the codec and upsert the secret row."""
        ...


class _HttpxTokenTransport:
    """Default :class:`TokenEndpointTransport` over :mod:`httpx`.

    One short-lived async client per refresh — bounded (``timeout``) and
    connectionless, so a single failure cannot wedge a pool. The response
    is wrapped in :class:`_HttpxTokenResponse` to satisfy the protocol.
    """

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> TokenResponse:
        import httpx  # noqa: PLC0415 - lazy; keeps the mail plane importable without httpx eager

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                content=body,
            )
        return _HttpxTokenResponse(response.status_code, response.text)


class _HttpxTokenResponse:
    """Duck-typed :class:`TokenResponse` over an httpx response text."""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self._text = text

    def json(self) -> Any:
        return json.loads(self._text)


class MailOAuthRefresher:
    """Durable refresh: vault-backed read → exchange → vault-backed write-back.

    The write-back goes through the *existing* connections
    ``upsert_secret`` path (the same seam the mail ingest worker resolves
    credentials from): the rotated ``refresh_token`` replaces the stale one
    under ``kind="refresh_token"``, and the fresh access token lands under
    ``kind="access_token"`` — the kind the worker's
    ``_secret_kind_for`` resolves for OAuth connections. Plaintext never
    touches ``config`` and never appears in logs.

    The token-endpoint transport is injectable (tests use a fake); the
    default is a bounded :mod:`httpx` adapter.
    """

    def __init__(
        self,
        *,
        connections: SecretStoreSeam,
        transport: TokenEndpointTransport | None = None,
    ) -> None:
        self._connections = connections
        self._transport = transport if transport is not None else _HttpxTokenTransport()

    async def refresh(
        self,
        *,
        connection_id: str,
        provider: OauthProviderName,
        client_id: str,
        client_secret: str,
    ) -> RefreshedTokens:
        """Refresh the connection's OAuth tokens durably.

        Raises :class:`MailOAuthRefreshError` (stable ``code``) without
        persisting anything when the exchange fails — a denied refresh must
        not overwrite the stored token, and a network failure must not lose
        the only refresh token we hold.
        """
        stored = await self._connections.get_secret(
            kind=REFRESH_TOKEN_KIND,
            owner_kind="connection",
            owner_id=connection_id,
        )
        tokens = await exchange_refresh_token(
            provider_name=provider,
            refresh_token=stored_refresh_token(stored),
            client_id=client_id,
            client_secret=client_secret,
            transport=self._transport,
        )
        # Durable write-back (GAP 1): persist BEFORE returning success so a
        # crash between exchange and persistence cannot leave a stale token
        # behind. Order is refresh first, then access (the access-token kind
        # is what the worker reads on the next poll).
        await self._connections.upsert_secret(
            kind=REFRESH_TOKEN_KIND,
            owner_kind="connection",
            owner_id=connection_id,
            plaintext=tokens.refresh_token,
        )
        await self._connections.upsert_secret(
            kind=ACCESS_TOKEN_KIND,
            owner_kind="connection",
            owner_id=connection_id,
            plaintext=tokens.access_token,
        )
        return tokens
