"""Internal service credentials for in-cluster callers (NS-ADR-122).

``NERVE_INTERNAL_SERVICE_TOKENS`` is ``name:token[,name:token]``. Tokens are
held only as SHA-256 digests and compared in constant time against every entry
(no early exit). A service principal is never a person: it passes only the
routes in :data:`backend.api.public_routes.SERVICE_ROUTE_SCOPES`. No token
value is ever logged or returned.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

import structlog

from backend.api.public_routes import SERVICE_ROUTE_SCOPES, service_may_call

log = structlog.get_logger(__name__)

TOKENS_ENV = "NERVE_INTERNAL_SERVICE_TOKENS"
MIN_TOKEN_CHARS = 32
SERVICE_SOURCE = "service"

_cache: tuple[str, tuple[tuple[str, bytes], ...]] = ("", ())


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _load() -> tuple[tuple[str, bytes], ...]:
    global _cache
    raw = os.environ.get(TOKENS_ENV, "")
    if raw == _cache[0]:
        return _cache[1]
    entries: list[tuple[str, bytes]] = []
    for part in raw.split(","):
        name, sep, token = part.strip().partition(":")
        name, token = name.strip(), token.strip()
        if not sep or name not in SERVICE_ROUTE_SCOPES:
            if part.strip():
                log.warning("service_auth.entry_ignored", reason="unknown service or bad format")
            continue
        if len(token) < MIN_TOKEN_CHARS or "$(" in token:
            log.warning("service_auth.entry_ignored", service=name, reason="token too short")
            continue
        entries.append((name, _digest(token)))
    _cache = (raw, tuple(entries))
    return _cache[1]


def identify_service(token: str) -> str | None:
    """Return the service name the token belongs to, or ``None``."""
    digest = _digest(token)
    found: str | None = None
    for name, stored in _load():
        if hmac.compare_digest(digest, stored) and found is None:
            found = name
    return found


def service_claims(name: str) -> dict[str, Any]:
    """Claims for a service principal. ``typ`` is not ``access``: never a person."""
    return {
        "sub": f"service:{name}",
        "typ": "service",
        "roles": ["service"],
        "nc_token_source": SERVICE_SOURCE,
        "service": name,
    }


def authorize_service(name: str, method: str, path: str) -> bool:
    """True when the service may call ``method`` on the concrete request ``path``."""
    return service_may_call(name, method, path)
