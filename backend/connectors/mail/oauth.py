"""OAuth2 onboarding helper for mail connectors (prompt2 §47).

NerveCenter stays secret-free (§47): the *user* runs the OAuth dance against
their provider (Google Workspace / Microsoft), and the resulting token is kept
by the user in their OS keychain — the backend only ever sees an opaque
``token_ref``. This module is the onboarding glue that makes that flow
deterministic and testable:

- :class:`OauthProvider` — per-provider endpoints + required scope.
- :func:`build_authorization_url` — the consent URL the user opens.
- :func:`parse_callback` — turn the redirect query back into a token grant.
- :func:`make_token_ref` / :func:`validate_token_ref` — the opaque pointer
  NerveCenter stores instead of the secret.

Nothing here performs a network call; the token exchange is documented so the
relay (which *does* hold the client secret) can complete it. The backend's job
is to hand the user a correct URL and a safe reference to store.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode

#: Which provider a connector speaks.
OauthProviderName = Literal["google", "microsoft"]


@dataclass(frozen=True)
class OauthProvider:
    """Endpoint + scope for one mail OAuth provider."""

    name: OauthProviderName
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    default_client_id: str = ""
    scopes_note: str = "IMAP read only"


#: Provider presets. Google and Microsoft both expose IMAP for mail; the
#: scopes below are the *minimal* read-only set that lets the relay tail
#: INBOX (``IMAP.AccessAsUser.All`` for Microsoft). Google additionally
#: grants the Gmail API ``readonly`` scope because the ingest plane polls
#: the history resource, not just IMAP.
PROVIDERS: dict[OauthProviderName, OauthProvider] = {
    "google": OauthProvider(
        name="google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=(
            "https://mail.google.com/imap",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/userinfo.email",
        ),
        scopes_note="Google Workspace IMAP + Gmail API read only",
    ),
    "microsoft": OauthProvider(
        name="microsoft",
        authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        scopes=(
            "https://outlook.office365.com/IMAP.AccessAsUser.All",
            "openid",
            "email",
        ),
        scopes_note="Microsoft 365 IMAP read (IMAP.AccessAsUser.All)",
    ),
}


def provider(name: OauthProviderName) -> OauthProvider:
    """Look up a provider preset. Raises ``ValueError`` for unknown names —
    the connector screen should not silently guess an endpoint."""
    try:
        return PROVIDERS[name]
    except KeyError as exc:
        raise ValueError(f"unknown OAuth provider: {name!r}") from exc


def s256_challenge(verifier: str) -> str:
    """RFC 7636 §4.1: base64url(SHA-256(verifier)) without padding.

    ``verifier`` must be the PKCE ``code_verifier`` (a high-entropy random
    string); the digest is the ``code_challenge`` sent to the provider.
    """
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64_url(digest)


def base64_url(data: bytes) -> str:
    """base64url without padding (RFC 7636 §4.2)."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_pkce_pair() -> tuple[str, str]:
    """A fresh ``code_verifier`` / ``code_challenge`` pair (RFC 7636)."""
    verifier = base64_url(secrets.token_bytes(32))
    return verifier, s256_challenge(verifier)


def build_authorization_url(
    name: OauthProviderName,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str | None = None,
) -> str:
    """The consent URL the user opens in a browser to grant IMAP access.

    ``state`` must be a per-flow random nonce the caller verifies on the way
    back (CSRF guard, §47). When ``code_challenge`` is supplied the URL uses
    the PKCE (S256) authorization-code flow (P1-106): the caller keeps the
    matching ``code_verifier`` and hands it to the token exchange.
    """
    p = provider(name)
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(p.scopes),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    if code_challenge:
        query["code_challenge"] = code_challenge
        query["code_challenge_method"] = "S256"
    return f"{p.authorize_url}?{urlencode(query)}"


@dataclass(frozen=True)
class OauthCallback:
    """Parsed result of the provider's redirect back to ``redirect_uri``."""

    code: str
    state: str
    error: str | None = None


def parse_callback(params: dict[str, str]) -> OauthCallback:
    """Turn the redirect query into an :class:`OauthCallback`.

    ``error`` responses are surfaced (not swallowed) so the UI can show the
    provider's reason; a successful grant carries ``code`` + ``state``.
    """
    code = params.get("code", "")
    state = params.get("state", "")
    error = params.get("error")
    return OauthCallback(code=code, state=state, error=error)


# -- token reference (the only thing NerveCenter persists) ---------------------

#: A token ref names *where* the secret lives, not the secret.
TOKEN_REF_PREFIXES = ("keychain:", "vault:", "env:")


def make_token_ref(store: Literal["keychain", "vault", "env"], item_id: str) -> str:
    """Build the opaque reference NerveCenter stores for a connector's
    credential. The secret itself is never accepted here."""
    prefix = f"{store}:"
    if not item_id or not item_id.strip():
        raise ValueError("token ref item id must be non-empty")
    return f"{prefix}{item_id.strip()}"


def validate_token_ref(ref: str) -> bool:
    """True if ``ref`` looks like a stored reference (not a raw secret).

    A raw token (long, unstructured, no known prefix) is rejected so the
    registry can never accidentally persist the secret itself (§47).
    """
    if not ref:
        return False
    if ref.startswith(TOKEN_REF_PREFIXES):
        return len(ref) > len(TOKEN_REF_PREFIXES[0])
    # Reject anything that looks like a raw credential blob.
    if len(ref) >= 24 and not ref.startswith(" "):
        return False
    return True


def exchange_request(
    name: OauthProviderName,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> dict[str, str]:
    """The form body the *relay* posts to the provider token endpoint to trade
    ``code`` for an access token. Documented here so the backend and the relay
    agree on the wire shape; the backend does not perform the request.
    """
    p = provider(name)
    return {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }
