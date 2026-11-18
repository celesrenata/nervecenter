"""IDE reverse proxy: ``/ide/<slug>/`` over HTTP and WebSocket to the project's pod only.

Gates (Requirements 6.7, 6.8): a WebSocket upgrade needs a valid SSO session
(401) and an ``Origin`` matching the Workbench's own host (403) before any
upstream connection is attempted. SSO credentials are never forwarded to the pod.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
import websockets
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import Response, StreamingResponse
from starlette.websockets import WebSocketDisconnect

from backend.workbench.ide import CODE_SERVER_PORT, AmbiguousSlug, IdeManager

TRAFFIC_INTERVAL_S = 30.0

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
# Never forwarded to the pod (SSO credentials / proxy identity).
SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "set-cookie", "x-csrf-token"})
_WS_HANDSHAKE = frozenset(
    {
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-accept",
    }
)

SessionValidator = Callable[[Mapping[str, str]], Awaitable[bool]]


# -- pure helpers ------------------------------------------------------------


def origin_host(origin: str | None) -> str | None:
    if not origin:
        return None
    try:
        host = urlsplit(origin).netloc
    except ValueError:
        return None
    return host.lower() or None


def check_ws_gate(
    session_ok: bool, origin: str | None, allowed_origins: Iterable[str]
) -> int | None:
    """Return 401/403 when the upgrade must be refused, else None.

    ``allowed_origins`` holds hosts (``host[:port]``) or full origins.
    """
    if not session_ok:
        return 401
    host = origin_host(origin)
    if host is None:
        return 403
    allowed: set[str] = set()
    for a in allowed_origins:
        a = (a or "").strip().lower()
        if not a:
            continue
        allowed.add(origin_host(a) or a if "://" in a else a)
    return None if host in allowed else 403


def should_record_traffic(
    last_recorded: float | None, now: float, interval: float = TRAFFIC_INTERVAL_S
) -> bool:
    """True when no update was recorded yet or ``interval`` seconds have elapsed."""
    return last_recorded is None or (now - last_recorded) >= interval


def filter_request_headers(
    headers: Iterable[tuple[str, str]], *, websocket: bool = False
) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers:
        lk = k.lower()
        if lk in HOP_BY_HOP or lk in SENSITIVE_HEADERS or lk in ("host", "content-length"):
            continue
        if websocket and (lk in _WS_HANDSHAKE or lk == "origin" or lk.startswith("sec-websocket")):
            continue
        # code-server checks Origin against X-Forwarded-Host on upgrades; the
        # upstream Origin is the pod address, so the public host must not leak.
        if websocket and lk == "x-forwarded-host":
            continue
        out[k] = v
    return out


def filter_response_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    return [
        (k, v)
        for k, v in headers
        if k.lower() not in HOP_BY_HOP
        and k.lower() not in ("set-cookie", "content-length", "content-encoding")
    ]


def allowed_origins_for(configured: Iterable[str], headers: Mapping[str, str]) -> list[str]:
    """Configured origins plus the request's own Host / X-Forwarded-Host."""
    out = list(configured)
    for name in ("x-forwarded-host", "host"):
        v = headers.get(name)
        if v:
            out.append(v.split(",")[0].strip())
    return out


# -- router ------------------------------------------------------------------


def create_ide_router(
    manager: IdeManager,
    validate_session: SessionValidator,
    *,
    validate_ticket: Callable[[Mapping[str, str], str], bool] | None = None,
    allowed_origins: Iterable[str] = (),
    http_client: httpx.AsyncClient | None = None,
    clock: Callable[[], float] = time.monotonic,
    prefix: str = "/ide",
) -> APIRouter:
    router = APIRouter()
    configured = list(allowed_origins)
    last_traffic: dict[str, float] = {}
    client = http_client or httpx.AsyncClient(timeout=None, follow_redirects=False)

    async def upstream_ip(slug: str) -> str | None:
        try:
            inst = await manager.get(slug)
        except AmbiguousSlug:
            return None
        return inst.pod_ip if inst and inst.pod_ip else None

    async def authorized(headers: Mapping[str, str], slug: str) -> bool:
        if await validate_session(headers):
            return True
        return bool(validate_ticket and validate_ticket(headers, slug))

    async def touch(slug: str) -> None:
        now = clock()
        if should_record_traffic(last_traffic.get(slug), now):
            last_traffic[slug] = now
            try:
                await manager.record_traffic(slug)
            except Exception:  # noqa: BLE001
                pass

    @router.websocket(prefix + "/{slug}/{path:path}")
    async def ide_ws(websocket: WebSocket, slug: str, path: str = "") -> None:
        headers = {k.lower(): v for k, v in websocket.headers.items()}
        session_ok = await authorized(headers, slug)
        refusal = check_ws_gate(
            session_ok, headers.get("origin"), allowed_origins_for(configured, headers)
        )
        if refusal is not None:
            # Refuse the handshake with an HTTP status; no upstream connection.
            await websocket.send_denial_response(Response(status_code=refusal))
            return
        ip = await upstream_ip(slug)
        if ip is None:
            await websocket.send_denial_response(Response(status_code=502))
            return
        query = websocket.url.query
        url = f"ws://{ip}:{CODE_SERVER_PORT}/{path}" + (f"?{query}" if query else "")
        subprotocols = [
            p.strip() for p in headers.get("sec-websocket-protocol", "").split(",") if p.strip()
        ]
        try:
            upstream = await websockets.connect(
                url,
                subprotocols=subprotocols or None,
                additional_headers=filter_request_headers(headers.items(), websocket=True),
                max_size=None,
                origin=f"http://{ip}:{CODE_SERVER_PORT}",
            )
        except Exception:  # noqa: BLE001
            await websocket.send_denial_response(Response(status_code=502))
            return
        await websocket.accept(subprotocol=upstream.subprotocol)
        await touch(slug)

        async def client_to_upstream() -> None:
            try:
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    data = msg.get("text") if msg.get("text") is not None else msg.get("bytes")
                    if data is not None:
                        await upstream.send(data)
                        await touch(slug)
            except (WebSocketDisconnect, websockets.ConnectionClosed):
                pass

        async def upstream_to_client() -> None:
            try:
                async for data in upstream:
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(data)
                    await touch(slug)
            except (WebSocketDisconnect, websockets.ConnectionClosed):
                pass

        tasks = [
            asyncio.create_task(client_to_upstream()),
            asyncio.create_task(upstream_to_client()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await upstream.close()
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    @router.api_route(
        prefix + "/{slug}/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def ide_http(request: Request, slug: str, path: str = "") -> Any:
        if not await authorized({k.lower(): v for k, v in request.headers.items()}, slug):
            return Response(status_code=401)
        ip = await upstream_ip(slug)
        if ip is None:
            return Response(status_code=502)
        url = f"http://{ip}:{CODE_SERVER_PORT}/{path}"
        try:
            req = client.build_request(
                request.method,
                url,
                params=request.query_params,
                headers=filter_request_headers(request.headers.items()),
                content=await request.body(),
            )
            resp = await client.send(req, stream=True)
        except httpx.HTTPError:
            return Response(status_code=502)
        await touch(slug)

        async def body() -> Any:
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()

        out = StreamingResponse(body(), status_code=resp.status_code)
        for k, v in filter_response_headers(resp.headers.multi_items()):
            out.headers.append(k, v)
        if resp.headers.get("content-encoding"):
            out.headers["content-encoding"] = resp.headers["content-encoding"]
        return out

    return router
