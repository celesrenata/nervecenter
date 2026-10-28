"""Transport-correctness tests for the durable mail plane (P0-A + P0-B).

These pin the *pinned cursor contract* the parallel mail-worker owns:

- IMAP (``imap_seq``): ``{"uid": int, "uidvalidity": int, "folder": str,
  "seen": [...]}`` — cursor is per (connection, folder); the folder is
  SELECTed explicitly (never derived from the mailbox email address); a
  UIDVALIDITY change resets that folder to a fresh sync.
- Gmail (``gmail_history``): ``{"history_id": int, "account": str,
  "seen": [...]}`` — resume reuses the saved valid history id (never ``+1``);
  an expired id (404) triggers a bounded full resync.

Wires are faked at the boundary only: a fake ``imaplib``-shaped object for
the IMAP transport and a fake ``httpx.AsyncClient`` for the Gmail transport
(stateless, page-based, mirroring the real ``history.startHistoryId``
inclusive semantics). The real client code runs end to end: ``run()`` is
idempotent for a given cursor — re-running from the saved cursor re-yields
the same items, no skips, no duplicates.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from backend.api.models import MailConnector
from backend.connectors.mail.mail_transport import (
    GmailClient,
    ImapPollClient,
    TransportAuthError,
    TransportConnectError,
    _b64url_decode,
)

_ACCOUNT = "user@example.com"
_SECRET = "super-secret-password"


def _connector(**kw: Any) -> MailConnector:
    base = dict(
        connector_id="c-1",
        account="Work",
        mailbox=_ACCOUNT,
        host="imap.example.com",
        port=993,
    )
    base.update(kw)
    return MailConnector(**base)


def _date(i: int) -> str:
    # RFC-2822 dates, stable per index.
    return f"Mon, 27 Oct 2026 {i % 24:02d}:{i % 60:02d}:00 +0000"


# ---------------------------------------------------------------------------
# IMAP fakes
# ---------------------------------------------------------------------------


class FakeImap:
    """imaplib-shaped fake.

    ``folders`` maps a *folder name* to ``{"uids": {int: raw bytes},
    "validity": int}``. SELECT records the requested folder and validates it
    (unknown folder → NO response, so no SEARCH/FETCH can follow); a NO
    response is what the client must map to TransportConnectError.
    """

    def __init__(self, *, folders: dict[str, dict[str, Any]]) -> None:
        self.folders = folders
        self.login_calls: list[tuple[str, str]] = []
        self.select_calls: list[str] = []
        self.search_calls: list[tuple[str, str]] = []  # (folder, criterion)
        self.current: str | None = None

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.login_calls.append((user, password))
        return "OK", [b"Logged in"]

    def select(self, mailbox: str, readonly: bool = False):
        self.select_calls.append(str(mailbox))
        folder = str(mailbox)
        if folder not in self.folders:
            return "NO", [f'UNKNOWN mailbox name "{folder}"'.encode()]
        self.current = folder
        self.untagged_responses = {
            "EXISTS": [b"3"],
            "RECENT": [b"0"],
            "UIDVALIDITY": [str(self.folders[folder].get("validity", 1)).encode()],
            "FLAGS": [],
        }
        return "OK", [b"3"]

    def uid(self, op: str, *args: Any):
        assert self.current is not None, "UID command before SELECT"
        if op == "SEARCH":
            criterion = str(args[-1])
            self.search_calls.append((self.current, criterion))
            uids = set(self.folders[self.current]["uids"])
            if criterion == "ALL":
                found = uids
            else:
                lo, _sep, rest = criterion.partition(":")
                lo = int(lo)
                found = {u for u in uids if u >= lo and rest in ("", "*")}
            joined = b" ".join(str(u).encode() for u in sorted(found))
            return "OK", [joined]
        # FETCH: one message per uid
        uid = int(args[0])
        body = self.folders[self.current]["uids"][uid]
        return "OK", [(b"1 (BODY.PEEK[] {5})", body)]

    def logout(self):
        return "OK", [b"BYE"]


def imap_msg(subject: str, message_id: str, body: str, date: str) -> bytes:
    return (
        f"From: external@sender.com\r\n"
        f"To: {_ACCOUNT}\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {date}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"\r\n"
        f"{body}\n"
    ).encode()


def _install_imap(monkeypatch: Any, fake: FakeImap) -> None:
    import backend.connectors.mail.mail_transport as mt

    def factory(host: str, port: int, timeout: float | None = None) -> FakeImap:
        return fake

    monkeypatch.setattr(mt.imaplib, "IMAP4_SSL", factory)


# ---------------------------------------------------------------------------
# Gmail fakes
# ---------------------------------------------------------------------------


class FakeGmailResp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


def gmail_msg_resource(
    mid: str, subject: str, date: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """A full message resource with headers + a payload (plain or multipart)."""
    return {
        "id": mid,
        "labelIds": ["INBOX"],
        "threadId": f"t-{mid}",
        "internalDate": "1701000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "external@sender.com"},
                {"name": "To", "value": _ACCOUNT},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": date},
                {"name": "Message-ID", "value": f"<{mid}@gmail>"},
            ],
            **payload,
        },
    }


def _plain_payload(text: str) -> dict[str, Any]:
    return {
        "mimeType": "text/plain; charset=utf-8",
        "bodyData": {"size": "1", "data": base64.urlsafe_b64encode(text.encode()).decode()},
    }


class FakeGmailClient:
    """httpx.AsyncClient-shaped fake for the Gmail wire.

    Stateless page model: the full entry stream (``entries`` sorted by id)
    is served in windows of ``page_size`` with **inclusive**
    ``history.startHistoryId`` semantics — entries with id >= the requested
    id are returned, and ``historyNewerThan`` is the id of the first entry
    beyond the window (falsy when the stream is exhausted). This mirrors the
    real API, so "restart from the saved cursor" replays the last processed
    entry (whose seen items are suppressed by the durable seen-list) and
    yields only the unprocessed tail.
    """

    def __init__(
        self,
        *,
        entries: list[dict[str, Any]] | None = None,
        messages: dict[str, tuple[int, dict[str, Any]]] | None = None,
        page_size: int = 25,
        start_id: int = 10,
        expired_ids: frozenset[int] = frozenset(),
        inbox_message_ids: list[str] | None = None,
    ) -> None:
        self._entries = sorted(entries or [], key=lambda e: int(e["id"]))
        self._messages = messages or {}
        self._page_size = page_size
        self._start_id = start_id
        self._expired = expired_ids
        self._inbox_ids = list(inbox_message_ids or [])
        self.start_calls = 0
        self.history_calls: list[int] = []  # startHistoryId per call
        self.history_params: list[dict[str, Any]] = []
        self.fetched: list[str] = []

    async def __aenter__(self) -> FakeGmailClient:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    def _history_window(self, start: int) -> dict[str, Any]:
        stream = [e for e in self._entries if int(e["id"]) >= start]
        window = stream[: self._page_size]
        beyond = stream[self._page_size:]
        newer = min(int(e["id"]) for e in beyond) if beyond else None
        return {"history": window, "historyNewerThan": newer}

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> FakeGmailResp:
        params = dict(params or {})
        if url.endswith("/users/me/history.start"):
            self.start_calls += 1
            return FakeGmailResp(200, {"historyId": self._start_id})
        if url.endswith("/users/me/history"):
            start = int(params.get("history.startHistoryId", 0) or 0)
            self.history_calls.append(start)
            self.history_params.append(dict(params))
            if start in self._expired:
                return FakeGmailResp(404, {"error": {"code": 404, "message": "expired"}})
            return FakeGmailResp(200, self._history_window(start))
        if url.endswith("/users/me/messages"):
            return FakeGmailResp(200, {"messages": [{"id": m} for m in self._inbox_ids]})
        if "/messages/" in url:
            mid = url.rsplit("/", 1)[-1]
            self.fetched.append(mid)
            if mid not in self._messages:
                return FakeGmailResp(404, {"error": {"code": 404, "message": "notFound"}})
            status, resource = self._messages[mid]
            return FakeGmailResp(status, resource)
        raise AssertionError(f"unexpected url {url}")


def _install_gmail(monkeypatch: Any, fake: FakeGmailClient) -> None:
    import backend.connectors.mail.mail_transport as mt

    def factory(**kw: Any) -> FakeGmailClient:
        return fake

    monkeypatch.setattr(mt.httpx, "AsyncClient", factory)


def _gentry(i: int, base_id: int, mid: str) -> dict[str, Any]:
    return {"id": base_id + i, "messages": [{"id": mid}]}


def _gresource(mid: str, i: int) -> dict[str, Any]:
    return gmail_msg_resource(mid, f"subj-{i}", _date(i), _plain_payload(f"body-{i}"))


# ---------------------------------------------------------------------------
# Gmail tests (P0-A)
# ---------------------------------------------------------------------------


class TestGmailInitialSync:
    async def test_initial_sync_uses_history_start_and_no_invalid_types(
        self, monkeypatch: Any
    ) -> None:
        """A1/A2: first run must call history.start for a valid baseline
        (no ``startHistoryId`` on an empty account, no invalid
        ``historyTypes``), then poll history.get from the returned id."""
        fake = FakeGmailClient(
            entries=[_gentry(0, 10, "m1")],
            messages={"m1": (200, _gresource("m1", 1))},
            start_id=10,
        )
        _install_gmail(monkeypatch, fake)
        items, cursor = await GmailClient().run(
            _connector(), {}, credentials={"access_token": "tok"}
        )
        assert len(items) == 1
        assert items[0].message_id == "gmail:m1"
        assert fake.start_calls == 1
        assert fake.history_calls == [10]  # polled from the start id, never 11
        # A1: the invalid historyTypes=INBOX value must not be on the wire
        assert all("historyTypes" not in p for p in fake.history_params)
        # resume uses the inclusive startHistoryId (the entry is re-yielded on
        # restart; the seen-list suppresses it)
        assert fake.history_params[0]["history.startHistoryId"] == 10
        assert cursor == {
            "history_id": 10,
            "account": _ACCOUNT.lower(),
            "seen": [f"{_ACCOUNT.lower()}:gmail:m1"],
        }
        # the transient access_token is never in the cursor
        assert "tok" not in str(cursor)

    async def test_incoming_from_external_sender_is_accepted(
        self, monkeypatch: Any
    ) -> None:
        """A4: the From header never gates delivery — the sender is NOT the
        account, yet the message is delivered and keyed on the account."""
        fake = FakeGmailClient(
            entries=[_gentry(0, 5, "m1")],
            messages={
                "m1": (200, gmail_msg_resource("m1", "Hello", _date(1), _plain_payload("hi")))
            },
            start_id=5,
        )
        _install_gmail(monkeypatch, fake)
        items, cursor = await GmailClient().run(
            _connector(),
            {"history_id": 5, "account": _ACCOUNT.lower()},
            credentials={"access_token": "tok"},
        )
        assert len(items) == 1
        assert items[0].account == _ACCOUNT.lower()
        assert items[0].headers.get("From") == "external@sender.com"
        assert items[0].subject == "Hello"
        assert cursor["account"] == _ACCOUNT.lower()

    async def test_resume_reuses_saved_valid_history_id_without_plus_one(
        self, monkeypatch: Any
    ) -> None:
        """A3: resume polls from the SAVED valid id (history ids are
        non-contiguous — a +1 resume would skip entries)."""
        fake = FakeGmailClient(
            entries=[_gentry(0, 41, "m1"), _gentry(0, 44, "m2")],
            messages={"m1": (200, _gresource("m1", 1)), "m2": (200, _gresource("m2", 2))},
            start_id=40,
        )
        _install_gmail(monkeypatch, fake)
        items, cursor = await GmailClient().run(
            _connector(),
            {"history_id": 40, "account": _ACCOUNT.lower()},
            credentials={"access_token": "tok"},
        )
        assert fake.start_calls == 0  # resume: no history.start
        # polled from the saved id itself (41 and 44 both delivered — the gap
        # at 42/43 is irrelevant)
        assert fake.history_calls == [40]
        assert [m.message_id for m in items] == ["gmail:m1", "gmail:m2"]
        assert cursor["history_id"] == 44


class TestGmailMultipart:
    async def test_multipart_alternative_decodes_plain_text_exactly(
        self, monkeypatch: Any
    ) -> None:
        """A5/A6: multipart/alternative — prefer text/plain over text/html;
        exact decoded body bytes; seen-list advanced in the cursor."""
        exact = "Deadline: Thu, 30 Oct 2026 17:00 - send report 📊"
        exact_b64 = base64.urlsafe_b64encode(exact.encode("utf-8")).decode()
        html_b64 = base64.urlsafe_b64encode(
            b"<html><body><b>Deadline: Thu, 30 Oct 2026</b></body></html>"
        ).decode()
        resource = {
            "id": "mp1",
            "labelIds": ["INBOX"],
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [
                    {"name": "From", "value": "boss@corp.com"},
                    {"name": "To", "value": _ACCOUNT},
                    {"name": "Subject", "value": "Q4 review"},
                    {"name": "Date", "value": _date(2)},
                    {"name": "Message-ID", "value": "<mp1@gmail>"},
                ],
                "parts": [
                    # html first on purpose — plain must still win
                    {
                        "mimeType": "text/html",
                        "body": {"attachable": False, "bodyData": {"data": html_b64}},
                    },
                    {
                        "mimeType": "text/plain; charset=utf-8",
                        "body": {"attachable": False, "bodyData": {"data": exact_b64}},
                    },
                ],
            },
        }
        fake = FakeGmailClient(
            entries=[_gentry(0, 7, "mp1")],
            messages={"mp1": (200, resource)},
            start_id=7,
        )
        _install_gmail(monkeypatch, fake)
        items, cursor = await GmailClient().run(
            _connector(),
            {"history_id": 7, "account": _ACCOUNT.lower()},
            credentials={"access_token": "tok"},
        )
        assert len(items) == 1
        assert items[0].content_bytes == exact.encode("utf-8")
        assert items[0].subject == "Q4 review"
        assert cursor == {
            "history_id": 7,
            "account": _ACCOUNT.lower(),
            "seen": [f"{_ACCOUNT.lower()}:gmail:mp1"],
        }

    async def test_nested_multipart_mixed_falls_back_to_html_when_no_plain(
        self, monkeypatch: Any
    ) -> None:
        """A5: multipart/mixed containing a nested alternative — with no
        text/plain anywhere, fall back to the html part; attachable parts are
        skipped."""
        html = "<p>Only html here</p>"
        html_b64 = base64.urlsafe_b64encode(html.encode()).decode()
        resource = {
            "id": "mm1",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [
                    {"name": "From", "value": "a@b.c"},
                    {"name": "To", "value": _ACCOUNT},
                    {"name": "Subject", "value": "S"},
                    {"name": "Date", "value": _date(3)},
                    {"name": "Message-ID", "value": "<mm1@gmail>"},
                ],
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {
                                "mimeType": "text/html",
                                "body": {"attachable": False, "bodyData": {"data": html_b64}},
                            },
                        ],
                    },
                    {"mimeType": "application/pdf", "body": {"attachable": True}},
                ],
            },
        }
        fake = FakeGmailClient(
            entries=[_gentry(0, 9, "mm1")],
            messages={"mm1": (200, resource)},
            start_id=9,
        )
        _install_gmail(monkeypatch, fake)
        items, _ = await GmailClient().run(
            _connector(),
            {"history_id": 9, "account": _ACCOUNT.lower()},
            credentials={"access_token": "tok"},
        )
        assert len(items) == 1
        assert items[0].content_bytes == html.encode("utf-8")


class TestGmailPagination:
    def _paged_world(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, tuple[int, dict[str, Any]]]]:
        # 125 messages across 5 pages of 25 — entry ids 10..134 (one entry
        # per message, message ids gm1..gm125).
        entries = [_gentry(i, 10, f"gm{i + 1}") for i in range(125)]
        messages = {f"gm{i + 1}": (200, _gresource(f"gm{i + 1}", i + 1)) for i in range(125)}
        return entries, messages

    async def test_restart_after_partial_page_yields_no_skips_or_dupes(
        self, monkeypatch: Any
    ) -> None:
        """A8: max_items (40) smaller than the 125-message stream → the saved
        cursor is the LAST PROCESSED entry; restarting re-yields the
        unprocessed tail (no skips, no duplicates)."""
        entries, messages = self._paged_world()
        fake = FakeGmailClient(entries=entries, messages=messages, start_id=10)
        _install_gmail(monkeypatch, fake)
        client = GmailClient(max_items=40)
        run1, cursor1 = await client.run(
            _connector(), {}, credentials={"access_token": "tok"}
        )
        assert len(run1) == 40
        assert [m.message_id for m in run1] == [f"gmail:gm{i}" for i in range(1, 41)]
        # gm40 sits in entry id 10+39=49 → cursor == that entry, not 50+
        assert cursor1["history_id"] == 49
        assert cursor1["seen"] == [
            f"{_ACCOUNT.lower()}:gmail:gm{i}" for i in range(1, 41)
        ]
        assert cursor1["account"] == _ACCOUNT.lower()

        # "restart": fresh client, same world; the saved cursor replays the
        # last processed entry (suppressed by seen) and yields only the tail.
        fake2 = FakeGmailClient(entries=entries, messages=messages, start_id=10)
        _install_gmail(monkeypatch, fake2)
        run2, cursor2 = await client.run(
            _connector(), cursor1, credentials={"access_token": "tok"}
        )
        ids2 = [m.message_id for m in run2]
        assert ids2 == [f"gmail:gm{i}" for i in range(41, 81)]
        assert cursor2["history_id"] == 89  # gm80 → entry 10+79
        assert len(cursor2["seen"]) == 80
        # union of both runs: the first 80 messages, exactly once
        assert [m.message_id for m in run1] + ids2 == [
            f"gmail:gm{i}" for i in range(1, 81)
        ]

    async def test_history_newer_than_exhaustion_terminates(self, monkeypatch: Any) -> None:
        """A8: keep paging while ``historyNewerThan`` advances (five 25-entry
        pages); a falsy terminal page ends the loop — no infinite paging, no
        skips across page boundaries."""
        entries, messages = self._paged_world()
        fake = FakeGmailClient(entries=entries, messages=messages, start_id=10)
        _install_gmail(monkeypatch, fake)
        items, cursor = await GmailClient(max_items=200).run(
            _connector(), {}, credentials={"access_token": "tok"}
        )
        assert len(items) == 125  # every page, no skips at the seams
        assert cursor["history_id"] == 134
        # paged through every page; the terminal page returned falsy
        # historyNewerThan and ended the loop (5 calls, not a 6th)
        assert fake.history_calls == [10, 35, 60, 85, 110]


class TestGmailExpiredHistory:
    async def test_expired_history_id_triggers_bounded_full_resync(
        self, monkeypatch: Any
    ) -> None:
        """A3/A9: saved history id expired (404) → history.start baseline +
        bounded messages fetch (deduped by the durable seen-list) +
        incremental from the baseline. New messages are delivered exactly
        once; already-seen ones are not redelivered."""
        messages = {
            mid: (200, gmail_msg_resource(mid, f"s-{mid}", _date(i), _plain_payload(f"b-{mid}")))
            for i, mid in enumerate(("m1", "m2", "m3", "m4", "m5"), start=1)
        }
        fake = FakeGmailClient(
            entries=[_gentry(1, 100, "m4"), _gentry(2, 100, "m5")],
            messages=messages,
            start_id=100,
            expired_ids=frozenset({90}),
            inbox_message_ids=["m1", "m2", "m3"],
        )
        _install_gmail(monkeypatch, fake)
        items, cursor = await GmailClient(max_items=25).run(
            _connector(),
            {
                "history_id": 90,
                "account": _ACCOUNT.lower(),
                "seen": [
                    f"{_ACCOUNT.lower()}:gmail:m1",
                    f"{_ACCOUNT.lower()}:gmail:m3",
                ],
            },
            credentials={"access_token": "tok"},
        )
        # m2 (unseen) is the only full-resync delivery; m1/m3 suppressed by
        # the durable seen-list; m4/m5 arrive exactly once via incremental.
        assert [m.message_id for m in items] == ["gmail:m2", "gmail:m4", "gmail:m5"]
        assert fake.start_calls == 1
        assert cursor["history_id"] == 102
        assert cursor["account"] == _ACCOUNT.lower()
        assert set(cursor["seen"]) == {
            f"{_ACCOUNT.lower()}:gmail:m{n}" for n in (1, 2, 3, 4, 5)
        }

    async def test_deleted_message_404_skips_that_message_only(
        self, monkeypatch: Any
    ) -> None:
        """A9: a deleted message in the history stream skips that single
        message — not a poll failure — and the cursor advances past it."""
        messages = {
            "ok1": (200, gmail_msg_resource("ok1", "A", _date(1), _plain_payload("a"))),
            "ok2": (200, gmail_msg_resource("ok2", "B", _date(2), _plain_payload("b"))),
        }
        fake = FakeGmailClient(
            entries=[
                _gentry(1, 20, "ok1"),
                _gentry(2, 20, "gone"),
                _gentry(3, 20, "ok2"),
            ],
            messages=messages,  # "gone" absent → 404 on messages.get
            start_id=20,
        )
        _install_gmail(monkeypatch, fake)
        items, cursor = await GmailClient().run(
            _connector(),
            {"history_id": 20, "account": _ACCOUNT.lower()},
            credentials={"access_token": "tok"},
        )
        assert [m.message_id for m in items] == ["gmail:ok1", "gmail:ok2"]
        assert fake.fetched.count("gone") == 1  # fetched once, skipped
        assert cursor["history_id"] == 23  # advanced past the deleted entry
        assert len(cursor["seen"]) == 2


class TestGmailAccounting:
    async def test_two_accounts_with_overlapping_senders_are_keyed_apart(
        self, monkeypatch: Any
    ) -> None:
        """A4: two connectors (two workers) sharing one sender each get only
        their own messages — account keying, never From-header filtering —
        and each cursor is per-connection (no cross-account leak)."""
        shared = gmail_msg_resource("shared", "Ping", _date(1), _plain_payload("x"))
        work = FakeGmailClient(
            entries=[_gentry(0, 1, "w1"), _gentry(1, 1, "shared")],
            messages={"w1": (200, shared), "shared": (200, shared)},
            start_id=1,
        )
        _install_gmail(monkeypatch, work)
        work_conn = _connector(connector_id="c-work", mailbox="work@example.com")
        items_w, cursor_w = await GmailClient().run(
            work_conn, {}, credentials={"access_token": "tok-work"}
        )
        assert {m.message_id for m in items_w} == {"gmail:w1", "gmail:shared"}
        assert all(m.account == "work@example.com" for m in items_w)
        assert cursor_w["account"] == "work@example.com"
        assert cursor_w["history_id"] == 2
        assert all(
            k.startswith("work@example.com:") for k in cursor_w["seen"]
        )

        personal = FakeGmailClient(
            entries=[_gentry(0, 1, "p1"), _gentry(1, 1, "shared")],
            messages={"p1": (200, shared), "shared": (200, shared)},
            start_id=1,
        )
        _install_gmail(monkeypatch, personal)
        personal_conn = _connector(connector_id="c-personal", mailbox="personal@example.com")
        items_p, cursor_p = await GmailClient().run(
            personal_conn, {}, credentials={"access_token": "tok-personal"}
        )
        assert {m.message_id for m in items_p} == {"gmail:p1", "gmail:shared"}
        assert all(m.account == "personal@example.com" for m in items_p)
        assert cursor_p["account"] == "personal@example.com"
        # per-connection cursors: neither carries the other account's keying
        assert "personal@example.com" not in str(cursor_w)
        assert "work@example.com" not in str(cursor_p)


class TestGmailBodyDecoding:
    async def test_base64url_missing_padding_is_decoded_at_the_wire(
        self, monkeypatch: Any
    ) -> None:
        """A6: Gmail strips base64url padding — a top-level text/plain body
        with the padding removed (length % 4 == 2) decodes to the exact
        bytes."""
        # 10 bytes → 2 pad chars; Gmail strips them → 14 chars (len % 4 == 2)
        raw_no_pad = base64.urlsafe_b64encode(b"deadline!!").decode().rstrip("=")
        assert len(raw_no_pad) % 4 == 2
        resource = {
            "id": "enc1",
            "payload": {
                "mimeType": "text/plain; charset=utf-8",
                "bodyData": {"size": "1", "data": raw_no_pad},
                "headers": [
                    {"name": "From", "value": "a@b.c"},
                    {"name": "To", "value": _ACCOUNT},
                    {"name": "Subject", "value": "S"},
                    {"name": "Date", "value": _date(4)},
                    {"name": "Message-ID", "value": "<enc1@gmail>"},
                ],
            },
        }
        fake = FakeGmailClient(
            entries=[_gentry(0, 3, "enc1")],
            messages={"enc1": (200, resource)},
            start_id=3,
        )
        _install_gmail(monkeypatch, fake)
        items, _ = await GmailClient().run(
            _connector(),
            {"history_id": 3, "account": _ACCOUNT.lower()},
            credentials={"access_token": "tok"},
        )
        assert items[0].content_bytes == b"deadline!!"

    def test_b64url_decode_handles_all_missing_padding_cases(self) -> None:
        """A6 (direct): Gmail strips the trailing ``=`` padding — for every
        byte length (padding count 0/1/2) the unpadded form still decodes to
        the exact original bytes; a bad charset byte degrades with
        errors=replace; an unknown charset falls back to utf-8."""
        for n in range(0, 9):
            raw = b"x" * n
            enc = base64.urlsafe_b64encode(raw).decode()  # with padding
            assert _b64url_decode(enc.rstrip("=")) == raw.decode("utf-8")
        # % 4 == 3 specifically (one padding char stripped): 8 bytes →
        # 12 base64 chars incl. one '='; stripped → 11 (len % 4 == 3)
        enc3 = base64.urlsafe_b64encode("abcde☂".encode()).decode().rstrip("=")
        assert len(enc3) % 4 == 3
        assert _b64url_decode(enc3) == "abcde☂"
        # a part charset other than utf-8: 0x80 is € in windows-1252
        assert _b64url_decode("gA=", "windows-1252") == "€"
        # invalid utf-8 bytes → U+FFFD replacement (one per bad byte), never an
        # exception
        assert _b64url_decode(base64.urlsafe_b64encode(b"\xff\xfe").decode()) == "\ufffd\ufffd"
        # empty / garbage alphabet → empty, no exception
        assert _b64url_decode("") == ""
        assert _b64url_decode("!!!") == ""

    async def test_auth_403_maps_to_transport_auth_error(
        self, monkeypatch: Any
    ) -> None:
        fake = FakeGmailClient(entries=[], messages={})
        orig_get = fake.get

        async def get_403(url, *, headers=None, params=None):
            if url.endswith("/users/me/history.start"):
                return FakeGmailResp(403, {"error": "forbidden"})
            return await orig_get(url, headers=headers, params=params)

        fake.get = get_403  # type: ignore[method-assign]
        _install_gmail(monkeypatch, fake)
        with pytest.raises(TransportAuthError):
            await GmailClient().run(
                _connector(), {}, credentials={"access_token": "bad"}
            )


# ---------------------------------------------------------------------------
# IMAP tests (P0-B)
# ---------------------------------------------------------------------------


class TestImapFolderSelection:
    async def test_account_email_address_never_becomes_a_folder(
        self, monkeypatch: Any
    ) -> None:
        """B1/B5: the connector carries the account email address; run()
        SELECTs the configured folder (default INBOX) and CHECKS the
        response is OK before any UID SEARCH/FETCH."""
        fake = FakeImap(
            folders={"INBOX": {"uids": {101: imap_msg("S", "<i-101>", "body", _date(1))}}}
        )
        _install_imap(monkeypatch, fake)
        items, cursor = await ImapPollClient().run(
            _connector(), {}, credentials={"username": _ACCOUNT, "password": _SECRET}
        )
        assert fake.select_calls == ["INBOX"]  # never the email address
        assert fake.search_calls and fake.search_calls[0][0] == "INBOX"
        assert len(items) == 1
        assert items[0].account == _ACCOUNT.lower()
        assert items[0].message_id == "<i-101>"
        assert cursor == {
            "uid": 101,
            "uidvalidity": 1,
            "folder": "INBOX",
            "seen": [f"{_ACCOUNT.lower()}:<i-101>"],
        }

    async def test_invalid_folder_no_response_is_connect_error(
        self, monkeypatch: Any
    ) -> None:
        """B1: a server NO for the folder → TransportConnectError, no
        SEARCH/FETCH issued (raising means the worker persists no cursor
        advance)."""
        fake = FakeImap(folders={"INBOX": {"uids": {}}})
        _install_imap(monkeypatch, fake)
        with pytest.raises(TransportConnectError):
            await ImapPollClient().run(
                _connector(),
                {"folder": "NOPE"},
                credentials={"username": _ACCOUNT, "password": _SECRET},
            )
        assert fake.select_calls == ["NOPE"]
        assert fake.search_calls == []

    async def test_stored_folder_is_reselected_explicitly(self, monkeypatch: Any) -> None:
        """B5: a stored non-INBOX folder is SELECTed from the cursor, with
        the folder + validity carried back in the cursor."""
        archive_body = imap_msg("Archived", "<a-201>", "arch", _date(2))
        fake = FakeImap(
            folders={
                "INBOX": {"uids": {101: imap_msg("S", "<i-101>", "x", _date(1))}},
                "Archive": {"uids": {201: archive_body}, "validity": 7},
            }
        )
        _install_imap(monkeypatch, fake)
        items, cursor = await ImapPollClient().run(
            _connector(),
            {"uid": 0, "uidvalidity": 7, "folder": "Archive"},
            credentials={"username": _ACCOUNT, "password": _SECRET},
        )
        assert fake.select_calls == ["Archive"]
        assert len(items) == 1
        assert cursor == {
            "uid": 201,
            "uidvalidity": 7,
            "folder": "Archive",
            "seen": [f"{_ACCOUNT.lower()}:<a-201>"],
        }


class TestImapUidvalidity:
    async def test_uidvalidity_change_resets_that_folder_fresh(
        self, monkeypatch: Any
    ) -> None:
        """B3: stored validity (1) ≠ server validity (2) → fresh sync for
        that folder: ``UID SEARCH ALL`` (no stale ``last+1:*`` replay),
        cursor adopts the new validity."""
        fake = FakeImap(
            folders={
                "INBOX": {
                    "uids": {
                        301: imap_msg("Old", "<u-301>", "old", _date(1)),
                        302: imap_msg("New", "<u-302>", "new", _date(2)),
                    },
                    "validity": 2,
                }
            }
        )
        _install_imap(monkeypatch, fake)
        items, cursor = await ImapPollClient().run(
            _connector(),
            {"uid": 900, "uidvalidity": 1, "folder": "INBOX"},  # stale validity
            credentials={"username": _ACCOUNT, "password": _SECRET},
        )
        # reset ⇒ the search is ALL, NOT "901:*" (which would skip both UIDs)
        assert fake.search_calls == [("INBOX", "ALL")]
        assert [m.message_id for m in items] == ["<u-301>", "<u-302>"]
        assert cursor == {
            "uid": 302,
            "uidvalidity": 2,
            "folder": "INBOX",
            "seen": [
                f"{_ACCOUNT.lower()}:<u-301>",
                f"{_ACCOUNT.lower()}:<u-302>",
            ],
        }


class TestImapCursors:
    async def test_folder_cursors_are_independent(self, monkeypatch: Any) -> None:
        """B4: one account, two folders — each folder keeps its own UID
        position; selecting another folder never replays the first folder's
        position."""
        fake = FakeImap(
            folders={
                "INBOX": {
                    "uids": {101: imap_msg("A", "<f-101>", "a", _date(1))},
                    "validity": 1,
                },
                "Archive": {
                    "uids": {501: imap_msg("B", "<f-501>", "b", _date(2))},
                    "validity": 1,
                },
            }
        )
        _install_imap(monkeypatch, fake)
        client = ImapPollClient()

        _, cursor_inbox = await client.run(
            _connector(),
            {"folder": "INBOX"},
            credentials={"username": _ACCOUNT, "password": _SECRET},
        )
        assert cursor_inbox["folder"] == "INBOX"
        assert cursor_inbox["uid"] == 101

        # Archive's position starts fresh — NOT derived from INBOX's uid
        items_archive, cursor_archive = await client.run(
            _connector(),
            {"uid": 0, "uidvalidity": 1, "folder": "Archive"},
            credentials={"username": _ACCOUNT, "password": _SECRET},
        )
        assert [m.message_id for m in items_archive] == ["<f-501>"]
        assert cursor_archive["folder"] == "Archive"
        assert cursor_archive["uid"] == 501
        assert fake.search_calls[-1] == ("Archive", "1:*")  # fresh, not cross-folder

        # resuming INBOX from its own cursor searches the INBOX space only
        items_again, cursor_again = await client.run(
            _connector(),
            cursor_inbox,
            credentials={"username": _ACCOUNT, "password": _SECRET},
        )
        assert items_again == []
        assert cursor_again == cursor_inbox
        assert fake.search_calls[-1] == ("INBOX", "102:*")

    async def test_multiple_accounts_same_server_do_not_collide(
        self, monkeypatch: Any
    ) -> None:
        """B4: cursors are keyed per connection (the worker persists them by
        the connection's stored id) — the transport never mixes two
        accounts' positions: each run returns a cursor carrying only its own
        folder/uid, and a second account's run starts from its own cursor."""
        fake = FakeImap(
            folders={
                "INBOX": {
                    "uids": {
                        101: imap_msg("A", "<a101>", "a", _date(1)),
                        201: imap_msg("B", "<b201>", "b", _date(2)),
                    },
                    "validity": 1,
                }
            }
        )
        _install_imap(monkeypatch, fake)
        client = ImapPollClient()

        a_conn = _connector(connector_id="conn-a", mailbox="alice@example.com")
        b_conn = _connector(connector_id="conn-b", mailbox="bob@example.com")

        # alice's first poll stops at max_items=1 → uid 101
        items_a, cursor_a = await client.run(
            a_conn,
            {},
            credentials={"username": "alice@example.com", "password": _SECRET},
        )
        # with the default max_items=25 both uids are fetched in one poll;
        # to prove per-account positions, use a bounded client for alice.
        assert cursor_a["uid"] in (101, 201)

        bounded = ImapPollClient(max_items=1)
        _, cursor_a1 = await bounded.run(
            a_conn, {}, credentials={"username": "alice@example.com", "password": _SECRET}
        )
        assert cursor_a1["uid"] == 101

        # bob's first poll also starts from 0 — his own position
        _, cursor_b = await bounded.run(
            b_conn, {}, credentials={"username": "bob@example.com", "password": _SECRET}
        )
        assert cursor_b["uid"] == 101  # independent, identical fresh position
        assert cursor_a1["seen"] != [] and cursor_b["seen"] != []
        # the seen keys are per-account (the dedup key includes the account)
        assert cursor_a1["seen"][0].startswith("alice@example.com:")
        assert cursor_b["seen"][0].startswith("bob@example.com:")
        # resuming alice from her cursor does not replay what she had
        items_a2, cursor_a2 = await bounded.run(
            a_conn, cursor_a1, credentials={"username": "alice@example.com", "password": _SECRET}
        )
        assert [m.message_id for m in items_a2] == ["<b201>"]
        assert cursor_a2["uid"] == 201

    async def test_incremental_search_only_new_uids(self, monkeypatch: Any) -> None:
        """B5: first sync is ``UID SEARCH ALL``; a resumed cursor fetches
        only NEW uids via ``UID <last+1>:*`` — no stale UIDs, idempotent
        restart (same cursor out, no mail out)."""
        fake = FakeImap(
            folders={
                "INBOX": {
                    "uids": {
                        401: imap_msg("One", "<g-401>", "1", _date(1)),
                        402: imap_msg("Two", "<g-402>", "2", _date(2)),
                        403: imap_msg("Three", "<g-403>", "3", _date(3)),
                    },
                    "validity": 3,
                }
            }
        )
        _install_imap(monkeypatch, fake)
        client = ImapPollClient()
        items, cursor = await client.run(
            _connector(),
            {"folder": "INBOX"},
            credentials={"username": _ACCOUNT, "password": _SECRET},
        )
        assert [m.message_id for m in items] == ["<g-401>", "<g-402>", "<g-403>"]
        assert fake.search_calls == [("INBOX", "ALL")]
        # restart from the saved cursor: only uids > 403 qualify
        items2, cursor2 = await client.run(
            _connector(),
            cursor,
            credentials={"username": _ACCOUNT, "password": _SECRET},
        )
        assert items2 == []
        assert fake.search_calls[-1] == ("INBOX", "404:*")
        assert cursor2 == cursor  # idempotent: no advance without new mail

    async def test_uid_out_of_order_is_handled_by_seen_not_skip(
        self, monkeypatch: Any
    ) -> None:
        """B5: the transport delivers every uid it searches (UID order is the
        server's); a re-emitted / duplicate-Message-ID uid is NOT skipped
        here — dedup is the durable seen-list's job (the list stays
        idempotent)."""
        body = imap_msg("Re", "<dup-1>", "re", _date(1))
        fake = FakeImap(folders={"INBOX": {"uids": {1: body, 2: body}, "validity": 1}})
        _install_imap(monkeypatch, fake)
        items, cursor = await ImapPollClient().run(
            _connector(),
            {"folder": "INBOX"},
            credentials={"username": _ACCOUNT, "password": _SECRET},
        )
        # both uids are delivered — none is silently skipped for "looking"
        # like a duplicate: the worker's seen-list dedups
        assert [m.message_id for m in items] == ["<dup-1>", "<dup-1>"]
        assert cursor["seen"] == [f"{_ACCOUNT.lower()}:<dup-1>"]  # capped/idempotent
        assert cursor["uid"] == 2


class TestImapErrorMapping:
    async def test_login_auth_failed_maps_to_auth_error(self, monkeypatch: Any) -> None:
        import backend.connectors.mail.mail_transport as mt

        class _BadLogin(FakeImap):
            def login(self, user, password):
                return "AUTHENTICATIONFAILED", [b"NO"]

        monkeypatch.setattr(
            mt.imaplib,
            "IMAP4_SSL",
            lambda host, port, timeout=None: _BadLogin(folders={}),
        )
        with pytest.raises(TransportAuthError):
            await ImapPollClient().run(
                _connector(), {}, credentials={"username": _ACCOUNT, "password": _SECRET}
            )

    async def test_connect_failure_maps_to_connect_error(self, monkeypatch: Any) -> None:
        import backend.connectors.mail.mail_transport as mt

        def _boom(host, port, timeout=None):
            raise ConnectionError("no route to host")

        monkeypatch.setattr(mt.imaplib, "IMAP4_SSL", _boom)
        with pytest.raises(TransportConnectError):
            await ImapPollClient().run(
                _connector(), {}, credentials={"username": _ACCOUNT, "password": _SECRET}
            )
