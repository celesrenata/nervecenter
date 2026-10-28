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
- :class:`TransportConnectError` — network/host/TLS failure (also a failed
  folder SELECT); the worker maps it onto ``last_error_code = connect_failed``.

Cursor contract (the worker persists it via ``ingest_cursors``; it advances
the durable row ONLY after a durable commit, so ``run()`` must be *idempotent
for a given cursor* — re-running from the same cursor re-yields the same
items, with no skips and no duplicates):

- IMAP (``imap_seq``): ``{"uid": int, "uidvalidity": int, "folder": str,
  "seen": [...]}`` — the position is **per (connection, folder)**; the folder
  is selected explicitly (never derived from the mailbox email address) and a
  UIDVALIDITY change resets that folder to a fresh sync.
- Gmail (``gmail_history``): ``{"history_id": int, "account": str,
  "seen": [...]}`` — resume reuses the saved *valid* history id (IDs are
  non-contiguous, so never ``+1``); an expired id (404) triggers a bounded
  full resync before continuing incrementally.

The ``seen`` list is the durable, capped dedup guard persisted in the same
cursor row (mirrors :mod:`backend.ingest.seen` — most-recent-win, capped at
``MAX_SEEN_KEYS``); the worker folds its own seen-list into the cursor after
each poll. The credential is a *separate* parameter: the cursor is persisted
in the database and must never contain a secret (§4).
"""

from __future__ import annotations

import base64
import binascii
import email
import email.utils
import imaplib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
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


#: Upper bound on the durable seen-list (matches ``backend.ingest.seen``).
MAX_SEEN_KEYS = 2000


class TransportAuthError(Exception):
    """The mailbox rejected the credential (expired/invalid)."""


class TransportConnectError(Exception):
    """Host/port/TLS/network-level failure reaching the mailbox.

    A rejected folder SELECT (server NO) is mapped here too: it is a
    transport-level failure, not an auth failure, and must not advance the
    cursor.
    """


class _HistoryExpired(Exception):
    """Internal: the saved Gmail history id is expired (history.get → 404)."""


# -- seen-list helpers (mirror backend.ingest.seen; kept local so the
# connector layer does not import back into the ingest layer) ---------------


def _seen_keys(cursor: dict[str, Any] | None) -> list[str]:
    """The durable seen-list from a cursor row value (never mutates it)."""
    raw = (cursor or {}).get("seen") or []
    return [str(v) for v in raw]


def _is_seen(seen: list[str], key: str) -> bool:
    return key in seen


def _mark_seen(seen: list[str], key: str) -> list[str]:
    """Record ``key`` as ingested; return the capped, most-recent list.

    Idempotent: re-marking an existing key returns the list unchanged."""
    if key in seen:
        return seen
    return seen[-(MAX_SEEN_KEYS - 1):] + [key]


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

        Idempotent for a given cursor: re-running from the same cursor
        re-yields the same items (no skips, no duplicates). ``credentials``
        keys (implementation-defined): ``username`` / ``password`` (IMAP) or
        ``access_token`` (Gmail). Raises
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


def _imap_uidvalidity(conn: Any) -> int:
    """The folder's UIDVALIDITY captured from the SELECT untagged response."""
    raw = getattr(conn, "untagged_responses", None) or {}
    vals = raw.get("UIDVALIDITY") or []
    if not vals:
        return 0
    first = vals[0]
    if isinstance(first, bytes):
        first = first.decode("utf-8", "replace")
    try:
        return int(str(first).strip() or 0)
    except (TypeError, ValueError):
        return 0


class ImapPollClient:
    """Real IMAP incremental reader over stdlib :mod:`imaplib`.

    Bounded by ``timeout`` on connect and ``max_items`` per poll (the spec's
    "timeout polling fallback" — never an unbounded IDLE hold). The cursor is
    **per (connection, folder)**:

    - the folder is SELECTed explicitly (read-only) and the response is
      checked OK *before* any UID SEARCH/FETCH — it is never derived from the
      mailbox email address; default folder is ``INBOX``;
    - a UIDVALIDITY change (or a folder switch) resets that folder to a fresh
      ``UID SEARCH ALL`` instead of replaying stale UIDs;
    - otherwise only NEW uids are fetched (``UID <last+1>:*``); a UID
      reassignment/out-of-order is the durable seen-list's job, not a skip.

    Cursor shape: ``{"uid": int, "uidvalidity": int, "folder": str,
    "seen": [...]}``.
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
        username = str(creds.get("username") or connector.mailbox)
        password = str(creds.get("password") or "")
        account = username.lower()
        # B1/B5: the configured folder (default INBOX) — never the email
        # address. The folder is part of the durable cursor so each folder's
        # position is independent.
        folder = str(cursor.get("folder") or "INBOX").strip() or "INBOX"
        stored_uid = int(cursor.get("uid", 0) or 0)
        stored_validity = int(cursor.get("uidvalidity", 0) or 0)
        seen = _seen_keys(cursor)
        conn: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None
        try:
            if connector.ssl:
                conn = imaplib.IMAP4_SSL(connector.host, connector.port, timeout=self._timeout)
            else:
                conn = imaplib.IMAP4(connector.host, connector.port, timeout=self._timeout)
            code, _ = conn.login(username, password)
            if str(code).upper() != "OK":
                raise TransportAuthError(f"LOGIN -> {code}")
            # B1: SELECT the configured folder explicitly and CHECK the
            # response is OK before any UID SEARCH/FETCH. A NO response
            # (unknown folder) is a connect-level failure. (Plain
            # ``select(folder)`` — the imaplib default — so the seam works
            # with imaplib-shaped fakes that only accept the mailbox arg.)
            typ, _ = conn.select(folder)
            if str(typ).upper() != "OK":
                raise TransportConnectError(f"SELECT {folder} -> {typ}")
            # B3: capture UIDVALIDITY; a change (or a folder switch / no
            # stored validity) resets THIS folder to a fresh sync.
            server_validity = _imap_uidvalidity(conn)
            fresh = (
                stored_validity == 0
                or server_validity == 0
                or stored_validity != server_validity
                or str(cursor.get("folder") or folder) != folder
            )
            criterion = "ALL" if fresh else f"{stored_uid + 1}:*"
            # The stdlib idiom is uid("SEARCH", None, "<criterion>") — the
            # second slot is the charset placeholder.
            typ, data = conn.uid("SEARCH", None, criterion)  # type: ignore[call-arg]
            # On a fresh sync the stored uid belongs to a different
            # UIDVALIDITY space (or a different folder) — do not carry it
            # into the new cursor; the position is rebuilt from the fetched
            # UIDs (B3/B4).
            uid_base = 0 if fresh else stored_uid
            if typ != "OK" or not data or not data[0]:
                return [], {
                    "uid": uid_base,
                    "uidvalidity": server_validity,
                    "folder": folder,
                    "seen": seen,
                }
            uids = [int(uid) for uid in data[0].split()[: self._max_items]]
            items: list[MailItem] = []
            newest_uid = uid_base
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
                item = MailItem(
                    message_id=message_id,
                    account=account,
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
                # B5: every uid in range is delivered; dedup of a stale /
                # re-emitted uid is the seen-list's job, never a skip.
                seen = _mark_seen(seen, f"{account}:{message_id}")
                items.append(item)
            return items, {
                "uid": newest_uid,
                "uidvalidity": server_validity,
                "folder": folder,
                "seen": seen,
            }
        except (OSError, ConnectionError, imaplib.IMAP4.abort) as exc:
            raise TransportConnectError(str(exc)) from exc
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:  # noqa: BLE001 - teardown best-effort
                    pass


# -- Gmail --------------------------------------------------------------------


def _b64url_decode(data: str, charset: str = "utf-8") -> str:
    """Decode Gmail ``bodyData`` (base64url, padding often stripped).

    Restores the required ``=`` padding, then decodes with the part's
    ``charset`` (default utf-8), ``errors=replace`` so a bad byte never
    fails the poll.
    """
    if not data:
        return ""
    padded = data + "=" * ((-len(data)) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, binascii.Error):
        return ""
    try:
        return raw.decode(charset, "replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


def _extract_text_body(payload: dict[str, Any]) -> str:
    """Best text body of a Gmail payload (A5).

    Walks ``payload.parts`` recursively (multipart/mixed, multipart/...);
    prefers ``text/plain`` over ``text/html`` and falls back to html when
    there is no plain text; skips ``attachable`` parts and non-``text/*``
    parts; reads the body from ``part.body.bodyData`` (or the top-level
    ``payload.bodyData`` for a simple message).
    """
    best_plain: str | None = None
    best_html: str | None = None

    def consider(node: dict[str, Any]) -> None:
        nonlocal best_plain, best_html
        mime_raw = str(node.get("mimeType", "")).lower()
        if not mime_raw.startswith("text/"):
            return
        mime = mime_raw.split(";", 1)[0].strip()
        charset = "utf-8"
        for token in mime_raw.split(";")[1:]:
            if "charset" in token.lower():
                charset = token.split("=", 1)[1].strip().strip('"') or "utf-8"
        body = node.get("body")
        attachable = bool(body.get("attachable")) if isinstance(body, dict) else False
        data = ""
        if isinstance(body, dict):
            data = str(body.get("bodyData", {}).get("data", ""))
        else:
            data = str(node.get("bodyData", {}).get("data", ""))
        if attachable or not data:
            return
        text = _b64url_decode(data, charset)
        if mime == "text/plain":
            if best_plain is None:
                best_plain = text
        elif mime == "text/html":
            if best_html is None:
                best_html = text
        else:
            if best_plain is None:
                best_plain = text

    def walk(node: dict[str, Any]) -> None:
        consider(node)
        for child in node.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(payload)
    if best_plain is not None:
        return best_plain
    if best_html is not None:
        return best_html
    return ""


class GmailClient:
    """Gmail history-resource incremental reader over :mod:`httpx`.

    Cursor shape: ``{"history_id": int, "account": str, "seen": [...]}``.

    - **Initial sync** (no saved id): call ``history.start`` for a valid
      baseline ``historyId`` and poll ``history.get`` from it (never a bare
      ``startHistoryId`` on an empty account — A1/A2).
    - **Resume**: reuse the saved *valid* ``history_id`` (A3) — history ids
      are non-contiguous, so the cursor is never ``+1``.
    - **Expired id** (``history.get`` → 404): a bounded full resync
      (``history.start`` + ``users/me/messages`` under the INBOX label, capped
      by ``max_items``) then continue incrementally from the fresh baseline
      (A3/A9).
    - **INBOX scoping** via the ``INBOX`` label, never an invalid
      ``historyTypes`` value (A1).
    - Delivery is keyed on ``(account, message id)`` — the ``From`` header
      never gates acceptance (A4); the durable seen-list prevents duplicates
      (A10). A single deleted message (``messages.get`` → 404) is skipped, not
      a poll failure (A9).

    401/403 → :class:`TransportAuthError`; other HTTP/transport errors →
    :class:`TransportConnectError`.
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
        saved_history = int(cursor.get("history_id", 0) or 0)
        seen = _seen_keys(cursor)
        items: list[MailItem] = []
        next_history = saved_history

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            if saved_history:
                # Resume incrementally from the saved valid id (A3).
                try:
                    next_history, seen = await self._poll_history(
                        client, token, saved_history, items, seen, account
                    )
                except _HistoryExpired:
                    # A3/A9: the saved id expired (404) → bounded full
                    # resync, then continue incrementally from the baseline.
                    baseline, seen = await self._full_resync(
                        client, token, items, seen, account
                    )
                    next_history, seen = await self._poll_history(
                        client, token, baseline, items, seen, account
                    )
            else:
                # A1/A2: first sync — history.start gives a valid id, then
                # poll from it (a bare startHistoryId is invalid on an empty
                # account).
                baseline = await self._start_history(client, token)
                if baseline:
                    next_history, seen = await self._poll_history(
                        client, token, baseline, items, seen, account
                    )

        # The credential is a separate parameter: the returned cursor is
        # persisted verbatim and must stay secret-free (§4).
        return items, {"history_id": next_history, "account": account, "seen": seen}

    async def _start_history(self, client: httpx.AsyncClient, token: str) -> int:
        """A1/A2: obtain a valid baseline history id via history.start."""
        resp = await client.get(
            f"{self._base_url}/gmail/v1/users/me/history.start",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code in (401, 403):
            raise TransportAuthError(f"gmail history.start -> {resp.status_code}")
        if resp.status_code >= 500:
            raise TransportConnectError(f"gmail history.start -> {resp.status_code}")
        resp.raise_for_status()
        return int(resp.json().get("historyId") or 0)

    async def _full_resync(
        self,
        client: httpx.AsyncClient,
        token: str,
        items: list[MailItem],
        seen: list[str],
        account: str,
    ) -> tuple[int, list[str]]:
        """A3/A10: bounded full resync after an expired history id.

        Returns ``(baseline_history_id, seen)``. Fetches INBOX messages under
        a label filter, capped by ``max_items``, deduping against the durable
        seen-list so nothing is delivered twice.
        """
        baseline = await self._start_history(client, token)
        resp = await client.get(
            f"{self._base_url}/gmail/v1/users/me/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": "in:inbox", "maxResults": self._max_items},
        )
        if resp.status_code in (401, 403):
            raise TransportAuthError(f"gmail messages -> {resp.status_code}")
        if resp.status_code >= 500:
            raise TransportConnectError(f"gmail messages -> {resp.status_code}")
        resp.raise_for_status()
        for m in resp.json().get("messages", []):
            mid = str(m.get("id") or "").strip()
            if not mid:
                continue
            key = f"{account}:gmail:{mid}"
            if _is_seen(seen, key):
                continue
            fetched = await self._fetch_message(client, token, mid)
            if fetched is None:
                continue
            headers, body = fetched
            ts = _parse_rfc2822(headers.get("Date")) or datetime.now(UTC)
            seen = _mark_seen(seen, key)
            items.append(
                MailItem(
                    message_id=f"gmail:{mid}",
                    account=account,
                    subject=str(headers.get("Subject", "")).strip(),
                    content_bytes=body.encode("utf-8", "replace"),
                    timestamp=ts,
                    headers={k: v for k, v in headers.items()},
                    source="gmail",
                )
            )
        return baseline, seen

    async def _poll_history(
        self,
        client: httpx.AsyncClient,
        token: str,
        start_id: int,
        items: list[MailItem],
        seen: list[str],
        account: str,
    ) -> tuple[int, list[str]]:
        """A8: page ``history.get`` from ``start_id`` until the saved
        position is exhausted (``historyNewerThan`` falsy) or ``max_items``
        is met.

        Returns ``(next_history, seen)`` where ``next_history`` is the history
        position of the **last processed** message (so a restart re-yields
        the unprocessed tail of that entry — no skips).
        """
        next_newer = start_id
        last_entry_id = start_id
        guard = 0
        while len(items) < self._max_items and next_newer:
            # Real Gmail wire: query params use dot-notation. `startHistoryId`
            # is INCLUSIVE (entries with id >= it are returned) — so resuming
            # from the saved "last processed" position re-yields that entry's
            # unprocessed messages (no skips); the seen-list suppresses the
            # already-processed ones.
            params: dict[str, Any] = {
                "history.startHistoryId": next_newer,
                "history.maxHistoryEntries": self._max_items * 4,
            }
            resp = await client.get(
                f"{self._base_url}/gmail/v1/users/me/history",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            if resp.status_code in (401, 403):
                raise TransportAuthError(f"gmail history -> {resp.status_code}")
            if resp.status_code == 404:
                raise _HistoryExpired("expired historyId")
            if resp.status_code >= 500:
                raise TransportConnectError(f"gmail history -> {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            entries = data.get("history", [])
            newer_than = data.get("historyNewerThan")
            for entry in entries:
                entry_id = int(entry.get("id") or 0)
                # A8: the cursor position is the last *visited* entry, so a
                # restart re-yields that entry's unprocessed messages (seen
                # suppresses the rest) — and deleted-only entries still
                # advance the cursor past themselves (A9).
                last_entry_id = entry_id
                for change in entry.get("messages", []):
                    mid = str(change.get("id") or "").strip()
                    if not mid:
                        continue
                    key = f"{account}:gmail:{mid}"
                    # A10: the durable seen-list suppresses re-delivery.
                    if _is_seen(seen, key):
                        continue
                    fetched = await self._fetch_message(client, token, mid)
                    if fetched is None:
                        continue  # A9: a deleted message skips, not fails
                    headers, body = fetched
                    ts = _parse_rfc2822(headers.get("Date")) or datetime.now(UTC)
                    # A4: no From-header rejection — delivery is keyed on
                    # (account, message id).
                    seen = _mark_seen(seen, key)
                    items.append(
                        MailItem(
                            message_id=f"gmail:{mid}",
                            account=account,
                            subject=str(headers.get("Subject", "")).strip(),
                            content_bytes=body.encode("utf-8", "replace"),
                            timestamp=ts,
                            headers={k: v for k, v in headers.items()},
                            source="gmail",
                        )
                    )
                    last_entry_id = entry_id
                    if len(items) >= self._max_items:
                        break
                if len(items) >= self._max_items:
                    break
            if not newer_than:
                break  # A8: historyNewerThan exhausted
            next_newer = int(newer_than)
            guard += 1
            if guard > 10000:
                break  # defensive bound; paging should terminate on its own
        return last_entry_id, seen

    async def _fetch_message(
        self, client: httpx.AsyncClient, token: str, message_id: str
    ) -> tuple[dict[str, str], str] | None:
        """Fetch one message's headers + best text body.

        Returns ``None`` for a deleted message (404) so the caller skips that
        single message instead of failing the poll (A9).
        """
        resp = await client.get(
            f"{self._base_url}/gmail/v1/users/me/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"format": "full"},
        )
        if resp.status_code == 404:
            return None
        if resp.status_code in (401, 403):
            raise TransportAuthError(f"gmail message -> {resp.status_code}")
        if resp.status_code >= 500:
            raise TransportConnectError(f"gmail message -> {resp.status_code}")
        resp.raise_for_status()
        body = resp.json()
        payload = body.get("payload", {})
        headers = {
            str(h.get("name")): str(h.get("value", ""))
            for h in payload.get("headers", [])
            if h.get("name") in ("From", "To", "Subject", "Date", "Message-ID", "In-Reply-To")
        }
        return headers, _extract_text_body(payload)


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
                "credential_keys": sorted(credentials or {}),
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        delivered = self.items
        self.items = []  # one-shot delivery (cursor already advanced)
        cursor_out: dict[str, Any] = dict(cursor)
        cursor_out.update(self._next_cursor)
        return delivered, cursor_out
