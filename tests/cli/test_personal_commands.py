"""R113 — §65 personal CLI surface tests (AC-R11).

Every new command (``attention`` + ``attention snooze``, ``today``,
``commitments``, ``waiting``, ``brief``, ``people``, ``communication`` /
``communication search``) is exercised with a Click ``CliRunner`` against a
seeded local fake HTTP backend — a stdlib ``ThreadingHTTPServer`` serving the
canonical §46/§63 routes with deterministic fixtures (the CLI itself carries
no demo data). The suite covers:

- success paths for every command (exit 0, canonical route + query hit)
- ``--json`` paths (raw API body emitted)
- usage/validation errors (exit 2), HTTP >= 400 contract errors (exit 2), and
  an unreachable backend (exit 127)
- ranked attention output includes the ``why`` explainability column
- existing command groups remain registered (preservation)
- routes the backend has not shipped yet report a contract error, not success
"""
from __future__ import annotations

import json
import socket
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import pytest
from click.testing import CliRunner

from cli.main import cli

# ---------------------------------------------------------------------------
# Seeded fixtures (local fake backend data — the CLI itself carries none)
# ---------------------------------------------------------------------------

ATTENTION_ITEMS = [
    {
        "attention_id": "at-001",
        "reason": "Reply to Alice about the Friday plan",
        "priority": 0.92,
        "urgency": 0.9,
        "importance": 0.95,
        "due": "2026-10-20T18:00:00+00:00",
        "entities": ["Alice Chen"],
        "sources": ["imessage"],
        "suggested_action": "Reply to Alice",
        "status": "NOW",
        "why": ["due within 3 hours", "person responds quickly on iMessage"],
        "created_at": "2026-10-20T07:00:00+00:00",
        "updated_at": "2026-10-20T07:00:00+00:00",
    },
    {
        "attention_id": "at-002",
        "reason": "Follow up with Sam on deployment credentials",
        "priority": 0.61,
        "urgency": 0.5,
        "importance": 0.7,
        "due": None,
        "entities": ["Sam Patel"],
        "sources": ["messages"],
        "suggested_action": "Follow up with Sam",
        "status": "WAITING_ON",
        "why": ["waiting on another person for 3 days"],
        "created_at": "2026-10-17T09:00:00+00:00",
        "updated_at": "2026-10-19T09:00:00+00:00",
    },
]

ATTENTION_META = {
    "version": "1.0.0",
    "request_id": "req-test-attention",
    "timestamp": "2026-10-20T08:00:00+00:00",
    "pagination": {"total": 2, "page": 1, "per_page": 50},
}

WAITING_ON_ITEMS = [
    {
        "request_id": "wo-001",
        "person_display_name": "Sam Patel",
        "person_id": "p-002",
        "description": "deployment credentials",
        "requested_at": "2026-10-17T09:00:00+00:00",
        "source": "MESSAGES",
        "source_thread_id": "th-7",
        "status": "OPEN",
        "resolved_at": None,
        "note": None,
        "follow_up_count": 1,
        "last_follow_up_at": "2026-10-19T09:00:00+00:00",
        "attention_id": "at-002",
    }
]

WAITING_ON_META = {
    "version": "1.0.0",
    "request_id": "req-test-waiting",
    "timestamp": "2026-10-20T08:00:00+00:00",
    "pagination": {"total": 1, "page": 1, "per_page": 200},
}

COMMITMENTS = [
    {
        "finding_id": "cm-001",
        "person_id": "p-001",
        "person_display_name": "Alice Chen",
        "statement": "review the draft by Friday",
        "due": "2026-10-23T12:00:00+00:00",
        "status": "OPEN",
        "source": "imessage",
    }
]

COMMITMENTS_META = {
    "version": "1.0.0",
    "request_id": "req-test-commitments",
    "timestamp": "2026-10-20T08:00:00+00:00",
    "pagination": {"total": 1, "page": 1, "per_page": 100},
}

OPERATIONAL = {
    "as_of": "2026-10-20T08:00:00+00:00",
    "open_commitments": COMMITMENTS,
    "incoming_requests": [
        {"request_id": "rq-001", "person": "Alice Chen", "what": "draft review"}
    ],
    "waiting_on": [{"request_id": "wo-001", "person": "Sam Patel", "days": 3}],
    "upcoming_deadlines": [
        {"title": "Design review", "due": "2026-10-20T17:00:00+00:00"}
    ],
    "pending_approvals": [],
    "calendar_today": [
        {"event_id": "ev-1", "title": "Standup", "start": "2026-10-20T09:00:00+00:00"}
    ],
    "active_incidents": [],
    "blocked_goals": [],
}

BRIEFING = {
    "kind": "morning",
    "generated_at": "2026-10-20T07:30:00+00:00",
    "actions": [
        {"attention_id": "at-001", "label": "Reply to Alice", "due": "2026-10-20T18:00:00+00:00"}
    ],
    "waiting_on": [
        {"request_id": "wo-001", "person": "Sam Patel", "description": "deployment credentials", "days_elapsed": 3}
    ],
    "today": [{"event_id": "ev-1", "title": "Standup", "start": "2026-10-20T09:00:00+00:00"}],
    "overnight": {"emails": 2, "messages": 5, "voicemails": 1},
    "empty": False,
}

PEOPLE = [
    {
        "person_id": "p-001",
        "display_name": "Alice Chen",
        "is_vip": True,
        "identities": [
            {"kind": "email", "value": "alice@example.com", "raw_value": "Alice <alice@example.com>"}
        ],
        "proposed_merges": [],
        "created_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-10-20T07:00:00+00:00",
    },
    {
        "person_id": "p-002",
        "display_name": "Sam Patel",
        "is_vip": False,
        "identities": [
            {"kind": "phone", "value": "+15550001234", "raw_value": "(555) 000-1234"}
        ],
        "proposed_merges": ["p-003"],
        "created_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-10-20T07:00:00+00:00",
    },
]

PEOPLE_META = {
    "version": "1.0.0",
    "request_id": "req-test-people",
    "timestamp": "2026-10-20T08:00:00+00:00",
    "pagination": {"total": 2, "page": 1, "per_page": 100},
}

COMMUNICATIONS = [
    {
        "communication_id": "cm-100",
        "person_id": "p-001",
        "channel": "voicemail",
        "direction": "INBOUND",
        "sender": "Alice Chen",
        "recipients": [],
        "timestamp": "2026-10-19T21:15:00+00:00",
        "thread_id": "th-9",
        "content_ref": "ref://voicemail/vm-1",
        "attachments": [],
        "classification": {"kind": "REQUEST", "confidence": 0.8},
        "sensitivity": "PERSONAL",
        "content_routing": {"routing": "LOCAL_ONLY"},
        "source": "voicemail",
    }
]

COMMUNICATIONS_META = {
    "version": "1.0.0",
    "request_id": "req-test-comm",
    "timestamp": "2026-10-20T08:00:00+00:00",
    "pagination": {"total": 1, "page": 1, "per_page": 100},
}

SEARCH_RESULTS = {
    "data": [
        {"kind": "communication", "id": "cm-100", "channel": "voicemail",
         "person": "Alice Chen", "summary": "voicemail about the prescription"},
        {"kind": "person", "id": "p-001", "display_name": "Alice Chen",
         "summary": "2 communications this week"},
    ],
    "meta": {
        "version": "1.0.0",
        "request_id": "req-test-search",
        "timestamp": "2026-10-20T08:00:00+00:00",
        "pagination": {"total": 2, "page": 1, "per_page": 50},
    },
}


# ---------------------------------------------------------------------------
# Fake backend
# ---------------------------------------------------------------------------


class RequestRecord:
    """One request observed by the fake backend."""

    def __init__(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.body = body

    def __repr__(self) -> str:
        return f"RequestRecord({self.method} {self.path} {self.query} {self.body!r})"


Dispatch = Callable[..., tuple[int, Any]]


def _snooze_response(attention_id: str, body: Any) -> Any:
    """Echo a snoozed item reflecting the request's ``until``."""
    until = (body or {}).get("until")
    item = dict(ATTENTION_ITEMS[0])
    item["attention_id"] = attention_id
    item["status"] = "TODAY"
    item["due"] = until
    item["why"] = ["snoozed until " + str(until)]
    return item


def _mutated_item(attention_id: str, status: str, **extra: Any) -> Any:
    item = dict(ATTENTION_ITEMS[0])
    item["attention_id"] = attention_id
    item["status"] = status
    item.update(extra)
    return item


def _default_dispatch(
    method: str, path: str, query: dict[str, list[str]], body: Any
) -> tuple[int, Any]:
    """Seed the canonical routes (AC-R11 targets the contract paths)."""
    if method == "GET" and path == "/api/attention":
        return 200, {"data": ATTENTION_ITEMS, "meta": ATTENTION_META}
    if method == "GET" and path in ("/api/attention/at-001", "/api/attention/at-002"):
        item = dict(ATTENTION_ITEMS[1]) if path.endswith("at-002") else dict(ATTENTION_ITEMS[0])
        return 200, item
    if method == "POST" and path == "/api/attention/at-001/snooze":
        return 200, _snooze_response("at-001", body)
    if method == "POST" and path.endswith("/resolve"):
        return 200, _mutated_item(path.split("/")[3], "RESOLVED")
    if method == "POST" and path.endswith("/suppress"):
        return 200, _mutated_item(path.split("/")[3], "SUPPRESSED")
    if method == "POST" and path.endswith("/acknowledge"):
        return 200, _mutated_item(path.split("/")[3], "RESOLVED")
    if method == "GET" and path == "/api/briefing/operational":
        return 200, OPERATIONAL
    if method == "GET" and path == "/api/briefing":
        kind = (query.get("kind") or ["morning"])[0]
        return 200, dict(BRIEFING, kind=kind)
    if method == "GET" and path == "/api/commitments":
        return 200, {"data": COMMITMENTS, "meta": COMMITMENTS_META}
    if method == "GET" and path == "/api/waiting-on":
        status = (query.get("status") or ["open"])[0]
        data = [w for w in WAITING_ON_ITEMS if status in ("all", str(w["status"]).lower())]
        return 200, {"data": data, "meta": dict(
            WAITING_ON_META, pagination={"total": len(data), "page": 1, "per_page": 200})}
    if method == "GET" and path == "/api/people":
        return 200, {"data": PEOPLE, "meta": PEOPLE_META}
    if method == "GET" and path == "/api/communications":
        return 200, {"data": COMMUNICATIONS, "meta": COMMUNICATIONS_META}
    if method == "GET" and path == "/api/communications/search":
        return 200, dict(SEARCH_RESULTS)
    return 404, {"detail": f"No route for {method} {path}"}


def _missing_route_dispatch(
    method: str, path: str, query: dict[str, list[str]], body: Any
) -> tuple[int, Any]:
    """A backend that has not shipped the personal routes yet (all 404)."""
    return 404, {"detail": f"No route for {method} {path}"}


class _Handler(BaseHTTPRequestHandler):
    """Dispatch to the seeded routes; record every request on the server."""

    def _respond(self, method: str) -> None:
        parts = urlsplit(self.path)
        query: dict[str, list[str]] = {k: v for k, v in parse_qs(parts.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        body: Any = None
        if length:
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = raw.decode("utf-8", "replace")
        dispatch: Dispatch = self.server.dispatch
        records: list[RequestRecord] = self.server.records
        status, payload = dispatch(method, parts.path, query, body)
        records.append(RequestRecord(method, parts.path, query, body))
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        self._respond("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._respond("POST")

    def log_message(self, format, *args: Any) -> None:  # noqa: N802
        pass


def _start_server(dispatch: Dispatch) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.dispatch = dispatch  # type: ignore[attr-defined]
    server.records = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture()
def fake_api():
    """A seeded local fake HTTP backend on an ephemeral port."""
    server = _start_server(_default_dispatch)
    yield {
        "base_url": f"http://127.0.0.1:{server.server_address[1]}",
        "requests": server.records,  # type: ignore[attr-defined]
    }
    server.shutdown()
    server.server_close()


@pytest.fixture()
def fake_api_missing():
    """A backend that has not shipped the personal routes (all 404)."""
    server = _start_server(_missing_route_dispatch)
    yield {
        "base_url": f"http://127.0.0.1:{server.server_address[1]}",
        "requests": server.records,  # type: ignore[attr-defined]
    }
    server.shutdown()
    server.server_close()


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _api_args(base_url: str, *extra: str) -> list[str]:
    return [*extra, "--api", base_url]


# ---------------------------------------------------------------------------
# attention
# ---------------------------------------------------------------------------


def test_attention_list_ranked_table_includes_why(runner, fake_api) -> None:
    result = runner.invoke(cli, _api_args(fake_api["base_url"], "attention"))
    assert result.exit_code == 0, result.output
    # The ranked table carries the why column and both seeded items.
    assert "why" in result.output
    assert "due within 3 hours" in result.output
    assert "waiting on another person for 3 days" in result.output
    assert "Reply to Alice about the Friday plan" in result.output
    assert "Follow up with Sam on deployment credentials" in result.output
    # Ranking: the 0.92 item is listed before the 0.61 item.
    assert result.output.index("0.92") < result.output.index("0.61")
    # The canonical route was hit.
    assert fake_api["requests"][0].method == "GET"
    assert fake_api["requests"][0].path == "/api/attention"


def test_attention_list_json(runner, fake_api) -> None:
    result = runner.invoke(cli, _api_args(fake_api["base_url"], "attention", "--json"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"][0]["attention_id"] == "at-001"
    assert payload["data"][0]["why"] == [
        "due within 3 hours",
        "person responds quickly on iMessage",
    ]
    assert payload["meta"]["pagination"]["total"] == 2


def test_attention_list_subcommand_matches_bare(runner, fake_api) -> None:
    result = runner.invoke(cli, _api_args(fake_api["base_url"], "attention", "list"))
    assert result.exit_code == 0, result.output
    assert "why" in result.output


def test_attention_show(runner, fake_api) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "attention", "show", "at-002", "--json")
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["attention_id"] == "at-002"
    assert fake_api["requests"][-1].path == "/api/attention/at-002"


def test_attention_snooze_relative_tomorrow(runner, fake_api) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "attention", "snooze", "at-001", "tomorrow", "--json")
    )
    assert result.exit_code == 0, result.output
    record = fake_api["requests"][-1]
    assert record.method == "POST"
    assert record.path == "/api/attention/at-001/snooze"
    until = record.body["until"]
    parsed = datetime.fromisoformat(until)
    assert parsed.tzinfo is not None
    # "tomorrow" → ~24h out.
    assert timedelta(hours=20) < (parsed - datetime.now(UTC)) < timedelta(hours=28)
    # The response echoes the snoozed state.
    payload = json.loads(result.output)
    assert payload["status"] == "TODAY"
    assert payload["due"] == until


def test_attention_snooze_iso_date(runner, fake_api) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "attention", "snooze", "at-001", "2026-10-21")
    )
    assert result.exit_code == 0, result.output
    until = fake_api["requests"][-1].body["until"]
    parsed = datetime.fromisoformat(until)
    # A bare date is interpreted in the local zone (naive → local wall clock).
    assert parsed.tzinfo is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 10, 21)
    assert (parsed.hour, parsed.minute) == (0, 0)


def test_attention_snooze_invalid_when_is_usage_error(runner, fake_api) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "attention", "snooze", "at-001", "whenever")
    )
    assert result.exit_code == 2, result.output
    assert "invalid snooze time" in result.stderr
    # No request was made: the command failed client-side.
    assert fake_api["requests"] == []


def test_attention_snooze_missing_when_is_usage_error(runner, fake_api) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "attention", "snooze", "at-001")
    )
    assert result.exit_code == 2, result.output
    assert "Missing argument" in result.stderr
    assert fake_api["requests"] == []


def test_attention_resolve(runner, fake_api) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "attention", "resolve", "at-001", "--json")
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "RESOLVED"
    assert fake_api["requests"][-1].path == "/api/attention/at-001/resolve"


def test_attention_suppress(runner, fake_api) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "attention", "suppress", "at-002")
    )
    assert result.exit_code == 0, result.output
    assert "SUPPRESSED" in result.output
    assert fake_api["requests"][-1].path == "/api/attention/at-002/suppress"


def test_attention_acknowledge(runner, fake_api) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "attention", "acknowledge", "at-001")
    )
    assert result.exit_code == 0, result.output
    assert fake_api["requests"][-1].path == "/api/attention/at-001/acknowledge"


def test_attention_show_unknown_id_contract_error(runner, fake_api) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "attention", "show", "nope")
    )
    assert result.exit_code == 2, result.output
    assert "HTTP 404" in result.stderr
    assert "No route for GET /api/attention/nope" in result.stderr


def test_attention_unreachable_backend_exit_127(runner) -> None:
    # A closed port → UnreachableError → exit 127.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    result = runner.invoke(cli, _api_args(f"http://127.0.0.1:{port}", "attention"))
    assert result.exit_code == 127, result.output
    assert "cannot reach NerveCenter API" in result.stderr


# ---------------------------------------------------------------------------
# today / commitments / waiting / brief
# ---------------------------------------------------------------------------


def test_today_fetches_operational_state(runner, fake_api) -> None:
    result = runner.invoke(cli, _api_args(fake_api["base_url"], "today", "--json"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["open_commitments"][0]["finding_id"] == "cm-001"
    assert payload["waiting_on"][0]["person"] == "Sam Patel"
    assert fake_api["requests"][0].path == "/api/briefing/operational"


def test_today_text_rendering(runner, fake_api) -> None:
    result = runner.invoke(cli, _api_args(fake_api["base_url"], "today"))
    assert result.exit_code == 0, result.output
    assert "Standup" in result.output
    assert "Design review" in result.output


def test_today_missing_route_reports_contract_error(runner, fake_api_missing) -> None:
    # When the backend has not shipped /api/briefing/operational yet, the
    # command reports a contract error (exit 2), not a false success.
    result = runner.invoke(
        cli, _api_args(fake_api_missing["base_url"], "today")
    )
    assert result.exit_code == 2, result.output
    assert "HTTP 404" in result.stderr


def test_commitments(runner, fake_api) -> None:
    result = runner.invoke(cli, _api_args(fake_api["base_url"], "commitments", "--json"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"][0]["statement"] == "review the draft by Friday"
    assert fake_api["requests"][0].path == "/api/commitments"


def test_commitments_text_rendering(runner, fake_api) -> None:
    result = runner.invoke(cli, _api_args(fake_api["base_url"], "commitments"))
    assert result.exit_code == 0, result.output
    assert "review the draft by Friday" in result.output


def test_commitments_missing_route_reports_contract_error(runner, fake_api_missing) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api_missing["base_url"], "commitments")
    )
    assert result.exit_code == 2, result.output
    assert "HTTP 404" in result.stderr


def test_waiting(runner, fake_api) -> None:
    result = runner.invoke(cli, _api_args(fake_api["base_url"], "waiting", "--json"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"][0]["request_id"] == "wo-001"
    assert fake_api["requests"][0].path == "/api/waiting-on"
    assert fake_api["requests"][0].query == {}


def test_waiting_status_filter(runner, fake_api) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "waiting", "--status", "all", "--json")
    )
    assert result.exit_code == 0, result.output
    assert fake_api["requests"][-1].query == {"status": ["all"]}


def test_brief(runner, fake_api) -> None:
    result = runner.invoke(cli, _api_args(fake_api["base_url"], "brief", "--json"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "morning"
    assert payload["waiting_on"][0]["person"] == "Sam Patel"
    assert fake_api["requests"][0].path == "/api/briefing"
    assert fake_api["requests"][0].query == {}


def test_brief_kind_evening(runner, fake_api) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "brief", "--kind", "evening", "--json")
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["kind"] == "evening"
    assert fake_api["requests"][-1].query == {"kind": ["evening"]}


# ---------------------------------------------------------------------------
# people / communication
# ---------------------------------------------------------------------------


def test_people_list(runner, fake_api) -> None:
    result = runner.invoke(cli, _api_args(fake_api["base_url"], "people", "--json"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [p["person_id"] for p in payload["data"]] == ["p-001", "p-002"]
    assert fake_api["requests"][0].path == "/api/people"


def test_people_query_filters_client_side(runner, fake_api) -> None:
    # The query is a client-side projection of the fetched records (the API
    # has no query param); it applies to the rendered table.
    result = runner.invoke(cli, _api_args(fake_api["base_url"], "people", "alice"))
    assert result.exit_code == 0, result.output
    assert "Alice Chen" in result.output
    assert "Sam Patel" not in result.output
    # One plain list request went to the API; the filter happened locally.
    assert len(fake_api["requests"]) == 1
    assert fake_api["requests"][0].path == "/api/people"


def test_people_query_no_match(runner, fake_api) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "people", "nobody-here")
    )
    assert result.exit_code == 0, result.output
    assert "(empty)" in result.output


def test_people_identity_value_match(runner, fake_api) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "people", "alice@example.com")
    )
    assert result.exit_code == 0, result.output
    assert "Alice Chen" in result.output
    assert "Sam Patel" not in result.output


def test_communication_list(runner, fake_api) -> None:
    result = runner.invoke(cli, _api_args(fake_api["base_url"], "communication", "--json"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"][0]["communication_id"] == "cm-100"
    assert fake_api["requests"][0].path == "/api/communications"


def test_communication_list_subcommand(runner, fake_api) -> None:
    result = runner.invoke(cli, _api_args(fake_api["base_url"], "communication", "list"))
    assert result.exit_code == 0, result.output
    assert "cm-100" in result.output


def test_communication_search(runner, fake_api) -> None:
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "communication", "search", "prescription", "--json")
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"][0]["summary"] == "voicemail about the prescription"
    record = fake_api["requests"][0]
    assert record.method == "GET"
    assert record.path == "/api/communications/search"
    assert record.query == {"q": ["prescription"]}


def test_communication_search_requires_query(runner, fake_api) -> None:
    result = runner.invoke(cli, _api_args(fake_api["base_url"], "communication", "search"))
    assert result.exit_code == 2, result.output
    assert "Missing argument" in result.stderr


# ---------------------------------------------------------------------------
# exit conventions + preservation
# ---------------------------------------------------------------------------


def test_invalid_api_url_is_usage_error(runner) -> None:
    result = runner.invoke(cli, ["attention", "--api", "not-a-url"])
    assert result.exit_code == 2, result.output
    assert "invalid --api URL" in result.stderr


def test_all_command_groups_registered(runner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    help_text = result.output
    # R113 additions.
    for name in ("attention", "today", "commitments", "waiting", "brief", "people", "communication"):
        assert name in help_text, name
    # Pre-existing commands preserved.
    for name in ("status", "ask", "entity", "changes", "goal", "task", "agents",
                 "compute", "routes", "incident", "pause-agents", "stop-mutations"):
        assert name in help_text, name


def test_attention_snooze_bad_id_contract_error(runner, fake_api) -> None:
    # A seeded snooze only exists for at-001; another id 404s → exit 2.
    result = runner.invoke(
        cli, _api_args(fake_api["base_url"], "attention", "snooze", "ghost", "tomorrow")
    )
    assert result.exit_code == 2, result.output
    assert "HTTP 404" in result.stderr
