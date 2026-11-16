"""Post-deploy default-deny check (NS-ADR-122).

Run inside the api pod by deploy.sh (``kubectl exec -i <pod> -c api -- python -
< check-route-auth.py``) or locally against a started app:

    NERVE_CHECK_BASE=http://127.0.0.1:8000 NERVE_CHECK_LOCAL=1 \
        python deployments/harbor/check-route-auth.py

It reads the route table from the app's own code (the schema route needs a
login), then calls every operation over HTTP with NO credential, an invalid path
id and an empty body. Any operation outside the public allowlist that does not
answer 401/403 is listed and the exit status is 1. It also checks the relay
POST with a bad token (401 from the app), the WebSocket without an auth frame
(close 4401), /docs and /openapi.json (404), and the infra MCP listener (401
without a token). No credential or token value is ever sent or printed.
"""

from __future__ import annotations

import base64
import http.client
import os
import re
import socket
import struct
import sys
import urllib.parse

BASE = os.environ.get("NERVE_CHECK_BASE", "http://127.0.0.1:8000")
INFRA_PORT = int(os.environ.get("NERVE_CHECK_INFRA_PORT", "8001"))
#: Local run without Postgres or the 8001 listener: skip the listener check and
#: accept the relay's "durable storage is not configured" no-op as a refusal.
LOCAL = os.environ.get("NERVE_CHECK_LOCAL") == "1"
METHODS = ("get", "post", "put", "patch", "delete")
TIMEOUT = 10.0

failures: list[str] = []


def fail(message: str) -> None:
    failures.append(message)


def call_full(
    host: str, port: int, method: str, path: str, headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
    try:
        send = dict(headers or {})
        if method in ("POST", "PUT", "PATCH"):
            send.setdefault("Content-Length", str(len(body or b"")))
        conn.request(method, path, body=body, headers=send)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def call(*args: object, **kwargs: object) -> int:
    return call_full(*args, **kwargs)[0]  # type: ignore[arg-type]


def ws_close_code(host: str, port: int, path: str) -> int | None:
    """Open a WebSocket, send nothing, return the close code the server sends."""
    sock = socket.create_connection((host, port), timeout=TIMEOUT)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            data += chunk
        status_line = data.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            return None
        rest = data.split(b"\r\n\r\n", 1)[1]
        sock.settimeout(TIMEOUT)
        while True:
            while len(rest) < 2:
                chunk = sock.recv(4096)
                if not chunk:
                    return None
                rest += chunk
            opcode = rest[0] & 0x0F
            length = rest[1] & 0x7F
            offset = 2
            if length == 126:
                while len(rest) < 4:
                    rest += sock.recv(4096)
                length = struct.unpack(">H", rest[2:4])[0]
                offset = 4
            while len(rest) < offset + length:
                chunk = sock.recv(4096)
                if not chunk:
                    return None
                rest += chunk
            payload = rest[offset : offset + length]
            rest = rest[offset + length :]
            if opcode == 8:
                return struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else None
    except (OSError, TimeoutError):
        return None
    finally:
        sock.close()


def main() -> int:
    sys.path.insert(0, os.getcwd())
    from backend.api.app import create_app
    from backend.api.public_routes import PUBLIC_ROUTES

    app = create_app()
    schema = app.openapi()
    ops = sorted(
        (method.upper(), path)
        for path, item in schema["paths"].items()
        for method in item
        if method in METHODS
    )
    parsed = urllib.parse.urlparse(BASE)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80

    stale = sorted(set(PUBLIC_ROUTES) - set(ops) - {("WS", "/api/events/ws")})
    for method, path in stale:
        fail(f"stale allowlist entry: {method} {path}")

    checked = 0
    for method, path in ops:
        if (method, path) in PUBLIC_ROUTES:
            continue
        checked += 1
        concrete = re.sub(r"\{[^}]+\}", "x", path)
        try:
            status = call(host, port, method, concrete)
        except OSError as exc:
            fail(f"{method} {path}: request failed ({type(exc).__name__})")
            continue
        if status not in (401, 403):
            fail(f"OPEN {method} {path} -> {status}")
    print(f"checked {checked} gated operations over HTTP ({len(PUBLIC_ROUTES)} allowlisted)")

    for path in ("/docs", "/openapi.json"):
        status = call(host, port, "GET", path)
        if status != 404:
            fail(f"{path} answered {status}, expected 404")
    if call(host, port, "GET", "/api/openapi.json") not in (401, 403):
        fail("/api/openapi.json is reachable without a login")

    status, relay_body = call_full(
        host, port, "POST", "/api/relay/messages",
        {"Authorization": "Bearer invalid-check-token", "Content-Type": "application/json"},
        b'{"device_id": "nc-route-check", "messages": []}',
    )
    degraded_noop = LOCAL and status == 200 and b'"executed":false' in relay_body
    if status != 401 and not degraded_noop:
        fail(f"relay POST with a bad token answered {status}, expected 401")

    code = ws_close_code(host, port, "/api/events/ws")
    if code != 4401:
        fail(f"/api/events/ws without an auth frame closed with {code}, expected 4401")

    if not LOCAL:
        try:
            status = call(host, INFRA_PORT, "POST", "/api/mcp/infra", body=b"")
        except OSError as exc:
            fail(f"infra MCP listener on {INFRA_PORT} unreachable ({type(exc).__name__})")
        else:
            if status != 401:
                fail(f"infra MCP listener answered {status} without a token, expected 401")

    if failures:
        print(f"FAILED: {len(failures)} problem(s)")
        for line in failures:
            print(f"  {line}")
        return 1
    print("OK: 0 open routes outside the allowlist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
