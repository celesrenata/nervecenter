"""The CLI sends NERVE_TOKEN as a bearer when set (NS-ADR-124)."""

from __future__ import annotations

from typing import Any

import pytest

from cli import client


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake(method, base_url, url, headers, body, timeout):  # noqa: ANN001, ARG001
        seen["headers"] = headers
        return client.Response(status_code=200, data={})

    monkeypatch.setattr(client, "_do_request", fake)
    return seen


def test_bearer_header_sent_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    monkeypatch.setenv("NERVE_TOKEN", "nct_fake-test-value")
    client.request("GET", "http://x", "/api/health")
    assert seen["headers"]["Authorization"] == "Bearer nct_fake-test-value"


def test_no_authorization_header_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    monkeypatch.delenv("NERVE_TOKEN", raising=False)
    client.request("GET", "http://x", "/api/health")
    assert "Authorization" not in seen["headers"]
