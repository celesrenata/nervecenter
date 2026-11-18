"""Path-scoped IDE ticket cookie for the Browser_IDE iframe.

The SPA holds its bearer token in memory, and an iframe navigation cannot send
an ``Authorization`` header. After a bearer-authenticated call, the API sets
``nc_ide``: an HttpOnly, Secure, SameSite=Strict cookie whose Path is
``/ide/<slug>/``, carrying an HMAC-signed ``(subject, slug, expiry)``. The IDE
proxy accepts either a bearer token or a valid ticket for that slug. The proxy
strips ``Cookie`` before forwarding, so the pod never sees the ticket.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Mapping
from http.cookies import SimpleCookie

COOKIE_NAME = "nc_ide"
KEY_ENV = "WORKBENCH_IDE_TICKET_KEY"
TTL_S = 8 * 3600

_process_key = secrets.token_bytes(32)


def _key() -> bytes:
    raw = os.environ.get(KEY_ENV, "").strip()
    return raw.encode() if raw else _process_key


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign(payload: str) -> str:
    return _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())


def issue(subject: str, slug: str, now: float | None = None, ttl: int = TTL_S) -> str:
    exp = int((time.time() if now is None else now) + ttl)
    payload = f"v1.{_b64(subject.encode())}.{slug}.{exp}"
    return f"{payload}.{_sign(payload)}"


def verify(token: str, slug: str, now: float | None = None) -> bool:
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != "v1" or parts[2] != slug:
        return False
    payload = ".".join(parts[:4])
    if not hmac.compare_digest(parts[4], _sign(payload)):
        return False
    try:
        exp = int(parts[3])
    except ValueError:
        return False
    return exp > (time.time() if now is None else now)


def cookie_path(slug: str) -> str:
    return f"/ide/{slug}/"


def from_headers(headers: Mapping[str, str], slug: str) -> bool:
    raw = headers.get("cookie") or ""
    try:
        jar = SimpleCookie()
        jar.load(raw)
    except Exception:  # noqa: BLE001 - malformed cookie header means no ticket
        return False
    morsel = jar.get(COOKIE_NAME)
    return bool(morsel and verify(morsel.value, slug))
