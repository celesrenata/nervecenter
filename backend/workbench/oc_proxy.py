"""opencode web UI on its own host (opencode.nervecenter.celestium.life).

The opencode web app is root-mounted (``/_assets``, ``/sw.js``, root API
paths), so it gets a dedicated host instead of a path under the main site.

Request path: Traefik -> a dedicated oauth2-proxy (Keycloak SSO, host-only
cookie, ``--pass-authorization-header``) -> the api pod, where
:class:`HostDispatch` sends requests for the opencode host to this app before
any NerveCenter route runs. This app then:

* verifies the Keycloak ID token in ``Authorization`` itself (signature via
  JWKS, issuer, audience, expiry), so identity headers are never trusted and a
  caller that bypasses oauth2-proxy gets 401;
* refuses (403) any ``directory`` (query, ``x-opencode-directory`` header, or
  top-level JSON body field) outside ``/projects/<slug>`` of a registered,
  non-archived opencode Project. This is a best-effort limit: the opencode UI
  also has a terminal and global views that this proxy cannot scope;
* replaces ``Authorization`` with the opencode Basic credential, which never
  reaches the browser, strips cookies, and streams responses (SSE included);
* for WebSockets, requires ``Origin`` to be the opencode host.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import posixpath
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any
from urllib.parse import unquote

import httpx
import structlog
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect

log = structlog.get_logger(__name__)

HOST_ENV = "WORKBENCH_OPENCODE_WEB_HOST"
PROJECTS_ROOT = "/projects"
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}
DROP_REQUEST = HOP_BY_HOP | {
    "authorization", "cookie", "x-forwarded-user", "x-forwarded-email",
    "x-forwarded-preferred-username", "x-forwarded-groups", "x-auth-request-user",
    "x-auth-request-email", "x-forwarded-access-token", "gap-auth",
}
DROP_RESPONSE = HOP_BY_HOP | {"set-cookie", "www-authenticate"}
MAX_BODY_INSPECT = 1_048_576

TokenVerifier = Callable[[str], Awaitable[dict[str, Any] | None]]
AllowedSlugs = Callable[[], Awaitable[set[str]]]


# -- pure helpers --------------------------------------------------------------
def directory_allowed(directory: str, slugs: Iterable[str]) -> bool:
    """True when ``directory`` is inside ``/projects/<slug>`` for an allowed slug."""
    if not directory:
        return True
    d = unquote(directory)
    if "\x00" in d or not d.startswith("/"):
        return False
    norm = posixpath.normpath(d)
    allowed = set(slugs)
    parts = norm.split("/")
    return len(parts) >= 3 and parts[1] == PROJECTS_ROOT.strip("/") and parts[2] in allowed


def requested_directories(
    query: Mapping[str, str] | Iterable[tuple[str, str]],
    headers: Mapping[str, str],
    body: bytes | None,
) -> list[str]:
    items = query.items() if isinstance(query, Mapping) else query
    out = [v for k, v in items if k.lower() == "directory"]
    h = headers.get("x-opencode-directory")
    if h:
        out.append(h)
    if body:
        try:
            doc = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            doc = None
        if isinstance(doc, dict):
            for key in ("directory", "worktree"):
                v = doc.get(key)
                if isinstance(v, str):
                    out.append(v)
    return out


def basic_auth(password: str) -> str:
    return "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()


def bearer(headers: Mapping[str, str]) -> str | None:
    scheme, _, value = (headers.get("authorization") or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


# -- token verification ----------------------------------------------------------
def make_keycloak_verifier(issuer: str, client_id: str) -> TokenVerifier:
    """Verify a Keycloak ID token (RS256 via JWKS, iss, aud, exp)."""
    import jwt

    from backend.api.endpoints.mail_security import _eligible_for_server_scopes

    jwks = jwt.PyJWKClient(f"{issuer.rstrip('/')}/protocol/openid-connect/certs", cache_keys=True)

    def _verify(token: str) -> dict[str, Any] | None:
        try:
            key = jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256", "RS384", "RS512"],
                issuer=issuer,
                audience=client_id,
                options={"require": ["exp", "iss", "aud"]},
            )
        except Exception:  # noqa: BLE001 - any failure is "no session"
            return None
        claims.setdefault("sso", True)
        return claims if _eligible_for_server_scopes(claims) else None

    async def verify(token: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(_verify, token)

    return verify


def make_slug_provider(get_engine: Callable[[], Any], ttl: float = 30.0) -> AllowedSlugs:
    cache: dict[str, Any] = {"at": 0.0, "slugs": set()}

    async def slugs() -> set[str]:
        now = time.monotonic()
        if now - cache["at"] < ttl:
            return cache["slugs"]
        engine = get_engine()
        if engine is None:
            return set()
        from sqlalchemy import text

        async with engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT slug FROM workbench_projects WHERE kind = 'opencode' AND NOT archived")
            )
            cache["slugs"] = {r[0] for r in rows}
        cache["at"] = now
        return cache["slugs"]

    return slugs


# -- app --------------------------------------------------------------------------
def create_oc_app(
    *,
    host: str,
    upstream: str,
    password: str,
    verify_token: TokenVerifier,
    allowed_slugs: AllowedSlugs,
    http_client: httpx.AsyncClient | None = None,
) -> Starlette:
    upstream = upstream.rstrip("/")
    client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30, read=None))
    auth = basic_auth(password)

    async def authenticate(headers: Mapping[str, str]) -> dict[str, Any] | None:
        tok = bearer(headers)
        return await verify_token(tok) if tok else None

    async def http_proxy(request: Request) -> Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        if await authenticate(headers) is None:
            return Response("unauthorized", status_code=401)
        body = await request.body()
        dirs = requested_directories(
            request.query_params.multi_items(),
            headers,
            body if len(body) <= MAX_BODY_INSPECT else None,
        )
        slugs = await allowed_slugs()
        bad = [d for d in dirs if not directory_allowed(d, slugs)]
        if bad:
            log.info("workbench.oc_proxy.directory_refused", path=request.url.path)
            return Response(
                json.dumps({"error": "directory_not_permitted",
                            "detail": "Only registered Workbench opencode projects."}),
                status_code=403,
                media_type="application/json",
            )
        fwd = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST}
        fwd["authorization"] = auth
        url = upstream + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        req = client.build_request(request.method, url, headers=fwd, content=body)
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError:
            return Response("opencode unavailable", status_code=502)
        out_headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in DROP_RESPONSE]

        async def stream():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()

        r = StreamingResponse(stream(), status_code=resp.status_code)
        r.raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in out_headers]
        return r

    async def ws_proxy(websocket: WebSocket) -> None:
        headers = {k.lower(): v for k, v in websocket.headers.items()}
        if headers.get("origin") != f"https://{host}":
            await websocket.send_denial_response(Response(status_code=403))
            return
        if await authenticate(headers) is None:
            await websocket.send_denial_response(Response(status_code=401))
            return
        dirs = requested_directories(websocket.query_params.multi_items(), headers, None)
        slugs = await allowed_slugs()
        if any(not directory_allowed(d, slugs) for d in dirs):
            await websocket.send_denial_response(Response(status_code=403))
            return
        target = upstream.replace("http", "ws", 1) + websocket.url.path
        if websocket.url.query:
            target += f"?{websocket.url.query}"
        try:
            up = await websockets.connect(
                target, additional_headers={"Authorization": auth}, max_size=None
            )
        except Exception:  # noqa: BLE001
            await websocket.send_denial_response(Response(status_code=502))
            return
        await websocket.accept(subprotocol=up.subprotocol)

        async def c2u() -> None:
            try:
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    data = msg.get("text") if msg.get("text") is not None else msg.get("bytes")
                    if data is not None:
                        await up.send(data)
            except (WebSocketDisconnect, websockets.ConnectionClosed):
                pass

        async def u2c() -> None:
            try:
                async for data in up:
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(data)
            except (WebSocketDisconnect, websockets.ConnectionClosed):
                pass

        tasks = [asyncio.create_task(c2u()), asyncio.create_task(u2c())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await up.close()
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(
        routes=[
            WebSocketRoute("/{path:path}", ws_proxy),
            Route("/{path:path}", http_proxy, methods=methods),
        ]
    )


class HostDispatch:
    """ASGI wrapper: requests whose Host is ``host`` go to ``other``."""

    def __init__(self, app: ASGIApp, host: str, other: Callable[[], ASGIApp | None]) -> None:
        self.app = app
        self.host = host.lower()
        self.other = other

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            raw = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1")
            if raw.split(":")[0].lower() == self.host:
                target = self.other()
                if target is None:
                    await Response("opencode web is not configured", status_code=503)(
                        scope, receive, send
                    )
                    return
                await target(scope, receive, send)
                return
        await self.app(scope, receive, send)


def configured_host(env: Mapping[str, str] | None = None) -> str:
    return ((env if env is not None else os.environ).get(HOST_ENV) or "").strip().lower()
