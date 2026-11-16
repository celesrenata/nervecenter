"""The single reviewed list of routes that answer without a signed-in person.

Every other route is gated by :func:`backend.api.auth.require_person`
(NS-ADR-122). Adding a route here is a deliberate, reviewed edit: the
permanent test ``tests/backend/test_route_auth_default_deny.py`` pins this set
and fails on a stale entry, and the post-deploy check
``deployments/harbor/check-route-auth.py`` reads it too.

Each entry is ``(METHOD, path template)`` (``WS`` for a WebSocket) and a one
line reason. A public route is either harmless bootstrap, or it authenticates
itself with its own credential (PKCE code, refresh cookie, device token, MCP
bearer, first WebSocket frame).
"""

from __future__ import annotations

import re

#: ``(method, path template)`` -> why the route is public.
PUBLIC_ROUTES: dict[tuple[str, str], str] = {
    # Probes and version (no data beyond status).
    ("GET", "/api/health"): "kubelet probes and the deploy.sh in-pod probe",
    ("GET", "/api/v1/health"): "versioned alias of the health probe",
    ("GET", "/version"): "version information only",
    # Sign-in bootstrap.
    ("GET", "/api/auth/sso/config"): "SSO bootstrap, fetched before any session exists",
    ("POST", "/api/auth/sso/token"): "authenticates with the PKCE code and verifier",
    ("POST", "/api/auth/login"): "break-glass login, authenticates with the local-admin password",
    ("POST", "/api/auth/refresh"): "authenticates with the nc_rt refresh cookie and CSRF header",
    ("POST", "/api/auth/pairing/confirm"): "authenticates with the single-use pairing challenge",
    # Device-token routes (exact SSO-free ingress paths and the relay import request).
    ("POST", "/api/relay/messages"): "device relay token (SSO-free ingress path)",
    ("POST", "/api/relay/contacts"): "device relay token (SSO-free ingress path)",
    ("POST", "/api/relay/import-request"): "device relay token",
    ("POST", "/api/health/ingest"): "device token (SSO-free ingress path)",
    ("POST", "/api/handoffs/device/claim"): "device token (SSO-free ingress path)",
    ("POST", "/api/handoffs/device/result"): "device token (SSO-free ingress path)",
    # Static MCP bearer tokens.
    ("POST", "/api/mcp/projects"): "static MCP bearer tokens (backend/projects/auth.py)",
    # WebSocket: a browser socket cannot send headers.
    ("WS", "/api/events/ws"): "authenticates with the first frame (close 4401 otherwise)",
}

#: Routes where a live paired-device credential is accepted as well as a
#: signed-in person (owner decision A). They stay gated, never anonymous.
PERSON_OR_DEVICE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/approvals/{approval_id}/verify"),
        ("POST", "/api/approvals/{approval_id}/verify"),
        ("POST", "/api/approvals/{approval_id}/decide"),
    }
)


def is_public(method: str, template: str | None) -> bool:
    """True when ``(method, template)`` is on the public allowlist."""
    return template is not None and (method.upper(), template) in PUBLIC_ROUTES


def is_person_or_device(method: str, template: str | None) -> bool:
    return template is not None and (method.upper(), template) in PERSON_OR_DEVICE_ROUTES


#: What each internal service credential may call. Matched against the request
#: path (``{id}`` is one path segment), not the route template, so the scope does
#: not depend on how the api names its path parameters. The ``mcp`` scope is
#: exactly the paths in ``mcp/tools_module.py`` (a test keeps them equal).
SERVICE_ROUTE_SCOPES: dict[str, frozenset[tuple[str, str]]] = {
    "mcp": frozenset(
        {
            ("GET", "/api/search"),
            ("GET", "/api/entities/{id}"),
            ("GET", "/api/events"),
            ("GET", "/api/changes"),
            ("GET", "/api/goals/{id}"),
            ("POST", "/api/goals"),
            ("POST", "/api/goals/{id}"),
            ("GET", "/api/tasks/{id}"),
            ("POST", "/api/tasks"),
            ("POST", "/api/claims"),
            ("POST", "/api/evidence"),
            ("POST", "/api/context/compile"),
            ("GET", "/api/context"),
            ("GET", "/api/compute"),
            ("GET", "/api/models"),
            ("GET", "/api/routes"),
            ("GET", "/api/incidents/{id}"),
            ("POST", "/api/incidents/investigate"),
            ("POST", "/api/changes"),
            ("GET", "/api/attention"),
            ("GET", "/api/briefing"),
            ("GET", "/api/people/{id}"),
            ("GET", "/api/conversations/{id}"),
            ("GET", "/api/communications"),
            ("GET", "/api/commitments"),
            ("GET", "/api/waiting-on"),
            ("POST", "/api/followups"),
            ("POST", "/api/attention/{id}/snooze"),
            ("POST", "/api/attention/{id}/resolve"),
            ("POST", "/api/drafts"),
            ("POST", "/api/handoffs"),
        }
    ),
}


def _compile(template: str) -> re.Pattern[str]:
    return re.compile("^" + re.escape(template).replace(r"\{id\}", "[^/]+") + "/?$")


_COMPILED: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    name: tuple((method, _compile(path)) for method, path in sorted(scope))
    for name, scope in SERVICE_ROUTE_SCOPES.items()
}


def service_may_call(service: str, method: str, path: str) -> bool:
    """True when ``service`` is scoped to ``method`` on the concrete ``path``."""
    return any(
        m == method.upper() and pattern.match(path) for m, pattern in _COMPILED.get(service, ())
    )
