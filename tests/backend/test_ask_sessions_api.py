"""Saved Ask chats: per-account isolation, validation and caps."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.api.endpoints import ask_sessions as ep


def _client(sub: str | None) -> tuple[TestClient, FastAPI]:
    app = FastAPI()

    @app.middleware("http")
    async def principal(request: Request, call_next):  # type: ignore[no-untyped-def]
        if sub is not None:
            request.state.principal = {"sub": sub}
        return await call_next(request)

    app.include_router(ep.router)
    return TestClient(app), app


def _doc(*titles: str) -> dict:
    return {
        "active_id": "s1",
        "sessions": [
            {
                "id": f"s{i + 1}",
                "title": t,
                "messages": [
                    {"id": "m1", "role": "user", "content": t, "timestamp": "2026-11-17T00:00:00Z"},
                    {
                        "id": "m2",
                        "role": "assistant",
                        "content": "ok",
                        "timestamp": "2026-11-17T00:00:01Z",
                        "provenance": {"facts": [], "hypotheses": []},
                    },
                ],
            }
            for i, t in enumerate(titles)
        ],
    }


def test_round_trip_keeps_messages_and_extra_message_fields() -> None:
    client, _ = _client("alice")
    assert client.get("/api/ask/sessions").json() == {
        "sessions": [],
        "active_id": None,
        "saved_at": None,
    }
    r = client.put("/api/ask/sessions", json=_doc("first", "second"))
    assert r.status_code == 200 and r.json()["sessions"] == 2
    got = client.get("/api/ask/sessions").json()
    assert [s["title"] for s in got["sessions"]] == ["first", "second"]
    assert got["active_id"] == "s1" and got["saved_at"]
    assert got["sessions"][0]["messages"][1]["provenance"] == {"facts": [], "hypotheses": []}


def test_accounts_do_not_see_each_other() -> None:
    shared = ep._MemoryPrefs()
    alice, alice_app = _client("alice")
    bob, bob_app = _client("bob")
    alice_app.state.ask_sessions_repo = shared
    bob_app.state.ask_sessions_repo = shared
    alice.put("/api/ask/sessions", json=_doc("alice chat"))
    assert bob.get("/api/ask/sessions").json()["sessions"] == []
    assert alice.get("/api/ask/sessions").json()["sessions"][0]["title"] == "alice chat"


def test_no_subject_is_refused() -> None:
    client, _ = _client(None)
    assert client.get("/api/ask/sessions").status_code == 401
    assert client.put("/api/ask/sessions", json=_doc("x")).status_code == 401


@pytest.mark.parametrize(
    "bad",
    [
        {
            "sessions": [
                {
                    "id": "s",
                    "title": "t",
                    "messages": [{"id": "m", "role": "robot", "content": "", "timestamp": ""}],
                }
            ]
        },
        {"sessions": [{"id": "", "title": "t", "messages": []}]},
        {
            "sessions": [
                {"id": f"s{i}", "title": "t", "messages": []} for i in range(ep.MAX_SESSIONS + 1)
            ]
        },
    ],
)
def test_invalid_documents_are_rejected(bad: dict) -> None:
    client, _ = _client("alice")
    assert client.put("/api/ask/sessions", json=bad).status_code == 422
