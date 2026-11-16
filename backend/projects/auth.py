"""Pluggable token verifiers for the restricted project MCP endpoint (NS-ADR-104).

One interface, :class:`TokenVerifier`. ``(a)`` :class:`BearerTokenVerifier`
checks high-entropy tokens from a secret in constant time. ``(b)``
:class:`OidcVerifier` validates an OAuth/OIDC access token as a resource server
(issuer, audience, signature from the issuer's JWKS); it is off unless
``NERVE_MCP_OIDC_ENABLED`` is set. Neither logs or returns a token value.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)

TOKENS_ENV = "NERVE_MCP_PROJECTS_TOKENS"
#: mcp_token of the owner-created Secret nervecenter-openai-tunnel (NS-ADR-105);
#: the OpenAI tunnel-client sends it, notes it writes are tagged "openai".
TUNNEL_TOKEN_ENV = "NERVE_MCP_TUNNEL_TOKEN"
TUNNEL_CLIENT = "openai"
MIN_TOKEN_CHARS = 32
_CLIENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class Principal:
    """Who called. ``client`` tags the notes the caller writes (author)."""

    client: str
    verifier: str


class TokenVerifier(Protocol):
    name: str

    async def verify(self, token: str) -> Principal | None:  # pragma: no cover - interface
        ...


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


class BearerTokenVerifier:
    """Static high-entropy tokens, each bound to a client name.

    Tokens are held only as SHA-256 digests and compared with
    ``hmac.compare_digest`` against every entry (no early exit).
    """

    name = "bearer"

    def __init__(self, tokens: dict[str, str]) -> None:
        # tokens: client name -> token
        self._entries: list[tuple[str, bytes]] = []
        for client, token in tokens.items():
            if not _CLIENT_RE.match(client) or len(token) < MIN_TOKEN_CHARS:
                log.warning("mcp.projects.token_rejected_config", client=client)
                continue
            self._entries.append((client, _digest(token)))

    @property
    def configured(self) -> bool:
        return bool(self._entries)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BearerTokenVerifier:
        """Parse ``client:token,client2:token2`` plus the tunnel token; bad entries are skipped."""
        e = env if env is not None else os.environ
        tokens: dict[str, str] = {}
        for item in e.get(TOKENS_ENV, "").split(","):
            client, sep, token = item.strip().partition(":")
            if sep:
                tokens[client.strip()] = token.strip()
        tunnel = (e.get(TUNNEL_TOKEN_ENV) or "").strip()
        if tunnel:
            tokens[TUNNEL_CLIENT] = tunnel
        return cls(tokens)

    async def verify(self, token: str) -> Principal | None:
        presented = _digest(token or "")
        match: str | None = None
        for client, digest in self._entries:
            if hmac.compare_digest(presented, digest) and match is None:
                match = client
        return Principal(match, self.name) if match is not None else None


class OidcVerifier:
    """OAuth/OIDC resource-server validation. Off by default."""

    name = "oidc"

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_uri: str,
        *,
        client: str = "chatgpt",
        algorithms: tuple[str, ...] = ("RS256",),
        fetch_jwks: Callable[[str], dict[str, Any]] | None = None,
        cache_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        self.jwks_uri = jwks_uri
        self.client = client
        self.algorithms = list(algorithms)
        self._fetch = fetch_jwks or self._http_fetch
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._jwks: dict[str, Any] | None = None
        self._fetched_at = 0.0

    @staticmethod
    def _http_fetch(uri: str) -> dict[str, Any]:
        import httpx

        resp = httpx.get(uri, timeout=5.0)
        resp.raise_for_status()
        return resp.json()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> OidcVerifier | None:
        e = env if env is not None else os.environ
        if (e.get("NERVE_MCP_OIDC_ENABLED") or "").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return None
        issuer = (e.get("NERVE_MCP_OIDC_ISSUER") or "").strip().rstrip("/")
        audience = (e.get("NERVE_MCP_OIDC_AUDIENCE") or "").strip()
        if not issuer or not audience:
            log.warning("mcp.projects.oidc_misconfigured")
            return None
        jwks = (e.get("NERVE_MCP_OIDC_JWKS_URI") or "").strip() or (
            issuer + "/protocol/openid-connect/certs"
        )
        client = (e.get("NERVE_MCP_OIDC_CLIENT") or "chatgpt").strip()
        if not _CLIENT_RE.match(client):
            client = "chatgpt"
        return cls(issuer, audience, jwks, client=client)

    async def _keys(self, force: bool = False) -> dict[str, Any]:
        now = self._clock()
        if force or self._jwks is None or now - self._fetched_at > self._cache_seconds:
            self._jwks = await asyncio.to_thread(self._fetch, self.jwks_uri)
            self._fetched_at = now
        return self._jwks

    async def verify(self, token: str) -> Principal | None:
        try:
            import jwt
        except ImportError:  # PyJWT missing: the verifier cannot say yes
            return None
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            jwks = await self._keys()
            if not any(k.get("kid") == kid for k in jwks.get("keys", [])):
                jwks = await self._keys(force=True)  # key rotation
            jwk = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
            if jwk is None:
                return None
            key = jwt.PyJWK(jwk).key
            jwt.decode(
                token,
                key,
                algorithms=self.algorithms,
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except Exception:  # noqa: BLE001 - any failure is a plain "no"
            return None
        return Principal(self.client, self.name)


def build_verifiers(env: dict[str, str] | None = None) -> list[TokenVerifier]:
    verifiers: list[TokenVerifier] = []
    bearer = BearerTokenVerifier.from_env(env)
    if bearer.configured:
        verifiers.append(bearer)
    oidc = OidcVerifier.from_env(env)
    if oidc is not None:
        verifiers.append(oidc)
    return verifiers
