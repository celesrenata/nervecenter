"""Transport-correctness tests for the durable mail plane (P0-A + P0-B).

These pin the *pinned cursor contract* the parallel mail-worker owns:

- IMAP (``imap_seq``): ``{"uid": int, "uidvalidity": int, "folder": str,
  "seen": [...]}`` — cursor is per (connection, folder); the folder is
  SELECTed explicitly (never derived from the mailbox email address); a
  UIDVALIDITY change resets that folder to a fresh sync.
- Gmail (``gmail_history``): ``{"history_id": int, "account": str,
  "seen": [...]}`` — resume reuses the saved valid history id (never ``+1``);
  an expired id (400/404) triggers a bounded full resync.

Wires are faked at the boundary only: a fake ``imaplib``-shaped object for
the IMAP transport and a fake ``httpx.AsyncClient`` for the Gmail transport
(stateless, page-based, mirroring the real ``history.startHistoryId``
inclusive semantics). The real client code runs end to end: ``run()`` is
idempotent for a given cursor — re-running from the saved cursor re-yields
the same items, no skips, no duplicates.
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from typing import Any

import pytest

from backend.api.models import MailConnector
from backend.connectors.mail.mail_transport import (
    ERR_INSUFFICIENT_SCOPE,
    ERR_PROVIDER_API_DISABLED,
    GmailClient,
    ImapPollClient,
    TransportAuthError,
    TransportConnectError,
    TransportProviderError,
    _b64url_decode,
    _extract_text_body,
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
    # Real Gmail wire shape: payload.body is the Body resource; the
    # base64url body sits at body.data (no bodyData object in the API).
    return {
        "mimeType": "text/plain; charset=utf-8",
        "body": {"size": "1", "data": base64.urlsafe_b64encode(text.encode()).decode()},
    }


def _gentry(idx: int, base_id: int, mid: str) -> dict[str, Any]:
    return {"id": base_id + idx, "messages": [{"id": mid}]}


def _gresource(mid: str, i: int) -> dict[str, Any]:
    return gmail_msg_resource(mid, f"subj-{i}", _date(i), _plain_payload(f"body-{i}"))


def _three_msg_entry(idx: int, base_id: int, mids: list[str]) -> dict[str, Any]:
    return {"id": base_id + idx, "messages": [{"id": m} for m in mids]}


class FakeGmailClient:
    """httpx.AsyncClient-shaped fake for the **real** Gmail v1 wire.

    Mirrors the pinned contract the transport talks to:

    - ``GET /users/me/profile`` is the **baseline probe** (``_history_head``)
      and returns the current mailbox ``historyId`` (``head_history_id``).
      This is the documented baseline source: ``history.list`` *requires*
      ``startHistoryId`` and 400s without it, so a bare ``history.list`` can
      never establish the first-ever baseline. ``profile_status`` /
      ``profile_error`` let a test force a 401/403 (and Google error body) on
      the baseline call. The bare ``history.list`` (no ``startHistoryId``)
      branch 400s like the real API — proving the transport no longer uses it
      for the baseline.
    - ``GET /users/me/history`` with ``startHistoryId`` is the **incremental
      poll**; ``startHistoryId`` is EXCLUSIVE (records strictly after it) and
      ``nextPageToken`` is the only page-continuation signal.
    - ``GET /users/me/messages`` (``q=in:inbox``) is the **bounded full
      resync** list used after an expired baseline.
    - ``GET /users/me/messages/{id}?format=full`` fetches one message.
    - A ``startHistoryId`` in ``expired_ids`` yields ``expired_status``
      (400 or 404) — the wire signature the transport maps to the resync.
    """

    def __init__(
        self,
        *,
        entries: list[dict[str, Any]] | None = None,
        messages: dict[str, tuple[int, dict[str, Any]]] | None = None,
        head_history_id: int = 0,
        expired_ids: frozenset[int] = frozenset(),
        expired_status: int = 404,
        inbox_message_ids: list[str] | None = None,
        page_size: int = 500,
        profile_status: int = 200,
        profile_error: dict[str, Any] | None = None,
    ) -> None:
        self._entries = sorted(entries or [], key=lambda e: int(e["id"]))
        self._messages = dict(messages or {})
        if head_history_id:
            self._head = head_history_id
        elif self._entries:
            self._head = min(int(e["id"]) for e in self._entries)
        else:
            self._head = 0
        self._expired = frozenset(expired_ids)
        self._expired_status = expired_status
        self._inbox_ids = list(inbox_message_ids or [])
        self._page_size = page_size
        self._profile_status = profile_status
        self._profile_error = profile_error
        self.history_calls: list[int] = []
        self.history_params: list[dict[str, Any]] = []
        self.head_calls = 0
        self.profile_calls = 0
        self.bare_history_calls = 0
        self.fetched: list[str] = []
        self.inbox_calls = 0

    def _history_window(self, start: int) -> dict[str, Any]:
        stream = [e for e in self._entries if int(e["id"]) > start]
        window = stream[: self._page_size]
        return {"history": window}

    async def __aenter__(self) -> "FakeGmailClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> FakeGmailResp:
        p = dict(params or {})
        if url.endswith("/users/me/profile"):
            # Baseline probe (``_history_head``): the documented source of the
            # current mailbox ``historyId``.
            self.profile_calls += 1
            self.head_calls += 1  # kept for existing assertions on head_calls
            if self._profile_status != 200:
                return FakeGmailResp(
                    self._profile_status,
                    self._profile_error
                    or {"error": {"code": self._profile_status, "message": "denied"}},
                )
            return FakeGmailResp(200, {"historyId": self._head})
        if url.endswith("/users/me/history"):
            if "startHistoryId" in p:
                start = int(p["startHistoryId"])
                self.history_calls.append(start)
                self.history_params.append(p)
                if start in self._expired:
                    return FakeGmailResp(
                        self._expired_status,
                        {"error": {"code": self._expired_status, "message": "expired"}},
                    )
                return FakeGmailResp(200, self._history_window(start))
            # Bare ``history.list`` (no ``startHistoryId``): the real Gmail v1
            # API rejects this with 400 INVALID_ARGUMENT. The transport must
            # NOT use it for the baseline — any hit here is a regression.
            self.bare_history_calls += 1
            return FakeGmailResp(
                400,
                {
                    "error": {
                        "code": 400,
                        "status": "INVALID_ARGUMENT",
                        "message": "Missing/invalid parameter: startHistoryId",
                        "errors": [{"reason": "invalidArgument"}],
                    }
                },
            )
        if url.endswith("/users/me/messages"):
            self.inbox_calls += 1
            return FakeGmailResp(200, {"messages": [{"id": m} for m in self._inbox_ids]})
        if "/messages/" in url:
            mid = url.rsplit("/", 1)[-1]
            self.fetched.append(mid)
            entry = self._messages.get(mid)
            if entry is None:
                return FakeGmailResp(404, {"error": {"code": 404, "message": "notFound"}})
            status, resource = entry
            return FakeGmailResp(status, resource)
        raise AssertionError(f"unexpected url {url}")


def _install_gmail(monkeypatch: Any, fake: FakeGmailClient) -> None:
    import backend.connectors.mail.mail_transport as mt

    def factory(**kw: Any) -> FakeGmailClient:
        return fake

    monkeypatch.setattr(mt.httpx, "AsyncClient", factory)


# ---------------------------------------------------------------------------
# Gmail tests (P0-A)
# ---------------------------------------------------------------------------


class TestGmailInitialSync:
    async def test_initial_sync_uses_captured_baseline_and_no_invalid_types(
        self, monkeypatch: Any
    ) -> None:
        """A1/A2: first run (empty cursor) probes the current mailbox
        ``historyId`` (no ``history.start`` endpoint in v1), full-syncs INBOX
        under that baseline, then polls EXCLUSIVELY after it. The invalid
        ``historyTypes`` value is never on the wire."""
        # inbox holds m1 (entry 10 == baseline); m2 (entry 11) is the only
        # post-baseline change, so the initial sync must deliver it
        # incrementally after the bounded INBOX sync.
        fake = FakeGmailClient(
            entries=[_gentry(1, 10, "m2")],
            messages={
                "m1": (200, _gresource("m1", 1)),
                "m2": (200, _gresource("m2", 2)),
            },
            head_history_id=10,
            inbox_message_ids=["m1"],
        )
        _install_gmail(monkeypatch, fake)
        items, cursor = await GmailClient().run(
            _connector(), {}, credentials={"access_token": "tok"}
        )
        # A2: the baseline was captured from users/me/profile (NOT a bare
        # history.list, which the real API 400s without startHistoryId).
        assert fake.head_calls == 1
        assert fake.profile_calls == 1
        assert fake.bare_history_calls == 0
        # the bounded full resync fetched the INBOX list before polling.
        assert fake.inbox_calls == 1
        # the saved cursor IS the captured baseline (no +1); polling is
        # EXCLUSIVE after it, so only m2 (entry 11) is a post-baseline change.
        assert fake.history_calls == [10]
        assert all("historyTypes" not in p for p in fake.history_params)
        assert [m.message_id for m in items] == ["gmail:m1", "gmail:m2"]
        assert cursor == {
            "history_id": 11,
            "account": _ACCOUNT.lower(),
            "seen": [
                f"{_ACCOUNT.lower()}:gmail:m1",
                f"{_ACCOUNT.lower()}:gmail:m2",
            ],
        }
        # the transient access_token is never in the cursor
        assert "tok" not in str(cursor)

    async def test_first_sync_succeeds_when_bare_history_list_would_400(
        self, monkeypatch: Any
    ) -> None:
        """Regression (first-ever sync baseline): a bare ``history.list``
        (no ``startHistoryId``) 400s on the real Gmail API. The first sync
        MUST still succeed by taking the baseline historyId from
        ``users/me/profile`` and ingesting INBOX — it must NOT raise and must
        NOT produce an ``ingest_failed``-class crash.

        This is the exact production failure:
        ``_HistoryExpired: gmail history baseline -> 400`` escaping ``run()``.
        """
        fake = FakeGmailClient(
            entries=[],  # no post-baseline changes — the full sync is the payload
            messages={
                "mp1": (200, _gresource("mp1", 1)),
                "mp2": (200, _gresource("mp2", 2)),
            },
            head_history_id=8392969,  # the live profile historyId shape
            inbox_message_ids=["mp1", "mp2"],
        )
        _install_gmail(monkeypatch, fake)

        items, cursor = await GmailClient().run(
            _connector(), {}, credentials={"access_token": "tok"}
        )

        # The first sync ingested the INBOX items (did not raise / ingest_failed).
        assert [m.message_id for m in items] == ["gmail:mp1", "gmail:mp2"]
        # The baseline historyId came from the PROFILE endpoint, never the
        # bare history.list (which would 400).
        assert fake.profile_calls == 1
        assert fake.bare_history_calls == 0
        assert cursor["history_id"] == 8392969
        assert cursor["account"] == _ACCOUNT.lower()

    async def test_first_sync_baseline_400_degrades_to_connect_error_not_crash(
        self, monkeypatch: Any
    ) -> None:
        """Defense-in-depth: if the first-sync baseline/resync path somehow
        hits a bare ``history.list`` 400 (an internal ``_HistoryExpired``),
        it degrades to a ``TransportConnectError`` — never an uncaught
        ``_HistoryExpired`` bubbling out of ``run()`` as ``ingest_failed``.

        We force this by making the fake 400 on the baseline fetch: the
        profile endpoint is bypassed by routing the baseline to the bare
        history.list (simulated here by a profile that itself raises the
        internal expiry via a 400-shaped history.list path).
        """
        # Monkeypatch _history_head to raise the internal _HistoryExpired so we
        # exercise run()'s first-sync symmetry directly (the real baseline now
        # uses profile, so a genuine 400 here is a defense-in-depth concern).
        import backend.connectors.mail.mail_transport as mt

        fake = FakeGmailClient(messages={}, head_history_id=5, inbox_message_ids=[])
        _install_gmail(monkeypatch, fake)

        async def boom(self: Any, client: Any, token: str) -> int:
            raise mt._HistoryExpired("gmail history baseline -> 400")

        monkeypatch.setattr(mt.GmailClient, "_history_head", boom)

        with pytest.raises(TransportConnectError):
            await GmailClient().run(
                _connector(), {}, credentials={"access_token": "tok"}
            )

    async def test_incoming_from_external_sender_is_accepted(
        self, monkeypatch: Any
    ) -> None:
        """A4: the From header never gates delivery — the sender is NOT the
        account, yet the message is delivered and keyed on the account."""
        fake = FakeGmailClient(
            entries=[_gentry(1, 5, "m1")],
            messages={
                "m1": (200, gmail_msg_resource("m1", "Hello", _date(1), _plain_payload("hi")))
            },
            head_history_id=5,
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
        """A3: resume polls EXCLUSIVELY from the SAVED valid id (history ids
        are non-contiguous — an inclusive resume would replay, a +1 resume
        would skip; only strictly-after is correct)."""
        fake = FakeGmailClient(
            entries=[_gentry(0, 41, "m1"), _gentry(0, 44, "m2")],
            messages={"m1": (200, _gresource("m1", 1)), "m2": (200, _gresource("m2", 2))},
            head_history_id=44,
        )
        _install_gmail(monkeypatch, fake)
        items, cursor = await GmailClient().run(
            _connector(),
            {"history_id": 40, "account": _ACCOUNT.lower()},
            credentials={"access_token": "tok"},
        )
        # resume: no baseline probe
        assert fake.head_calls == 0
        # polled from the saved id itself (41 and 44 both delivered — the gap
        # at 42/43 is irrelevant under the exclusive contract)
        assert fake.history_calls == [40]
        assert [m.message_id for m in items] == ["gmail:m1", "gmail:m2"]
        assert cursor["history_id"] == 44


class TestGmailAuthClassification:
    """403 reason classification: a valid token hitting a disabled API or an
    insufficient scope is NOT auth-expiry and must map to a distinct code
    carrying Google's reason — while a genuine 401 still means re-auth."""

    async def test_403_access_not_configured_is_provider_api_disabled(
        self, monkeypatch: Any
    ) -> None:
        body = {
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "message": "Gmail API has not been used in project 123 before or it is disabled.",
                "errors": [{"reason": "accessNotConfigured"}],
            }
        }
        fake = FakeGmailClient(
            head_history_id=5,
            inbox_message_ids=[],
            profile_status=403,
            profile_error=body,
        )
        _install_gmail(monkeypatch, fake)
        with pytest.raises(TransportProviderError) as excinfo:
            await GmailClient().run(_connector(), {}, credentials={"access_token": "tok"})
        exc = excinfo.value
        # Distinct, non-auth code; Google's reason preserved in the message.
        assert exc.error_code == ERR_PROVIDER_API_DISABLED
        assert "accessNotConfigured" in str(exc)

    async def test_403_insufficient_scope_is_insufficient_scope_code(
        self, monkeypatch: Any
    ) -> None:
        body = {
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "message": "Request had insufficient authentication scopes.",
                "errors": [{"reason": "insufficientPermissions"}],
            }
        }
        fake = FakeGmailClient(
            head_history_id=5,
            inbox_message_ids=[],
            profile_status=403,
            profile_error=body,
        )
        _install_gmail(monkeypatch, fake)
        with pytest.raises(TransportProviderError) as excinfo:
            await GmailClient().run(_connector(), {}, credentials={"access_token": "tok"})
        assert excinfo.value.error_code == ERR_INSUFFICIENT_SCOPE

    async def test_403_scope_via_status_field_is_insufficient_scope(
        self, monkeypatch: Any
    ) -> None:
        # Some scope failures surface only in the top-level status.
        body = {
            "error": {
                "code": 403,
                "status": "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                "message": "Request had insufficient authentication scopes.",
                "errors": [],
            }
        }
        fake = FakeGmailClient(
            head_history_id=5,
            inbox_message_ids=[],
            profile_status=403,
            profile_error=body,
        )
        _install_gmail(monkeypatch, fake)
        with pytest.raises(TransportProviderError) as excinfo:
            await GmailClient().run(_connector(), {}, credentials={"access_token": "tok"})
        assert excinfo.value.error_code == ERR_INSUFFICIENT_SCOPE

    async def test_genuine_401_still_maps_to_auth_error(self, monkeypatch: Any) -> None:
        fake = FakeGmailClient(
            head_history_id=5,
            inbox_message_ids=[],
            profile_status=401,
            profile_error={"error": {"code": 401, "message": "Invalid Credentials"}},
        )
        _install_gmail(monkeypatch, fake)
        with pytest.raises(TransportAuthError):
            await GmailClient().run(_connector(), {}, credentials={"access_token": "tok"})

    async def test_generic_403_without_reason_maps_to_auth_error(
        self, monkeypatch: Any
    ) -> None:
        # A 403 with no accessNotConfigured / scope reason is a
        # permission-revoked / forbidden case → re-auth (TransportAuthError).
        fake = FakeGmailClient(
            head_history_id=5,
            inbox_message_ids=[],
            profile_status=403,
            profile_error={"error": {"code": 403, "message": "forbidden", "errors": []}},
        )
        _install_gmail(monkeypatch, fake)
        with pytest.raises(TransportAuthError):
            await GmailClient().run(_connector(), {}, credentials={"access_token": "tok"})


class TestGmailMultipart:
    async def test_multipart_alternative_decodes_plain_text_exactly(
        self, monkeypatch: Any
    ) -> None:
        """A5/A6: multipart/alternative — prefer text/plain over text/html;
        exact decoded body bytes; seen-list advanced in the cursor."""
        exact = "Deadline: Thu, 30 Oct 2026 17:00 - send report [chart]"
        exact_b64 = base64.urlsafe_b64encode(exact.encode("utf-8")).decode()
        html_b64 = base64.urlsafe_b64encode(
            b"<html><body><b>Deadline</b> (html variant)</body></html>"
        ).decode()
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": html_b64}},
                {"mimeType": "text/plain", "body": {"data": exact_b64}},
            ],
        }
        fake = FakeGmailClient(
            entries=[_gentry(1, 3, "m1")],
            messages={
                "m1": (200, gmail_msg_resource("m1", "Deadline", _date(1), payload))
            },
            head_history_id=3,
        )
        _install_gmail(monkeypatch, fake)
        items, cursor = await GmailClient().run(
            _connector(),
            {"history_id": 3, "account": _ACCOUNT.lower()},
            credentials={"access_token": "tok"},
        )
        assert len(items) == 1
        assert items[0].content_bytes == exact.encode("utf-8")
        assert b"<html>" not in items[0].content_bytes
        assert cursor["seen"] == [f"{_ACCOUNT.lower()}:gmail:m1"]

    async def test_nested_multipart_mixed_falls_back_to_html_when_no_plain(
        self, monkeypatch: Any
    ) -> None:
        """A6: a nested multipart/mixed with only an html leaf falls back to
        the html body (never empty)."""
        html = "<html><body>report</body></html>"
        html_b64 = base64.urlsafe_b64encode(html.encode()).decode()
        outer = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [{"mimeType": "text/html", "body": {"data": html_b64}}],
                }
            ],
        }
        fake = FakeGmailClient(
            entries=[_gentry(1, 4, "m1")],
            messages={"m1": (200, gmail_msg_resource("m1", "R", _date(1), outer))},
            head_history_id=4,
        )
        _install_gmail(monkeypatch, fake)
        items, _c = await GmailClient().run(
            _connector(),
            {"history_id": 4, "account": _ACCOUNT.lower()},
            credentials={"access_token": "tok"},
        )
        assert len(items) == 1
        assert items[0].content_bytes.decode() == html


class TestGmailWireShape:
    def test_extract_reads_body_data_not_bodydata(self) -> None:
        # Real Gmail v1 puts the base64url body at payload.body.data; there is
        # no bodyData object. The extractor must read body.data.
        data = base64.urlsafe_b64encode(b"hello").decode()
        payload = {
            "mimeType": "text/plain",
            "body": {"data": data, "size": "5"},
        }
        assert _extract_text_body(payload) == "hello"

    def test_multipart_prefers_plain_from_body_data(self) -> None:
        html_b64 = base64.urlsafe_b64encode(b"<b>html</b>").decode()
        plain_b64 = base64.urlsafe_b64encode(b"plain wins").decode()
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": html_b64}},
                {"mimeType": "text/plain", "body": {"data": plain_b64}},
            ],
        }
        assert _extract_text_body(payload) == "plain wins"


class TestGmailPagination:
    async def test_restart_after_partial_page_yields_no_skips_or_dupes(
        self, monkeypatch: Any
    ) -> None:
        """A8: max_items (40) smaller than the 125-message stream → the cap
        record is HELD in the saved cursor (the record just before the one
        whose last message hit the cap); restarting (exclusive
        ``startHistoryId``) re-yields the cap record and the durable
        seen-list suppresses what was already delivered — so only the
        unprocessed tail is delivered: no skips, no duplicates."""
        # 125 messages, one per entry: entry ids 10..134, message ids
        # gm1..gm125 (gm_k sits in entry 9+k). A pre-existing saved cursor at
        # the mailbox head (entry 10) makes delivery pure incremental
        # polling — no first-sync INBOX resync, no baseline probe.
        entries = [_gentry(i, 10, f"gm{i + 1}") for i in range(125)]
        messages = {f"gm{i + 1}": (200, _gresource(f"gm{i + 1}", i + 1)) for i in range(125)}
        fake = FakeGmailClient(entries=entries, messages=messages, head_history_id=10)
        _install_gmail(monkeypatch, fake)
        client = GmailClient(max_items=40)
        run1, cursor1 = await client.run(
            _connector(),
            {"history_id": 10, "account": _ACCOUNT.lower()},
            credentials={"access_token": "tok"},
        )
        # resume: no baseline probe, no INBOX resync, single poll from saved
        assert fake.head_calls == 0
        assert fake.inbox_calls == 0
        assert fake.history_calls == [10]
        assert len(run1) == 40
        # exclusive start=10 → gm2..gm41 (gm1 sits AT the saved cursor)
        assert [m.message_id for m in run1] == [f"gmail:gm{i}" for i in range(2, 42)]
        # gm41 sits in entry 50; the cap is hit on that record, so the record
        # is held — re-yielded on resume and suppressed by the seen-list →
        # cursor == 49
        assert cursor1["history_id"] == 49
        assert cursor1["seen"] == [
            f"{_ACCOUNT.lower()}:gmail:gm{i}" for i in range(2, 42)
        ]
        assert cursor1["account"] == _ACCOUNT.lower()

        # "restart": fresh client, same world; exclusive resume after entry 49
        # re-yields entry 50 (gm41 suppressed by the seen-list) and delivers
        # only the unprocessed tail gm42..gm81 — nothing at or before the
        # saved cursor is replayed and no seen-list entry is re-delivered.
        fake2 = FakeGmailClient(entries=entries, messages=messages, head_history_id=134)
        _install_gmail(monkeypatch, fake2)
        client2 = GmailClient(max_items=40)
        run2, cursor2 = await client2.run(
            _connector(), cursor1, credentials={"access_token": "tok"}
        )
        assert [m.message_id for m in run2] == [f"gmail:gm{i}" for i in range(42, 82)]
        # gm81 sits in entry 90; the cap is hit on that record → it is held →
        # cursor == 89
        assert cursor2["history_id"] == 89

    async def test_cap_hit_mid_record_does_not_skip_undelivered_tail(
        self, monkeypatch: Any
    ) -> None:
        """A8/A9: when the ``max_items`` cap is reached in the middle of a
        history record, the cursor must NOT advance past that record — a
        restart (exclusive ``startHistoryId``) re-yields the record and the
        seen-list delivers only its undelivered tail instead of skipping it."""
        # entry 51 holds three messages; with max_items=2 the cap is hit
        # after delivering gm1, gm2 — gm3 sits undelivered in the same record.
        # Delivery is pure incremental polling seeded at entry 49 (no
        # first-sync INBOX resync).
        entries = [
            _three_msg_entry(1, 50, ["gm1", "gm2", "gm3"]),
            _gentry(2, 50, "gm4"),
        ]
        messages = {
            "gm1": (200, _gresource("gm1", 1)),
            "gm2": (200, _gresource("gm2", 2)),
            "gm3": (200, _gresource("gm3", 3)),
            "gm4": (200, _gresource("gm4", 4)),
        }
        fake = FakeGmailClient(entries=entries, messages=messages, head_history_id=50)
        _install_gmail(monkeypatch, fake)
        client = GmailClient(max_items=2)
        run1, cursor1 = await client.run(
            _connector(),
            {"history_id": 49, "account": _ACCOUNT.lower()},
            credentials={"access_token": "tok"},
        )
        # first two messages delivered; gm3 (same record) NOT yet
        assert [m.message_id for m in run1] == ["gmail:gm1", "gmail:gm2"]
        # The cap hit the very first record yielded after the cursor, so no
        # record was fully consumed — the cursor stays at the start (49).
        # Record 51's undelivered tail survives.
        assert cursor1["history_id"] == 49

        # run 2: the exclusive resume re-yields record 51; the seen-list
        # suppresses gm1/gm2 and delivers the undelivered tail gm3 — NOT
        # skipped. gm4 (record 52) hits the cap, so record 52 is held →
        # cursor == 51.
        fake2 = FakeGmailClient(entries=entries, messages=messages, head_history_id=51)
        _install_gmail(monkeypatch, fake2)
        client2 = GmailClient(max_items=2)
        run2, cursor2 = await client2.run(
            _connector(), cursor1, credentials={"access_token": "tok"}
        )
        assert [m.message_id for m in run2] == ["gmail:gm3", "gmail:gm4"]
        assert cursor2["history_id"] == 51

        # run 3: resume after 51 re-yields record 52 (gm4 suppressed by the
        # seen-list); nothing is re-delivered and the cursor advances to 52.
        fake3 = FakeGmailClient(entries=entries, messages=messages, head_history_id=51)
        _install_gmail(monkeypatch, fake3)
        client3 = GmailClient(max_items=2)
        run3, cursor3 = await client3.run(
            _connector(), cursor2, credentials={"access_token": "tok"}
        )
        assert run3 == []
        assert cursor3["history_id"] == 52


class TestGmailExpiredHistory:
    async def test_expired_history_id_triggers_bounded_full_resync(
        self, monkeypatch: Any
    ) -> None:
        """A3/A9: saved history id expired (404) → baseline probe + bounded
        INBOX full resync (deduped by the durable seen-list) + incremental
        poll from the fresh baseline. New messages are delivered exactly
        once; already-seen ones are not redelivered."""
        messages = {
            mid: (200, gmail_msg_resource(mid, f"s-{mid}", _date(i), _plain_payload(f"b-{mid}")))
            for i, mid in enumerate(("m1", "m2", "m3", "m4", "m5"), start=1)
        }
        fake = FakeGmailClient(
            entries=[_gentry(1, 100, "m4"), _gentry(2, 100, "m5")],
            messages=messages,
            head_history_id=100,
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
        assert cursor["history_id"] == 102
        assert cursor["seen"] == [
            f"{_ACCOUNT.lower()}:gmail:m1",
            f"{_ACCOUNT.lower()}:gmail:m3",
            f"{_ACCOUNT.lower()}:gmail:m2",
            f"{_ACCOUNT.lower()}:gmail:m4",
            f"{_ACCOUNT.lower()}:gmail:m5",
        ]
        # expired poll (start=90) → resync → poll from the fresh baseline (100)
        assert fake.history_calls == [90, 100]
        assert fake.inbox_calls == 1
        assert fake.head_calls == 1

    async def test_history_400_invalid_argument_triggers_bounded_full_resync(
        self, monkeypatch: Any
    ) -> None:
        """A9: a 400 INVALID_ARGUMENT on an expired ``startHistoryId`` maps
        to the same bounded resync (the transport maps both 400 and 404 to
        the resync path — a raw 400 must NOT crash the poll loop)."""
        fake = FakeGmailClient(
            entries=[_gentry(1, 1, "n1"), _gentry(2, 1, "n2")],
            messages={
                "n1": (200, _gresource("n1", 1)),
                "n2": (200, _gresource("n2", 2)),
            },
            head_history_id=1,
            expired_ids=frozenset({77}),
            expired_status=400,
            inbox_message_ids=[],
        )
        _install_gmail(monkeypatch, fake)
        items, cursor = await GmailClient().run(
            _connector(),
            {"history_id": 77, "account": _ACCOUNT.lower()},
            credentials={"access_token": "tok"},
        )
        assert [m.message_id for m in items] == ["gmail:n1", "gmail:n2"]
        assert cursor["history_id"] == 3
        # the 400 was mapped to a resync (probe + INBOX list), not a crash
        assert fake.head_calls == 1
        assert fake.inbox_calls == 1
        assert fake.history_calls == [77, 1]

    async def test_deleted_message_404_skips_that_message_only(
        self, monkeypatch: Any
    ) -> None:
        """A9: a deleted message (fetch → 404) skips THAT message, not the
        whole record — the sibling is still delivered and the cursor
        advances normally."""
        fake = FakeGmailClient(
            entries=[_gentry(1, 5, "gone"), _gentry(1, 5, "alive")],
            # no "gone" key -> fetch returns 404
            messages={"alive": (200, _gresource("alive", 1))},
            head_history_id=5,
        )
        _install_gmail(monkeypatch, fake)
        items, cursor = await GmailClient().run(
            _connector(),
            {"history_id": 5, "account": _ACCOUNT.lower()},
            credentials={"access_token": "tok"},
        )
        assert [m.message_id for m in items] == ["gmail:alive"]
        assert "gone" in fake.fetched
        assert cursor["history_id"] == 6


class TestGmailAccounting:
    async def test_two_accounts_with_overlapping_senders_are_keyed_apart(
        self, monkeypatch: Any
    ) -> None:
        """A4/A10: the same external sender writing to two mailboxes is keyed
        per-account — two distinct (account, message) keys, two distinct
        cursors, never collapsed."""
        work_mailbox = "work@corp.com"
        personal_mailbox = "personal@corp.com"

        work = FakeGmailClient(
            entries=[_gentry(1, 2, "wm1")],
            messages={"wm1": (200, _gresource("wm1", 1))},
            head_history_id=2,
        )
        personal = FakeGmailClient(
            entries=[_gentry(1, 2, "pm1")],
            messages={"pm1": (200, _gresource("pm1", 2))},
            head_history_id=2,
        )
        work_connector = _connector(mailbox=work_mailbox, account="Work")
        personal_connector = _connector(mailbox=personal_mailbox, account="Personal")

        _install_gmail(monkeypatch, work)
        work_items, work_cursor = await GmailClient().run(
            work_connector,
            {"history_id": 2, "account": work_mailbox.lower()},
            credentials={"access_token": "tok"},
        )
        _install_gmail(monkeypatch, personal)
        personal_items, personal_cursor = await GmailClient().run(
            personal_connector,
            {"history_id": 2, "account": personal_mailbox.lower()},
            credentials={"access_token": "tok"},
        )
        assert work_items[0].account == work_mailbox.lower()
        assert personal_items[0].account == personal_mailbox.lower()
        assert work_cursor["account"] == work_mailbox.lower()
        assert personal_cursor["account"] == personal_mailbox.lower()
        assert work_cursor["seen"] == [f"{work_mailbox.lower()}:gmail:wm1"]
        assert personal_cursor["seen"] == [f"{personal_mailbox.lower()}:gmail:pm1"]
        assert work_cursor["seen"] != personal_cursor["seen"]


class TestGmailBodyDecoding:
    async def test_base64url_missing_padding_is_decoded_at_the_wire(
        self, monkeypatch: Any
    ) -> None:
        """A6: the wire may carry a base64url body missing trailing '='
        padding; the transport pads it before decoding."""
        body_text = "no pad!"  # 7 chars → base64url carries two trailing '='
        full_b64 = base64.urlsafe_b64encode(body_text.encode()).decode()
        assert full_b64.endswith("==")
        unpadded = full_b64.rstrip("=")
        payload = {
            "mimeType": "text/plain",
            "body": {"data": unpadded, "size": str(len(body_text))},
        }
        fake = FakeGmailClient(
            entries=[_gentry(1, 7, "m1")],
            messages={"m1": (200, gmail_msg_resource("m1", "NP", _date(1), payload))},
            head_history_id=7,
        )
        _install_gmail(monkeypatch, fake)
        items, _c = await GmailClient().run(
            _connector(),
            {"history_id": 7, "account": _ACCOUNT.lower()},
            credentials={"access_token": "tok"},
        )
        assert len(items) == 1
        assert items[0].content_bytes.decode("utf-8") == body_text

    def test_b64url_decode_handles_all_missing_padding_cases(self) -> None:
        # _b64url_decode must tolerate a full, partially-stripped, or
        # fully-stripped base64url body.
        # 1-byte input encodes to 4 chars with two trailing '='.
        full = base64.urlsafe_b64encode(b"a").decode()
        assert full.endswith("==")
        assert _b64url_decode(full) == "a"              # full padding kept
        assert _b64url_decode(full[:-1]) == "a"         # one '=' stripped
        assert _b64url_decode(full[:-2]) == "a"         # both stripped
        # 6-byte input encodes to 8 base64 chars with no padding at all.
        unpadded_full = base64.urlsafe_b64encode(b"abcdef").decode()
        assert "=" not in unpadded_full
        assert _b64url_decode(unpadded_full) == "abcdef"

    async def test_auth_403_maps_to_transport_auth_error(
        self, monkeypatch: Any
    ) -> None:
        """A generic 403 (no accessNotConfigured / scope reason) on the
        baseline probe → TransportAuthError (never a resync, never a crash)."""
        fake = FakeGmailClient(
            entries=[_gentry(0, 1, "m1")],
            messages={"m1": (200, _gresource("m1", 1))},
            head_history_id=1,
            profile_status=403,
            profile_error={"error": {"code": 403, "message": "auth"}},
        )
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


class TestImapClassificationHeaders:
    async def test_bulk_classification_headers_are_captured(
        self, monkeypatch: Any
    ) -> None:
        """FEAT-001: a message carrying the bulk/automated classification
        headers (List-Unsubscribe / List-Id / Precedence / Auto-Submitted)
        has those headers present in the resulting MailItem.headers so the
        ingest worker can derive flags from them."""
        raw = (
            b"From: news@newsletter.example\r\n"
            b"To: user@example.com\r\n"
            b"Subject: Weekly digest\r\n"
            b"Date: Mon, 27 Oct 2026 09:00:00 +0000\r\n"
            b"Message-ID: <bulk-101@news>\r\n"
            b"List-Unsubscribe: <mailto:unsub@newsletter.example>\r\n"
            b"List-Id: Weekly <weekly.newsletter.example>\r\n"
            b"Precedence: bulk\r\n"
            b"Auto-Submitted: auto-generated\r\n"
            b"\r\n"
            b"body\n"
        )
        fake = FakeImap(folders={"INBOX": {"uids": {101: raw}}})
        _install_imap(monkeypatch, fake)
        items, _cursor = await ImapPollClient().run(
            _connector(), {}, credentials={"username": _ACCOUNT, "password": _SECRET}
        )
        assert len(items) == 1
        headers = items[0].headers
        assert headers["List-Unsubscribe"] == "<mailto:unsub@newsletter.example>"
        assert headers["List-Id"] == "Weekly <weekly.newsletter.example>"
        assert headers["Precedence"] == "bulk"
        assert headers["Auto-Submitted"] == "auto-generated"


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


class TestImapOffLoop:
    """The blocking imaplib session must run off the event loop.

    The connect → login → select → search → fetch → logout sequence is one
    synchronous unit (:meth:`ImapPollClient._poll_sync`) handed to
    ``asyncio.to_thread`` (repo precedent: ``backend.connectors.registry``
    ``make_imap_probe``) — a slow socket must not stall the shared loop.
    """

    async def test_poll_sync_is_routed_through_to_thread_and_off_loop(
        self, monkeypatch: Any
    ) -> None:
        import backend.connectors.mail.mail_transport as mt

        fake = FakeImap(
            folders={
                "INBOX": {
                    "uids": {101: imap_msg("A", "<off-1>", "a", _date(1))},
                    "validity": 1,
                },
            }
        )
        observed: dict[str, Any] = {}

        class _TracedImap(FakeImap):
            def select(self, mailbox, readonly=False):
                observed["thread"] = threading.get_ident()
                time.sleep(0.2)  # stand-in for blocking socket I/O
                return super().select(mailbox, readonly=readonly)

        monkeypatch.setattr(
            mt.imaplib,
            "IMAP4_SSL",
            lambda host, port, timeout=None: _TracedImap(folders=fake.folders),
        )

        # Spy on to_thread: run() must hand _poll_sync — and only it — to it.
        real_to_thread = asyncio.to_thread
        routed: list[tuple[Any, tuple[Any, ...]]] = []

        async def spy(func, /, *args, **kwargs):
            routed.append((func, args))
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(mt.asyncio, "to_thread", spy)

        connector = _connector()
        loop_thread = threading.get_ident()
        items, cursor = await ImapPollClient().run(
            connector,
            {"uid": 0, "uidvalidity": 1, "folder": "INBOX"},
            credentials={"username": _ACCOUNT, "password": _SECRET},
        )

        # The poll still behaves exactly as before (items + cursor contract).
        assert [m.message_id for m in items] == ["<off-1>"]
        assert cursor["uid"] == 101
        assert cursor["folder"] == "INBOX"
        # The blocking unit is _poll_sync, routed through to_thread...
        assert len(routed) == 1
        func, args = routed[0]
        assert func.__func__ is ImapPollClient._poll_sync
        assert args[0] is connector
        # ...and the blocking I/O actually executed off the event-loop thread.
        assert observed["thread"] is not None
        assert observed["thread"] != loop_thread

    async def test_slow_imap_poll_does_not_block_the_event_loop(
        self, monkeypatch: Any
    ) -> None:
        import backend.connectors.mail.mail_transport as mt

        class _SlowImap(FakeImap):
            def select(self, mailbox, readonly=False):
                time.sleep(0.4)  # simulate a slow / stalled socket
                return super().select(mailbox, readonly=readonly)

        monkeypatch.setattr(
            mt.imaplib,
            "IMAP4_SSL",
            lambda host, port, timeout=None: _SlowImap(
                folders={
                    "INBOX": {
                        "uids": {101: imap_msg("A", "<off-2>", "a", _date(1))},
                        "validity": 1,
                    },
                }
            ),
        )

        beats = [0]

        async def heartbeat():
            for _ in range(3):
                beats[0] += 1
                await asyncio.sleep(0.1)

        hb = asyncio.create_task(heartbeat())
        items, _ = await ImapPollClient().run(
            _connector(),
            {"uid": 0, "uidvalidity": 1, "folder": "INBOX"},
            credentials={"username": _ACCOUNT, "password": _SECRET},
        )
        await asyncio.gather(hb, return_exceptions=True)

        # While the 0.4s blocking select ran in a worker thread, the loop
        # stayed responsive and the heartbeat ticked 3x (t≈0/0.1/0.2). A
        # blocking run on the loop would leave exactly 1 tick.
        assert [m.message_id for m in items] == ["<off-2>"]
        assert beats[0] >= 3
