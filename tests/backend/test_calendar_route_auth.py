"""Ask calendar-action routes are default-deny and none is public."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.public_routes import PUBLIC_ROUTES

pytestmark = pytest.mark.route_auth

ROUTES = [
    ("POST", "/api/calendar/proposals/{proposal_id}/confirm"),
    ("POST", "/api/calendar/proposals/{proposal_id}/cancel"),
    ("POST", "/api/calendar/events/{event_id}/undo"),
    ("POST", "/api/context/ask/stream"),
]


def test_new_routes_exist_refuse_anonymous_and_are_not_public() -> None:
    app = create_app()
    paths = app.openapi()["paths"]
    with TestClient(app, raise_server_exceptions=False) as client:
        for method, template in ROUTES:
            assert method.lower() in paths[template], template
            assert (method, template) not in PUBLIC_ROUTES
            url = template.replace("{proposal_id}", "x").replace("{event_id}", "x")
            body = {"question": "hi"} if url.endswith("/stream") else {}
            assert client.request(method, url, json=body).status_code in (401, 403), url
