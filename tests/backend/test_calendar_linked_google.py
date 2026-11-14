"""Calendar accounts linked to a connected Google account (NS-ADR-096).

The owner's Google accounts are already connected for mail. A calendar account
links to one of them (``connection_id``) instead of pasting a refresh token;
sync uses that connection's refresh token and the deployment's OAuth app keys.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from test_calendar_integration import (
    CALENDAR_TABLES,
    FakeTransport,
    _google_calendar_list_response,
    _google_items_response,
    _token_response,
)

from backend.calendar.service import CalendarService
from backend.calendar.sync import CalendarSyncRunner, _with_app_keys
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import ConnectionRow
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.connections_repo import IdentitySecretCodec


@pytest.fixture
async def wired(tmp_path: pathlib.Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cal.db'}",
                                 poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(ConnectionRow.__table__.metadata.create_all,
                            tables=[*CALENDAR_TABLES, ConnectionRow.__table__])
    registry = RepositoryRegistry(make_session_factory(engine), secret_codec=IdentitySecretCodec())
    from backend.api.endpoints import calendar as cal

    app = FastAPI()
    app.include_router(cal.router)
    app.state.repository_registry = registry
    app.state.calendar_service = CalendarService()
    yield TestClient(app), registry
    await engine.dispose()


async def _gmail(registry, refresh: str | None = "rt-gmail"):
    conn = await registry.connections.create_connection(kind="gmail", label="me@gmail.example")
    if refresh:
        await registry.connections.upsert_secret(kind="refresh_token", owner_kind="connection",
                                                 owner_id=conn.id, plaintext=refresh)
    return conn


async def test_linking_needs_no_pasted_token(wired) -> None:
    client, registry = wired
    conn = await _gmail(registry)
    r = client.post("/api/calendar/accounts", json={"provider": "google", "label": "Personal",
                                                    "connection_id": conn.id})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["config"]["connection_id"] == conn.id
    assert not body.get("secret_ref")
    assert "client_secret" not in json.dumps(body)


@pytest.mark.parametrize("payload", [
    {"provider": "caldav", "connection_id": "X"},
    {"provider": "google", "connection_id": "missing"},
])
async def test_bad_links_are_rejected(wired, payload) -> None:
    client, registry = wired
    conn = await _gmail(registry)
    payload = {**payload, "connection_id": conn.id if payload["connection_id"] == "X"
               else payload["connection_id"]}
    assert client.post("/api/calendar/accounts", json=payload).status_code == 422


async def test_a_link_cannot_be_smuggled_through_config(wired) -> None:
    client, registry = wired
    conn = await _gmail(registry)
    r = client.post("/api/calendar/accounts", json={"provider": "google",
                                                    "config": {"connection_id": conn.id}})
    assert r.status_code == 201
    assert "connection_id" not in r.json()["config"]


async def test_one_calendar_per_connected_account(wired) -> None:
    client, registry = wired
    conn = await _gmail(registry)
    body = {"provider": "google", "connection_id": conn.id}
    assert client.post("/api/calendar/accounts", json=body).status_code == 201
    assert client.post("/api/calendar/accounts", json=body).status_code == 409


async def test_a_mail_imap_connection_cannot_be_linked(wired) -> None:
    client, registry = wired
    imap = await registry.connections.create_connection(kind="imap")
    r = client.post("/api/calendar/accounts", json={"provider": "google", "connection_id": imap.id})
    assert r.status_code == 422


async def test_linked_account_syncs_with_the_connection_token(wired, monkeypatch) -> None:
    monkeypatch.setenv("NERVE_GOOGLE_CLIENT_ID", "app-id")
    monkeypatch.setenv("NERVE_GOOGLE_CLIENT_SECRET", "app-secret")
    _client, registry = wired
    conn = await _gmail(registry, refresh="rt-gmail")
    acct = await registry.calendar.create_account(
        provider="google", config={"connection_id": conn.id}
    )
    transport = FakeTransport({
        ("POST", "https://oauth2.googleapis.com/token"): _token_response(),
        ("GET", "https://www.googleapis.com/calendar/v3/users/me/calendarList"):
            _google_calendar_list_response(),
        ("GET", "https://www.googleapis.com/calendar/v3/calendars/"): _google_items_response(
            [{"id": "e1", "summary": "Dentist", "start": {"dateTime": "2026-11-20T17:00:00Z"},
              "end": {"dateTime": "2026-11-20T18:00:00Z"}, "updated": "2026-11-14T00:00:00Z"}],
            next_token="t1"),
    })
    runner = CalendarSyncRunner(registry, calendar_service=CalendarService(), transport=transport)
    summary = await runner.sync_account(acct.id)
    assert summary["synced"] is True and summary["ingested"] == 1
    method, url, _h, _p = transport.calls[0]
    assert (method, url) == ("POST", "https://oauth2.googleapis.com/token")
    row = await registry.calendar.get_account(acct.id)
    assert row.operational_status == "healthy"
    assert "client_secret" not in json.dumps(row.config or {}), "app keys are never persisted"


async def test_linked_account_without_a_token_reports_no_credential(wired) -> None:
    _client, registry = wired
    conn = await _gmail(registry, refresh=None)
    acct = await registry.calendar.create_account(
        provider="google", config={"connection_id": conn.id}
    )
    runner = CalendarSyncRunner(registry, calendar_service=CalendarService(),
                                transport=FakeTransport({}))
    await runner.sync_account(acct.id)
    row = await registry.calendar.get_account(acct.id)
    assert row.last_error_code == "no_credential"


def test_app_keys_only_for_linked_google(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setenv("NERVE_GOOGLE_CLIENT_ID", "app-id")
    monkeypatch.setenv("NERVE_GOOGLE_CLIENT_SECRET", "app-secret")
    linked = SimpleNamespace(provider="google", config={"connection_id": "c"})
    assert _with_app_keys(linked)["client_id"] == "app-id"
    own = SimpleNamespace(provider="google", config={"client_id": "mine"})
    assert _with_app_keys(own) == {"client_id": "mine"}
    caldav = SimpleNamespace(provider="caldav", config={"connection_id": "c"})
    assert "client_id" not in _with_app_keys(caldav)
