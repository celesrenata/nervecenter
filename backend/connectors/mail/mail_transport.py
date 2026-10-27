"""Mail transport seams for the durable ingest plane (Personal-Ops Recovery §4).

Spec §4 gives the mail worker two transports: *IMAP IDLE with timeout polling
fallback* and *Gmail history-resource incremental sync*. The backend polls
with bounded intervals (IDLE is a socket-hold optimisation that belongs to the
source-side relay, §47); what the backend owns is the **incremental read**:

- :class:`MailItem` — one fetched mail, ref-shaped: headers + content bytes.
  The bytes are *transient in this module*; the worker persists them to the
  object store and keeps only a content ref in the graph (§7: no raw content
  in the knowledge graph).
- :class:`MailTransport` — protocol seam. Production implementations:
  :class:`ImapPollClient` (stdlib :mod:`imaplib`) and :class:`GmailClient`
  (REST history resource over :mod:`httpx`); tests inject
  :class:`StubMailTransport`.
- :class:`TransportAuthError` — credential rejected (LOGIN/AUTHFAILED, or
  Gmail 401/403); the worker maps it onto ``auth_status = auth_expired``.
- :class:`TransportConnectError` — network/host/TLS failure; the worker maps
  it onto ``last_error_code = connect_failed``.

Cursor contract (the worker persists it via ``ingest_cursors``): the transport
returns ``cursor`` as an opaque JSON-serialisable dict, e.g.
``{"uid": 421}`` (IMAP ``imap_seq``) or ``{"history_id": 77, "account": …}``
(Gmail ``gmail_history``). A restarted worker passes the stored cursor back in
— ``run()`` must return *only* mail strictly after that position. The
credential is a *separate* parameter: the cursor is persisted in the database
and must never contain a secret (§4: workers never emit plaintext
credentials).
"""

from __future__ import annotations

import email
import email.utils
import imaplib
from email.message import Message
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from backend.api.models import MailConnector

__all__ = [
    "MailItem",
    "MailTransport",
    "TransportAuthError",
    "TransportConnectError",
    "ImapPollClient",
    "GmailClient",
    "StubMailTransport",
]


class TransportAuthError(Exception):
    """The mailbox rejected the credential (expired/invalid)."""


class TransportConnectError(Exception):
    """Host/port/TLS/network-level failure reaching the mailbox."""


@dataclass(frozen=True, slots=True)
class MailItem:
    """One fetched message.

    ``headers`` carries the minimal RFC-822 set the ingest path needs
    (``From``, ``To``, ``Subject``, ``Date``, ``Message-ID``,
    ``In-Reply-To``); ``content_bytes`` is the raw body (transient — the
    worker stores it in the object store and keeps only a content ref).
    ``timestamp`` is parsed to UTC from the ``Date`` header (server fallback
    when absent).
    """

    message_id: str
    account: str
    subject: str
    content_bytes: bytes
    timestamp: datetime
    headers: dict[str, str] = field(default_factory=dict)
    source: str = "imap"

    def subject_or_first_line(self) -> str:
        """Subject line / first non-empty line (§6 durable row ``subject``)."""
        if self.subject:
            return self.subject
        first = self.content_bytes.split(b"\n", 1)[0].decode("utf-8", "replace")
        return first.strip()[:200]


@runtime_checkable
class MailTransport(Protocol):
    """Incremental mail read seam (one call per poll).

    The persisted cursor (``ingest_cursors``) and the credential are
    deliberately separate inputs: ``cursor`` is a secret-free resume
    position, while ``credentials`` is transient secret material resolved
    from the durable secret store immediately before the poll and never
    persisted, logged, or echoed into the returned cursor (§4).
    """

    async def run(
        self,
        connector: MailConnector,
        cursor: dict[str, Any],
        *,
        credentials: dict[str, str] | None = None,
    ) -> tuple[list[MailItem], dict[str, Any]]:
        """Return ``(items, next_cursor)`` — only mail after ``cursor``.

        ``credentials`` keys (implementation-defined): ``username`` /
        ``password`` (IMAP) or ``access_token`` (Gmail). Raises
        :class:`TransportAuthError` / :class:`TransportConnectError` on
        failure.
        """
        ...


# -- IMAP ----------------------------------------------------------------------


def _parse_rfc2822(value: str | None) -> datetime | None:
    """Parse an RFC-2822/IMAP DATE header to UTC; ``None`` when unparseable."""
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _payload_bytes(message: Message, raw: bytes) -> bytes:
    """Decoded body bytes, falling back to the raw fetch bytes."""
    decoded = message.get_payload(decode=True)
    if isinstance(decoded, bytes):
        return decoded
    if isinstance(decoded, str):
        return decoded.encode("utf-8", "replace")
    return bytes(raw or b"")


class ImapPollClient:
    """Real IMAP incremental reader over stdlib :mod:`imaplib`.

    Bounded by ``timeout`` on connect and ``max_items`` per poll (the spec's
    "timeout polling fallback" — never an unbounded IDLE hold). Cursor shape:
    ``{"uid": <int>}`` — messages with UID strictly greater are returned.
    """

    def __init__(self, timeout: float = 10.0, max_items: int = 25) -> None:
        self._timeout = timeout
        self._max_items = max_items

    async def run(
        self,
        connector: MailConnector,
        cursor: dict[str, Any],
        *,
        credentials: dict[str, str] | None = None,
    ) -> tuple[list[MailItem], dict[str, Any]]:
        creds = credentials or {}
        last_uid = int(cursor.get("uid", 0) or 0)
        username = str(creds.get("username") or connector.mailbox)
        password = str(creds.get("password") or "")
        conn: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None
        try:
            if connector.ssl:
                conn = imaplib.IMAP4_SSL(connector.host, connector.port, timeout=self._timeout)
            else:
                conn = imaplib.IMAP4(connector.host, connector.port, timeout=self._timeout)
            code, _ = conn.login(username, password)
            if str(code).upper() != "OK":
                raise TransportAuthError(f"LOGIN -> {code}")
            if connector.mailbox and connector.mailbox.lower() != "inbox":
                conn.select(connector.mailbox)
            criterion = f"UID {last_uid + 1}:*"
            # The stdlib idiom is uid("SEARCH", None, "<criterion>") — the
            # second slot is the charset placeholder.
            typ, data = conn.uid("SEARCH", None, criterion)  # type: ignore[call-arg]
            if typ != "OK" or not data or not data[0]:
                return [], {"uid": last_uid}
            uids = data[0].split()[: self._max_items]
            uids = [int(uid) for uid in uids]
            items: list[MailItem] = []
            newest_uid = last_uid
            for uid in uids:
                typ, msg_data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                fetched = msg_data[0]
                raw = fetched[1] if isinstance(fetched, tuple) and len(fetched) > 1 else b""
                if isinstance(raw, str):
                    raw = raw.encode("utf-8", "replace")
                raw = bytes(raw or b"")
                message = email.message_from_bytes(raw)
                message_id = str(
                    message.get("Message-ID") or f"imap-{connector.mailbox}-{uid}"
                ).strip()
                ts = _parse_rfc2822(str(message.get("Date") or "")) or datetime.now(UTC)
                newest_uid = max(newest_uid, int(uid))
                items.append(
                    MailItem(
                        message_id=message_id,
                        account=username,
                        subject=str(message.get("Subject") or "").strip(),
                        content_bytes=_payload_bytes(message, raw),
                        timestamp=ts,
                        headers={
                            k: str(v)
                            for k, v in message.items()
                            if k in ("From", "To", "Subject", "Date", "Message-ID", "In-Reply-To")
                        },
                        source="imap",
                    )
                )
            return items, {"uid": newest_uid}
        except (OSError, ConnectionError, imaplib.IMAP4.abort) as exc:
            raise TransportConnectError(str(exc)) from exc
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:  # noqa: BLE001 - teardown best-effort
                    pass


# -- Gmail --------------------------------------------------------------------


class GmailClient:
    """Gmail history-resource incremental reader over :mod:`httpx`.

    History entries are polled after the last seen ``historyId`` (cursor shape
    ``{"history_id": int, "account": str}``). Entries are filtered to
    INBOX-history message changes; each referenced message is fetched for
    headers + body. 401/403 → :class:`TransportAuthError`; other
    HTTP/transport errors → :class:`TransportConnectError`.
    """

    def __init__(
        self,
        base_url: str = "https://gmail.googleapis.com",
        timeout: float = 10.0,
        max_items: int = 25,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_items = max_items

    async def run(
        self,
        connector: MailConnector,
        cursor: dict[str, Any],
        *,
        credentials: dict[str, str] | None = None,
    ) -> tuple[list[MailItem], dict[str, Any]]:
        creds = credentials or {}
        token = str(creds.get("access_token") or "")
        account = str(cursor.get("account") or connector.mailbox or "").lower()
        last_history = int(cursor.get("history_id", 0) or 0)
        next_history = last_history
        items: list[MailItem] = []
        params: dict[str, Any] = {
            "historyTypes": "INBOX",
            "maxHistoryEntries": self._max_items * 4,
        }
        if last_history:
            params["startHistoryId"] = last_history + 1

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                history = await client.get(
                    f"{self._base_url}/gmail/v1/users/me/history",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
                if history.status_code in (401, 403):
                    raise TransportAuthError(f"gmail history -> {history.status_code}")
                if history.status_code >= 500:
                    raise TransportConnectError(f"gmail history -> {history.status_code}")
                history.raise_for_status()
                for entry in history.json().get("history", []):
                    next_history = max(next_history, int(entry.get("id") or 0))
                    for change in entry.get("messages", []):
                        message_id = str(change.get("id") or "").strip()
                        if not message_id:
                            continue
                        headers = await self._fetch_message(client, token, message_id)
                        sender = headers.get("From", "")
                        if account and sender and account not in sender.lower():
                            continue  # another account in the shared history feed
                        ts = _parse_rfc2822(headers.pop("Date", None)) or datetime.now(UTC)
                        items.append(
                            MailItem(
                                message_id=f"gmail:{message_id}",
                                account=sender or account,
                                subject=str(headers.get("Subject", "")).strip(),
                                content_bytes=headers.pop("_body", "").encode("utf-8", "replace"),
                                timestamp=ts,
                                headers=headers,
                                source="gmail",
                            )
                        )
                        if len(items) >= self._max_items:
                            break
                    if len(items) >= self._max_items:
                        break
        except (httpx.HTTPError, OSError) as exc:
            raise TransportConnectError(str(exc)) from exc
        # The credential is a separate parameter: the returned cursor is
        # persisted verbatim and must stay secret-free (§4).
        return items, {"history_id": next_history, "account": account}

    async def _fetch_message(
        self, client: httpx.AsyncClient, token: str, message_id: str
    ) -> dict[str, str]:
        resp = await client.get(
            f"{self._base_url}/gmail/v1/users/me/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"format": "full"},
        )
        if resp.status_code in (401, 403):
            raise TransportAuthError(f"gmail message -> {resp.status_code}")
        if resp.status_code >= 500:
            raise TransportConnectError(f"gmail message -> {resp.status_code}")
        resp.raise_for_status()
        body = resp.json()
        headers = {
            str(h.get("name")): str(h.get("value", ""))
            for h in body.get("payload", {}).get("headers", [])
            if h.get("name") in ("From", "To", "Subject", "Date", "Message-ID", "In-Reply-To")
        }
        headers["_body"] = str(body.get("payload", {}).get("bodyData", {}).get("data", ""))
        return headers


# -- test stub ----------------------------------------------------------------


class StubMailTransport:
    """Scripted transport for tests — never opens a socket.

    ``items`` are returned until exhausted; ``raise_exc`` is raised on every
    call — used to exercise the worker's error path. ``next_cursor`` is
    merged into the returned cursor so replay semantics can be asserted.
    """

    def __init__(
        self,
        items: list[MailItem] | None = None,
        *,
        raise_exc: Exception | None = None,
        next_cursor: dict[str, Any] | None = None,
    ) -> None:
        self.items = list(items or [])
        self.raise_exc = raise_exc
        self._next_cursor = next_cursor or {}
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        connector: MailConnector,
        cursor: dict[str, Any],
        *,
        credentials: dict[str, str] | None = None,
    ) -> tuple[list[MailItem], dict[str, Any]]:
        # Record only the credential *keys* — never the values — so a test
        # can assert the worker supplied the secret without the secret
        # itself entering the stub's observable state (§4).
        self.calls.append(
            {
                "connector": connector.connector_id,
                "cursor": dict(cursor),
                "credential_keys": sorted((credentials or {})),
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        delivered = self.items
        self.items = []  # one-shot delivery (cursor already advanced)
        cursor_out: dict[str, Any] = dict(cursor)
        cursor_out.update(self._next_cursor)
        return delivered, cursor_out
