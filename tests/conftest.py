"""Suite-wide fixtures.

The default-deny gate (``backend.api.auth.require_person``, NS-ADR-122) refuses
every non-allowlisted route without a signed-in person. Most tests exercise
route behaviour, not the gate, so an autouse fixture replaces the gate's
authenticator with a fake person. Production code has no bypass flag or env
var. Tests marked ``route_auth`` keep the real gate; route-level
``current_access`` checks always stay real.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _fake_gate_person(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("route_auth") is not None:
        return
    from backend.api import auth

    async def _fake_person(conn: Any, token: str | None = None, **_: Any) -> dict[str, Any]:
        return {"sub": "test-person", "typ": "access", "roles": ["user"]}

    monkeypatch.setattr(auth, "_authenticate_default", _fake_person)
