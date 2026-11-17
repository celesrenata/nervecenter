"""Every /api/voice route is default-deny and none is public."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.public_routes import PUBLIC_ROUTES

pytestmark = pytest.mark.route_auth


def test_voice_routes_refuse_anonymous_and_are_not_public() -> None:
    app = create_app()
    ops = [
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/api/voice")
        for method in item
        if method in ("get", "post", "put", "patch", "delete")
    ]
    assert len(ops) >= 6
    with TestClient(app, raise_server_exceptions=False) as client:
        for method, path in ops:
            assert (method, path) not in PUBLIC_ROUTES
            response = client.request(method, re.sub(r"\{[^}]+\}", "x", path))
            assert response.status_code in (401, 403), (method, path)
