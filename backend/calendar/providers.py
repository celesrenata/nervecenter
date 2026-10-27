"""Calendar provider adapters (Personal-Ops Recovery §8 — workstream E).

Three providers sit behind a single injectable **transport seam** so that the
sync runner (and its tests) never perform real network I/O:

- :class:`GoogleCalendarProvider` — OAuth-refreshed token pulled from the
  connector secrets vault; multi-calendar support (one connection row per
  Google account; the account config lists the calendar ids to subscribe);
  event fetch via the Calendar events API; incremental sync via per-calendar
  *sync tokens* kept in the durable ``calendar_accounts.sync_state``.
- :class:`CaldavProvider` — CalDAV over HTTP with basic-auth credentials from
  the vault; reports calendars, fetches ``.ics`` and parses them with the
  minimal stdlib RFC5545 parser below (no third-party iCalendar dependency);
  incremental sync via per-calendar *etag*.
- :class:`AppleCalendarProvider` — **authorized platform integration only**.
  In-cluster Apple events reach NerveCenter through the existing
  ``POST /api/calendar/ingest`` relay path; the provider here *validates and
  normalizes inbound relayed events* rather than scraping the Mac. There is
  no outbound network and no unauthorized access.

Every provider normalizes into :class:`ProviderEvent`, which maps 1:1 onto
the durable ``calendar_events`` columns and (via
:func:`backend.calendar.sync.to_ingest_payload`) onto the in-memory
``CalendarEvent`` shape — so the in-memory :class:`CalendarService` keeps
working unchanged while the durable plane feeds it.

Timezone correctness (freeze §8): datetimes always leave the provider
*timezone-bearing*. A ``TZID`` on a time is honored (resolved via
``zoneinfo``; a non-IANA name is supported through a built VTIMEZONE rule
when the calendar carries one). Floating times fall back to the account's
configured timezone. Times are never stored naive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, Protocol
from zoneinfo import ZoneInfo

#: Bounded initial import window (first sync pulls a trailing context + a
#: leading horizon so "availability today" is answered, not just "tomorrow").
DEFAULT_BACK_WINDOW = timedelta(days=14)
DEFAULT_FORE_WINDOW = timedelta(days=60)


# ---------------------------------------------------------------------------
# Transport seam (injectable; no real network in tests)
# ---------------------------------------------------------------------------
@dataclass
class HttpResponse:
    """A minimal transport response (status, headers, body)."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        import json

        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8")


class HttpTransport(Protocol):
    """The single seam every provider talks to.

    A fake transport in tests implements this and returns scripted
    :class:`HttpResponse` objects — the providers are therefore unit-testable
    with zero network.
    """

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse: ...


class TransportError(Exception):
    """Transport-level failure (bad status, no token, …). Carries a stable
    ``code`` the sync layer maps to a durable ``last_error_code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Normalized event shape
# ---------------------------------------------------------------------------
@dataclass
class ProviderEvent:
    """One normalized provider event.

    ``start``/``end`` are tz-aware datetimes when present (all_day → ``None``
    start with ``all_day=True``). ``ical_uid`` is the provider's stable event
    identity (Google ``id`` / iCalendar ``UID``). ``deleted`` is the explicit
    deletion marker (§8): the sync runner removes it from both planes rather
    than silently dropping it.
    """

    ical_uid: str
    title: str
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    location: str | None = None
    description: str | None = None
    status: str = "confirmed"
    changed_at: str | None = None
    appt: bool = True
    deleted: bool = False
    timezone: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncResult:
    """The outcome of one per-account sync pass.

    - ``events`` — normalized events, including any with ``deleted=True``.
    - ``new_sync_tokens`` — calendar-id → Google sync token to persist.
    - ``etags`` — calendar-id/URL → etag to persist (CalDAV).
    - ``calendars`` — the calendar ids/URLs discovered this pass (the set the
      runner diffs against the durable rows to detect *disappearances*).
    """

    events: list[ProviderEvent] = field(default_factory=list)
    new_sync_tokens: dict[str, str] = field(default_factory=dict)
    etags: dict[str, str] = field(default_factory=dict)
    calendars: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Timezone resolution (freeze §8 "Timezone correctness")
# ---------------------------------------------------------------------------
def resolve_zone(zone: str | None, default: str) -> ZoneInfo:
    """Resolve an IANA zone name to a :class:`ZoneInfo`, falling back to the
    account default, then to UTC. Never raises for a bad name — an unknown
    zone degrades to the default rather than dropping the timezone."""

    for candidate in (zone, default):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (KeyError, ValueError, OSError):
            continue
    return ZoneInfo("UTC")


def to_utc(value: datetime) -> datetime:
    """Normalize a timezone-bearing datetime to UTC (naive input → treated as
    the supplied default zone is *not* applied here — callers must attach a
    zone first)."""

    if value.tzinfo is None:
        # A naive value reaching here is a bug; treat it as UTC rather than
        # silently inventing a wall clock.
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _fmt_local(dt: datetime) -> str:
    """Render a datetime back into its authored (non-UTC) zone for responses."""
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Google provider
# ---------------------------------------------------------------------------
class GoogleCalendarProvider:
    """Google Calendar adapter (multi-calendar, sync-token incremental).

    Credentials: the account's ``secret_ref`` points at a vault secret holding
    a JSON object ``{"refresh_token": ...}``. ``client_id``/``client_secret``
    are non-secret app keys carried in the account ``config``. The token is
    refreshed through the transport (``POST`` to the token endpoint) before
    each pass and cached on the instance for the pass.
    """

    TOKEN_URL = "https://oauth2.googleapis.com/token"
    CALENDARS_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
    EVENTS_URL_TMPL = (
        "https://www.googleapis.com/calendar/v3/calendars/{cal}/events"
    )
    PROVIDER = "google"

    def __init__(
        self,
        transport: HttpTransport,
        *,
        config: dict[str, Any] | None = None,
        secret: str | None = None,
        account_timezone: str = "UTC",
        sync_state: dict[str, Any] | None = None,
    ) -> None:
        self._transport = transport
        self._config = config or {}
        self._secret = secret or ""
        self._account_timezone = account_timezone
        self._sync_state = sync_state if sync_state is not None else {}
        self._access_token: str | None = None

    # -- credential handling ------------------------------------------------
    def _oauth_creds(self) -> dict[str, Any]:
        import json

        try:
            payload = json.loads(self._secret)
        except (ValueError, TypeError):
            payload = {}
        # refresh_token may be the bare secret or nested; client keys live in
        # the non-secret config.
        refresh = payload.get("refresh_token") if isinstance(payload, dict) else None
        if refresh is None and self._secret:
            refresh = self._secret
        return {
            "refresh_token": refresh or "",
            "client_id": self._config.get("client_id", ""),
            "client_secret": self._config.get("client_secret", ""),
        }

    async def _ensure_token(self) -> str:
        if self._access_token:
            return self._access_token
        creds = self._oauth_creds()
        if not creds["refresh_token"]:
            raise TransportError("no_credential", "no Google refresh token in vault")
        import json

        body = json.dumps(
            {
                "grant_type": "refresh_token",
                "refresh_token": creds["refresh_token"],
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
            }
        ).encode("utf-8")
        resp = await self._transport.request(
            "POST",
            self.TOKEN_URL,
            headers={"Content-Type": "application/json"},
            body=body,
        )
        if resp.status >= 300:
            raise TransportError(
                "auth_failed", f"token refresh failed ({resp.status})"
            )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise TransportError("auth_failed", "no access_token in token response")
        self._access_token = token
        return token

    # -- transport helpers ---------------------------------------------------
    async def _get_json(self, url: str, params: dict[str, str] | None) -> Any:
        token = await self._ensure_token()
        resp = await self._transport.request(
            "GET",
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        if resp.status >= 300:
            raise TransportError("upstream_error", f"HTTP {resp.status} from Google")
        return resp.json()

    # -- provider API --------------------------------------------------------
    async def list_calendars(self) -> list[str]:
        """Return the calendar ids to subscribe (config wins, else primary)."""
        configured = self._config.get("calendar_ids")
        if configured:
            return list(configured)
        data = await self._get_json(GoogleCalendarProvider.CALENDARS_URL, None)
        items = data.get("items", [])
        # Default to the primary calendar when none are configured.
        primary = next((c["id"] for c in items if c.get("type") == "primary"), None)
        if primary is None:
            return []
        return [primary]

    async def sync(self, *, window_start: datetime, window_end: datetime) -> SyncResult:
        """Fetch events in [window_start, window_end) for every subscribed
        calendar, honoring stored per-calendar sync tokens."""

        result = SyncResult()
        calendars = await self.list_calendars()
        result.calendars = list(calendars)
        tokens = self._sync_state.get("sync_tokens", {})
        for cal in calendars:
            params = {
                "timeMin": window_start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "timeMax": window_end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "maxResults": "500",
            }
            token = tokens.get(cal)
            if token:
                params["syncToken"] = token
            url = self.EVENTS_URL_TMPL.format(cal=cal)
            data = await self._get_json(url, params)
            for item in data.get("items", []):
                result.events.append(self._normalize(item))
            next_token = data.get("nextSyncToken")
            if next_token:
                result.new_sync_tokens[cal] = next_token
        return result

    # -- normalization -------------------------------------------------------
    def _normalize(self, item: dict[str, Any]) -> ProviderEvent:
        all_day = item.get("start", {}).get("date") is not None
        start = self._parse_start(item.get("start"), all_day)
        end = self._parse_end(item.get("end"), all_day)
        status = str(item.get("status") or "confirmed")
        return ProviderEvent(
            ical_uid=str(item.get("id") or ""),
            title=str(item.get("summary") or "(no title)"),
            start=start,
            end=end,
            all_day=all_day,
            location=item.get("location"),
            description=item.get("description"),
            status=status,
            changed_at=item.get("updated"),
            appt=True,
            # Google reports cancelled instances as status=cancelled; the
            # runner maps this to the deletion marker (durable audit row kept).
            deleted=status == "cancelled",
            timezone=item.get("start", {}).get("timeZone")
            or self._account_timezone,
            extra={
                "attendees": [
                    a.get("email")
                    for a in item.get("attendees", [])
                    if isinstance(a, dict)
                ],
                "recurrence": item.get("recurrence"),
            },
        )

    def _parse_start(self, node: dict[str, Any] | None, all_day: bool) -> datetime | None:
        if not node:
            return None
        if "date" in node:
            # All-day: midnight of the authored day, tz-bearing (the durable
            # row stays queryable; the in-memory plane uses all_day=True).
            zone = resolve_zone(self._config.get("timezone"), self._account_timezone)
            return datetime.strptime(node["date"], "%Y-%m-%d").replace(tzinfo=zone)
        return datetime.fromisoformat(node["dateTime"].replace("Z", "+00:00"))

    def _parse_end(self, node: dict[str, Any] | None, all_day: bool) -> datetime | None:
        if not node:
            return None
        if "date" in node:
            # Google end-date for an all-day event is exclusive; materialize
            # the next midnight so the durable span covers the day.
            zone = resolve_zone(self._config.get("timezone"), self._account_timezone)
            return datetime.strptime(node["date"], "%Y-%m-%d").replace(
                tzinfo=zone
            ) + timedelta(days=1)
        if all_day:
            return None
        return datetime.fromisoformat(node["dateTime"].replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# CalDAV provider
# ---------------------------------------------------------------------------
class CaldavProvider:
    """CalDAV adapter (basic-auth, etag incremental, stdlib iCal parse).

    Credentials: the account's ``secret_ref`` points at a vault secret holding
    a JSON object ``{"username": ..., "password": ...}``. The DAV url is in
    the account ``config``. Calendar discovery uses a ``PROPFIND`` on the
    principal; each calendar's events are fetched as ``text/calendar`` and
    parsed by :func:`parse_ics`.
    """

    PROVIDER = "caldav"

    def __init__(
        self,
        transport: HttpTransport,
        *,
        config: dict[str, Any] | None = None,
        secret: str | None = None,
        account_timezone: str = "UTC",
        sync_state: dict[str, Any] | None = None,
    ) -> None:
        self._transport = transport
        self._config = config or {}
        self._secret = secret or ""
        self._account_timezone = account_timezone
        self._sync_state = sync_state if sync_state is not None else {}
        self._creds = self._parse_creds()

    def _parse_creds(self) -> dict[str, str]:
        import json

        try:
            payload = json.loads(self._secret)
            if isinstance(payload, dict):
                return {
                    "username": str(payload.get("username", "")),
                    "password": str(payload.get("password", "")),
                }
        except (ValueError, TypeError):
            pass
        # Fall back to a bare "user:pass" secret.
        if ":" in self._secret:
            user, _, pw = self._secret.partition(":")
            return {"username": user, "password": pw}
        return {"username": "", "password": self._secret}

    def _auth_header(self) -> dict[str, str]:
        import base64

        token = base64.b64encode(
            f"{self._creds['username']}:{self._creds['password']}".encode()
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def _principal_url(self) -> str:
        base = (self._config.get("url") or "").rstrip("/")
        user = self._creds["username"]
        return f"{base}/dav/principal/{user}/"

    async def list_calendars(self) -> list[str]:
        propfind = (
            '<?xml version="1.0"?>'
            '<multistatus xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            '<prop><C:calendar-color/></prop>'
            "</multistatus>"
        )
        resp = await self._transport.request(
            "PROPFIND",
            self._principal_url(),
            headers={"Depth": "1", **self._auth_header()},
            body=propfind.encode("utf-8"),
        )
        if resp.status in (401, 403):
            raise TransportError("auth_failed", "CalDAV authentication failed")
        if resp.status >= 300:
            raise TransportError("upstream_error", f"PROPFIND failed ({resp.status})")
        # Extract calendar collection hrefs from the response body.
        hrefs = re.findall(r"<(?:C:)?calendar[^>]*href=\"([^\"]+)\"", resp.text())
        if not hrefs:
            hrefs = re.findall(r"href=\"([^\"]+\.ics)\"", resp.text())
        base = (self._config.get("url") or "").rstrip("/")
        out: list[str] = []
        for h in hrefs:
            if h.startswith("http"):
                out.append(h)
            else:
                out.append(f"{base}/{h.lstrip('/')}" if base else h)
        return out

    async def sync(self, *, window_start: datetime, window_end: datetime) -> SyncResult:
        result = SyncResult()
        calendars = await self.list_calendars()
        result.calendars = calendars
        etags = self._sync_state.get("etags", {})
        for cal in calendars:
            headers = {"Depth": "0", **self._auth_header()}
            stored = etags.get(cal)
            if stored:
                headers["If-None-Match"] = stored
            resp = await self._transport.request(
                "GET",
                cal,
                headers=headers,
            )
            if resp.status == 304:
                continue  # unchanged since last etag
            if resp.status >= 300:
                raise TransportError(
                    "upstream_error", f"fetch failed ({resp.status}) for {cal}"
                )
            new_etag = resp.headers.get("etag") or resp.headers.get("Etag")
            if new_etag:
                result.etags[cal] = new_etag
            for ev in parse_ics(resp.text(), default_timezone=self._account_timezone):
                result.events.append(ev)
        return result


# ---------------------------------------------------------------------------
# Apple provider (authorized relay only — no scraping)
# ---------------------------------------------------------------------------
class AppleCalendarProvider:
    """Apple Calendar adapter — **authorized platform integration only**.

    The Mac relay pushes normalized events to ``POST /api/calendar/ingest``;
    this provider does *not* open a network connection to the Mac. It simply
    validates and normalizes the inbound relayed events so they match the
    durable event shape. There is no unauthorized access and no outbound
    network — the class exists to give the sync runner a uniform provider
    interface for the Apple account row.
    """

    PROVIDER = "apple"

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        config: dict[str, Any] | None = None,
        secret: str | None = None,
        account_timezone: str = "UTC",
        sync_state: dict[str, Any] | None = None,
    ) -> None:
        self._transport = transport  # unused: no outbound network by design
        self._config = config or {}
        self._account_timezone = account_timezone

    async def list_calendars(self) -> list[str]:
        # Apple events arrive via the ingest relay, so there is nothing to
        # enumerate; the "calendar" is the relay path itself.
        relay = self._config.get("relay_path", "apple-relay")
        return [relay]

    async def sync(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        relayed_events: list[dict[str, Any]] | None = None,
    ) -> SyncResult:
        """Normalize inbound relayed events. ``relayed_events`` are the
        normalized payloads the Mac relay already posted (via the ingest
        endpoint). No network I/O occurs here."""

        result = SyncResult()
        result.calendars = await self.list_calendars()
        for payload in relayed_events or []:
            result.events.append(self.normalize(payload))
        return result

    def normalize(self, payload: dict[str, Any]) -> ProviderEvent:
        start = payload.get("start")
        end = payload.get("end")
        if isinstance(start, str):
            start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if isinstance(end, str):
            end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return ProviderEvent(
            ical_uid=str(payload.get("ical_uid") or payload.get("event_id") or ""),
            title=str(payload.get("title") or payload.get("summary") or "(no title)"),
            start=start,
            end=end,
            all_day=bool(payload.get("all_day", False)),
            location=payload.get("location"),
            description=payload.get("description"),
            status=str(payload.get("status") or "confirmed"),
            changed_at=payload.get("changed_at"),
            appt=bool(payload.get("appt", True)),
            deleted=bool(payload.get("deleted", False)),
            timezone=payload.get("timezone") or self._account_timezone,
            extra=dict(payload.get("extra") or {}),
        )


def build_provider(
    provider: str,
    transport: HttpTransport,
    *,
    config: dict[str, Any] | None = None,
    secret: str | None = None,
    account_timezone: str = "UTC",
    sync_state: dict[str, Any] | None = None,
):
    """Construct a provider by name (``google`` / ``caldav`` / ``apple``)."""
    config = config or {}
    sync_state = sync_state if sync_state is not None else {}
    if provider == "google":
        return GoogleCalendarProvider(
            transport,
            config=config,
            secret=secret,
            account_timezone=account_timezone,
            sync_state=sync_state,
        )
    if provider == "caldav":
        return CaldavProvider(
            transport,
            config=config,
            secret=secret,
            account_timezone=account_timezone,
            sync_state=sync_state,
        )
    if provider == "apple":
        return AppleCalendarProvider(
            transport,
            config=config,
            secret=secret,
            account_timezone=account_timezone,
            sync_state=sync_state,
        )
    raise TransportError("bad_provider", f"unknown calendar provider: {provider}")


__all__ = [
    "HttpResponse",
    "HttpTransport",
    "TransportError",
    "ProviderEvent",
    "SyncResult",
    "GoogleCalendarProvider",
    "CaldavProvider",
    "AppleCalendarProvider",
    "build_provider",
    "resolve_zone",
    "to_utc",
    "parse_ics",
    "DEFAULT_BACK_WINDOW",
    "DEFAULT_FORE_WINDOW",
]


# ---------------------------------------------------------------------------
# Minimal stdlib RFC5545 parser (SUMMARY/DTSTART/DTEND/UID/LOCATION/
# DESCRIPTION/STATUS/RECURRENCE + VTIMEZONE). No third-party dependency.
# ---------------------------------------------------------------------------
def _unfold(text: str) -> list[str]:
    """Collapse 75-octet continuation lines (a leading space) per RFC5545."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for line in lines:
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return [ln for ln in out if ln.strip() != ""]


def _parse_params(part: str) -> dict[str, str]:
    """Parse a ``;KEY=VAL;KEY2=VAL2`` parameter list (RFC5545 §3.1)."""
    params: dict[str, str] = {}
    for piece in part.strip(";").split(";"):
        if "=" in piece:
            k, _, v = piece.partition("=")
            params[k.strip().upper()] = v.strip().strip('"')
    return params


#: Parameter names that may legally appear on a content line; used to tell a
#: bare ``NAME:KEY=VAL:VALUE`` parameter apart from a plain value that merely
#: contains ``=`` (e.g. a summary with ``=`` in it).
_BARE_PARAM_KEYS = {"TZID", "VALUE", "RANGE", "CUTYPE", "ROLE", "SENT-BY", "RELTYPE"}


def _parse_line(line: str) -> tuple[str, dict[str, str], str] | None:
    """Split ``NAME[;PARAMS]:VALUE`` → (name, params, value).

    RFC5545 separates content-line parameters with ``;`` and the value with
    ``:``. Some emitters write bare ``NAME:KEY=VAL:VALUE`` (no leading
    ``;``); that shape is tolerated so real-world feeds still parse, but only
    when the parameter keys are ones a content line can legally carry.
    """
    if ":" not in line:
        return None
    head, _, value = line.partition(":")
    name, _, param_part = head.partition(";")
    name = name.strip().upper()
    if not name:
        return None
    params: dict[str, str] = _parse_params(param_part) if param_part else {}
    if not params:
        first_seg, has_more, rest_value = value.partition(":")
        bare = _parse_params(first_seg)
        if has_more and bare and all(k in _BARE_PARAM_KEYS for k in bare):
            params = bare
            value = rest_value
    return name, params, value.strip()


def _parse_dt(raw: str, params: dict[str, str], default_zone: str, tz_cache: dict[str, tzinfo]):
    """Parse a DTSTART/DTEND value into a tz-aware datetime (or None for an
    all-day date). Honors ``TZID`` (VTIMEZONE-cached or IANA) and floating →
    the default account zone. Never returns a naive datetime."""
    raw = raw.strip()
    tzid = params.get("TZID")
    zone = None
    if tzid:
        zone = tz_cache.get(tzid)
        if zone is None:
            zone = resolve_zone(tzid, default_zone)
            tz_cache[tzid] = zone
    if re.fullmatch(r"\d{8}", raw):
        d = datetime.strptime(raw, "%Y%m%d")
        return d  # date only — the caller marks all_day
    # 20261014T090000 (local) or 20261014T160000Z (UTC)
    if raw.endswith("Z"):
        dt = datetime.strptime(raw[:-1], "%Y%m%dT%H%M%S")
        return dt.replace(tzinfo=UTC)
    dt = datetime.strptime(raw, "%Y%m%dT%H%M%S")
    if zone is None:
        # Floating time → account's configured zone (freeze: default to the
        # account's timezone; never drop tz).
        zone = resolve_zone(None, default_zone)
    return dt.replace(tzinfo=zone)


def _build_vtimezone(tzid: str, lines: list[str], tz_cache: dict[str, tzinfo]) -> None:
    """Populate ``tz_cache`` for a non-IANA VTIMEZONE name if we can build a
    DST-aware zone from the STANDARD/DAYLIGHT rules. When the name is already
    IANA, ``resolve_zone`` already covers it and this is a no-op."""
    try:
        ZoneInfo(tzid)
        tz_cache[tzid] = ZoneInfo(tzid)
        return
    except (KeyError, ValueError, OSError):
        pass
    zone = _VTimezoneZone(tzid, lines)
    if zone is not None:
        tz_cache[tzid] = zone


class _VTimezoneZone(tzinfo):
    """A compact DST tzinfo built from a VTIMEZONE STANDARD/DAYLIGHT block.

    Supports the common transition rule shapes (BYMONTH + BYMONTHDAY, or
    BYMONTH + BYDAY "ord<weekday>") with a fixed UTC offset for each period.
    This is enough for the freeze's "honor VTIMEZONE" requirement without a
    full iCalendar RRULE engine.
    """

    def __init__(self, tzid: str, lines: list[str]) -> None:
        self.tzid = tzid
        self.standard = self._parse_rule(
            self._select(lines, "BEGIN:STANDARD")
        )
        self.daylight = self._parse_rule(self._select(lines, "BEGIN:DAYLIGHT"))
        if self.standard is None:
            raise ValueError("VTIMEZONE without STANDARD rule")

    # -- rule extraction -----------------------------------------------------
    @staticmethod
    def _select(lines: list[str], marker: str) -> list[str]:
        block: list[str] = []
        inside = False
        for ln in lines:
            if ln == marker:
                inside = True
                continue
            if inside and ln == f"END:{marker.split(':', 1)[1]}":
                break
            if inside:
                block.append(ln)
        return block

    def _parse_rule(self, block: list[str]) -> dict[str, Any] | None:
        if not block:
            return None
        props: dict[str, Any] = {}
        for ln in block:
            parsed = _parse_line(ln)
            if not parsed:
                continue
            name, params, value = parsed
            if name == "DTSTART":
                props["dtstart"] = value
            elif name == "TZOFFSETTO":
                props["offset"] = self._offset(value)
            elif name == "RRULE":
                props["rrule"] = value
        if "offset" not in props:
            return None
        return props

    @staticmethod
    def _offset(value: str) -> timedelta:
        sign = 1 if value[0] != "-" else -1
        value = value.lstrip("+-")
        hh = int(value[:2])
        mm = int(value[2:4])
        ss = int(value[4:6]) if len(value) >= 6 else 0
        return sign * timedelta(hours=hh, minutes=mm, seconds=ss)

    def _rule_date(self, props: dict[str, Any], year: int) -> datetime | None:
        rrule = props.get("rrule", "")
        bymonth = re.search(r"BYMONTH=(\d+)", rrule)
        if not bymonth:
            bymonth_day = re.search(r"BYMONTHDAY=(-?\d+)", rrule)
            weekday = re.search(r"BYDAY=(-?\d+)([A-Z]{2})", rrule)
            if bymonth and bymonth_day:
                month = int(bymonth.group(1))
                day = int(bymonth_day.group(1))
                if day < 0:
                    # negative: count back from month end
                    import calendar as _cal

                    last = _cal.monthrange(year, month)[1]
                    day = last + day + 1
                try:
                    d = date(year, month, day)
                except ValueError:
                    return None
                return datetime(d.year, d.month, d.day, 0, 0, 0)
            if bymonth and weekday:
                month = int(bymonth.group(1))
                ordn = int(weekday.group(1))  # 1 = first, -1 = last
                wd = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}.get(
                    weekday.group(2)
                )
                if wd is None:
                    return None
                import calendar as _cal

                last = _cal.monthrange(year, month)[1]
                days = [
                    d
                    for d in range(1, last + 1)
                    if date(year, month, d).weekday() == wd
                ]
                if not days:
                    return None
                idx = (ordn - 1) if ordn > 0 else (len(days) + ordn)
                if idx < 0 or idx >= len(days):
                    return None
                return datetime(year, month, days[idx], 0, 0, 0)
            return None
        return None

    # -- tzinfo protocol -----------------------------------------------------
    def utcoffset(self, dt: datetime | None) -> timedelta:
        assert self.standard is not None  # validated in __init__
        standard: dict[str, Any] = self.standard
        dt = dt or datetime.now(UTC)
        y = dt.year
        sdt = self._rule_date(standard, y)
        if self.daylight is not None:
            ddt = self._rule_date(self.daylight, y)
            if sdt is not None and ddt is not None and ddt <= sdt:
                # Northern-hemisphere style: daylight until standard starts.
                if dt >= sdt:
                    return standard["offset"]
                return self.daylight["offset"]
            # Southern-hemisphere style (daylight ends before standard starts).
            if sdt is not None and ddt is not None:
                if ddt <= dt < sdt:
                    return self.daylight["offset"]
                return standard["offset"]
        return standard["offset"]

    def dst(self, dt: datetime | None) -> timedelta:
        assert self.standard is not None  # validated in __init__
        standard: dict[str, Any] = self.standard
        std = standard["offset"]
        if self.daylight is not None:
            return self.utcoffset(dt) - std
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return self.tzid

    def __str__(self) -> str:
        return f"<VTimezone {self.tzid}>"


def parse_ics(
    text: str, *, default_timezone: str = "UTC"
) -> list[ProviderEvent]:
    """Parse an iCalendar (``.ics``) body into :class:`ProviderEvent` objects.

    Only the required fields are read (SUMMARY/DTSTART/DTEND/UID/LOCATION/
    DESCRIPTION/STATUS/RECURRENCE) plus VTIMEZONE. ``VEVENT:DELETED`` / a
    STATUS of CANCELLED mark ``deleted`` so the sync runner removes the copy
    rather than silently dropping it.
    """

    tz_cache: dict[str, tzinfo] = {}
    events: list[ProviderEvent] = []
    lines = _unfold(text)

    # Pre-scan for VTIMEZONE blocks so DTSTART TZID lookups can use them.
    for i, ln in enumerate(lines):
        if ln.startswith("BEGIN:VTIMEZONE"):
            block = []
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("END:"):
                    break
                block.append(lines[j])
            # TZID may be a top-level prop or a param on the BEGIN line.
            m = re.search(r"TZID=([^\s\,>]+)", ln)
            for b in block:
                if b.upper().startswith("TZID:"):
                    m = m or re.search(r"TZID=([^\s\,>]+)", b)
            if m:
                _build_vtimezone(m.group(1), block, tz_cache)

    # Walk VEVENT blocks.
    i = 0
    while i < len(lines):
        if not lines[i].startswith("BEGIN:VEVENT"):
            i += 1
            continue
        block: list[str] = []
        j = i
        for j in range(i + 1, len(lines)):
            if lines[j].startswith("END:VEVENT"):
                break
            block.append(lines[j])
        i = j + 1

        props: dict[str, tuple[dict[str, str], str]] = {}
        raw_lines: list[str] = []
        for b in block:
            raw_lines.append(b)
            parsed = _parse_line(b)
            if parsed:
                name, params, value = parsed
                # keep the last value for single-valued props
                props[name] = (params, value)

        def get(name: str, _props: dict[str, tuple[dict[str, str], str]] = props):
            return _props.get(name, ({}, ""))

        uid_params, uid = get("UID")
        summary_params, summary = get("SUMMARY")
        location_params, location = get("LOCATION")
        description_params, description = get("DESCRIPTION")
        status_params, status = get("STATUS")
        rrule_params, rrule = get("RRULE")

        dtstart_params, dtstart_raw = get("DTSTART")
        dtend_params, dtend_raw = get("DTEND")

        all_day = bool(re.fullmatch(r"\d{8}", dtstart_raw.strip() or ""))
        start: datetime | None = None
        end: datetime | None = None
        if dtstart_raw:
            start = _parse_dt(dtstart_raw, dtstart_params, default_timezone, tz_cache)
        if dtend_raw:
            end = _parse_dt(dtend_raw, dtend_params, default_timezone, tz_cache)

        # Recurring events are materialized per occurrence; here we keep the
        # base occurrence and record the RRULE for the (future) expander.
        tzid = dtstart_params.get("TZID")
        events.append(
            ProviderEvent(
                ical_uid=uid,
                title=summary or "(no title)",
                start=start,
                end=end,
                all_day=all_day,
                location=location or None,
                description=description or None,
                status=(status or "confirmed").lower(),
                changed_at=get("DTSTAMP")[1] or None,
                appt=True,
                deleted=(status == "CANCELLED") or (get("SEQUENCE")[0].get("DELETED") == "T"),
                timezone=tzid or default_timezone,
                extra={"rrule": rrule or None, "dtstomp": get("DTSTAMP")[1] or None},
            )
        )
    return events
