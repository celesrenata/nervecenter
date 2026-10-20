"""R110 — deterministic cross-channel communication search (prompt2 §50, AC-R16).

Implements the ``search_communications`` backing service for
``GET /api/communications/search``:

- **Metadata-first matching.** The normalized
  :class:`~backend.communications.models.Communication` record is JSON-safe by
  construction — it carries only a ``content_ref`` pointer (raw content lives
  encrypted outside the knowledge graph, §7). Search therefore runs
  deterministically over the record's metadata: ``channel``, ``direction``,
  ``timestamp``, ``thread_id``, ``person_id``/counterpart, ``classification``
  and ``sensitivity``.
- **Keyword hook for the text.** Where the registry exposes the transient text
  behind a ``content_ref`` (the same house-style content resolver R103
  detection uses), a caller passes a ``text_resolver`` callable and keyword
  matching runs over that text *without* persisting it — the resolver is
  transient by design, and results never embed the body (the keywords that
  matched are echoed back in ``matched_keywords`` instead).
- **§50 example support.** ``"Who asked me for something this week"`` maps to
  a person-scoped request search over the last week (``asked me`` intent +
  ``this week`` horizon → per-person incoming-request groups); ``"Find the
  voicemail about the prescription"`` maps to a channel (``voicemail``) +
  keyword (``prescription``) search.

The function resolves the canonical registry via the
:class:`~backend.communications.registry.CommunicationsService` singleton
(house pattern of the personal planes) so its signature is exactly
``search_communications(query, person_id=None, channel=None, within_week=True)``.

Endpoint follow-up (kept out of this worker's patch so it stays disjoint from
the integrator's work on ``backend/api/app.py``) — the integrator applies::

    from backend.communications import search_communications
    @communications_router.get("/search")
    def search_endpoint(q: str, person_id: str | None = None,
                        channel: CommunicationChannel | None = None,
                        within_week: bool = True):
        return search_communications(
            q, person_id=person_id, channel=channel, within_week=within_week)

"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
)
from backend.communications.registry import get_communications_service

#: A transient text accessor for the body behind a ``content_ref``.
#: ``resolve(content_ref) -> text | None`` (same shape as R103 detection's
#: content resolver — transient, never persisted).
TextResolver = Callable[[str], str | None]

#: Default look-back for ``within_week`` (rolling 7 days).
WEEK = timedelta(days=7)

_WEEK_RE = re.compile(r"\b(this|last)\s+week\b", re.IGNORECASE)
_MONTH_RE = re.compile(r"\b(this|last)\s+month\b", re.IGNORECASE)
_TODAY_RE = re.compile(r"\btoday\b", re.IGNORECASE)
_REQUEST_RE = re.compile(
    r"\b(asked|asks|ask|requested|requests?|needed?|need)\b", re.IGNORECASE
)
_SUBJECT_RE = re.compile(r"\b(who|what|anything|someone|somebody)\b", re.IGNORECASE)
_ASKED_ME_RE = re.compile(r"\basked\s+me\b", re.IGNORECASE)
_CHANNEL_TOKENS: dict[str, CommunicationChannel] = {
    "voicemail": CommunicationChannel.VOICEMAIL,
    "voice": CommunicationChannel.VOICEMAIL,
    "email": CommunicationChannel.EMAIL,
    "mail": CommunicationChannel.EMAIL,
    "imessage": CommunicationChannel.IMESSAGE,
    "message": CommunicationChannel.IMESSAGE,
    "sms": CommunicationChannel.SMS,
    "text": CommunicationChannel.SMS,
    "rcs": CommunicationChannel.RCS,
}


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _window_start(query: str, *, now: datetime, within_week: bool) -> datetime | None:
    """Lower bound of the search horizon parsed from the query text.

    ``this week`` → now − 7 days (rolling); ``last week`` → now − 14 days
    (the 7-day span ending a week ago, approximated); ``today`` → UTC
    midnight; ``this month`` → first of the month; ``last month`` → first of
    the previous month. No explicit horizon and ``within_week`` → now − 7
    days; ``within_week=False`` → unbounded.
    """
    now = _utc(now)
    m = _WEEK_RE.search(query)
    if m:
        return now - (2 * WEEK if m.group(1).lower() == "last" else WEEK)
    m = _MONTH_RE.search(query)
    if m:
        if m.group(1).lower() == "last":
            first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            return first_this - timedelta(days=31)
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if _TODAY_RE.search(query):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if within_week:
        return now - WEEK
    return None


def _window_end(query: str, *, now: datetime) -> datetime | None:
    """Upper (exclusive) bound of the search horizon.

    ``last week`` → the 7-day span *ending* a week ago, i.e. the window is
    ``[now − 14d, now − 7d)``; anything more recent than a week ago is *this*
    week, not last week. No explicit horizon → no upper bound.
    """
    m = _WEEK_RE.search(query)
    if m and m.group(1).lower() == "last":
        return _utc(now) - WEEK
    return None


def _channel_from_query(query: str) -> CommunicationChannel | None:
    """Channel named in the query, if any (deterministic token match)."""
    low = query.lower()
    for token, channel in _CHANNEL_TOKENS.items():
        if re.search(rf"\b{re.escape(token)}\b", low):
            return channel
    return None


def _request_intent(query: str) -> bool:
    """True when the query asks *who asked me for something* (request intent)."""
    if _ASKED_ME_RE.search(query):
        return True
    return _REQUEST_RE.search(query) is not None and _SUBJECT_RE.search(query) is not None


def _keyword_terms(query: str) -> list[str]:
    """Keyword terms: lowercase alphanumeric tokens minus stop-word noise.

    Channel / horizon / request-intent tokens are dropped so they don't
    double-match as keywords.
    """
    stop = {
        "the", "a", "an", "about", "find", "who", "what", "did", "ever",
        "this", "that", "last", "week", "month", "today",
        "voicemail", "voice", "email", "mail", "message", "imessage",
        "sms", "text", "rcs", "asked", "asks", "ask", "request", "requests",
        "requested", "needed", "need", "something", "someone", "somebody",
        "any", "for", "of", "in", "on", "to", "with", "and", "or", "i", "me",
    }
    terms: list[str] = []
    for token in re.findall(r"[a-z0-9][a-z0-9\-]*", query.lower()):
        if token in stop or len(token) < 2:
            continue
        if token not in terms:
            terms.append(token)
    return terms


class PersonRequestGroup(BaseModel):
    """Who asked me: one group per person with their request communications."""

    model_config = ConfigDict(frozen=True)

    person_id: str
    display_name: str | None = None
    communication_ids: list[str] = Field(default_factory=list)
    first_request_at: datetime
    last_request_at: datetime


class SearchFilters(BaseModel):
    """Deterministically parsed query filters (returned for explainability)."""

    model_config = ConfigDict(frozen=True)

    within_week: bool
    window_start: datetime | None = None
    window_end: datetime | None = None
    channel: CommunicationChannel | None = None
    request_intent: bool = False
    keywords: list[str] = Field(default_factory=list)


class CommunicationSearchResult(BaseModel):
    """§50 cross-channel search result — metadata rows only, no plaintext."""

    model_config = ConfigDict(frozen=True)

    query: str
    filters: SearchFilters
    communications: list[Communication] = Field(
        default_factory=list,
        description="Matching normalized records (canonical §7 shape).",
    )
    matched_keywords: list[str] = Field(
        default_factory=list,
        description="Keywords found in the resolved text (echoed, not the body).",
    )
    request_groups: list[PersonRequestGroup] = Field(
        default_factory=list,
        description="Per-person groups for 'who asked me' queries.",
    )
    total: int = Field(description="len(communications)")

    @property
    def people(self) -> list[PersonRequestGroup]:
        """Alias for :attr:`request_groups` (AC-R16: people are returned)."""
        return self.request_groups


def _communication_text(
    communication: Communication, text_resolver: TextResolver | None
) -> str:
    if text_resolver is None:
        return ""
    text = text_resolver(communication.content_ref)
    return str(text or "")


def _matches_communication(
    communication: Communication,
    *,
    person_id: str | None,
    channel: CommunicationChannel | None,
    window_start: datetime | None,
    window_end: datetime | None,
    keywords: list[str],
    text: str,
    person_names: dict[str, str],
) -> tuple[bool, list[str]]:
    """Deterministic metadata (+optional text) match. Returns (matched, hits)."""
    if person_id is not None and communication.person_id != person_id:
        return False, []
    if channel is not None and communication.channel is not channel:
        return False, []
    ts = _utc(communication.timestamp)
    if window_start is not None and ts < window_start:
        return False, []
    if window_end is not None and ts >= window_end:
        return False, []

    if not keywords:
        return True, []

    low = text.lower()
    if low:
        haystack = low
    else:
        # Metadata-only fallback: searchable even without a resolver.
        haystack = " ".join(
            [
                communication.sender or "",
                *communication.recipients,
                communication.thread_id or "",
                str(communication.classification or {}),
                person_names.get(communication.person_id or "", ""),
            ]
        ).lower()
    hits = [term for term in keywords if term in haystack]
    return (bool(hits), hits)


def search_communications(
    query: str,
    person_id: str | None = None,
    channel: CommunicationChannel | None = None,
    within_week: bool = True,
    *,
    now: datetime | None = None,
    text_resolver: TextResolver | None = None,
    request_communication_ids: set[str] | None = None,
) -> CommunicationSearchResult:
    """Deterministic cross-channel search over the normalized communications.

    Parameters
    ----------
    query:
        Free-text query. Channel mentions (``voicemail``/``email``/…),
        horizons (``this week``/``last week``/``today``/``this month``),
        request intent (``who asked me``) and keywords are parsed
        deterministically.
    person_id:
        Optional hard person scope. A person *named* in the query (display
        name) also scopes the search when this argument is ``None``.
    channel:
        Optional hard channel scope (a channel named in the query is used only
        when this argument is ``None``).
    within_week:
        Default rolling 7-day horizon when the query names no horizon.
    now:
        Deterministic clock anchor (UTC).
    text_resolver:
        Optional transient ``content_ref → text`` accessor (keyword hook).
    request_communication_ids:
        Optional set of communication ids that are *genuine requests* (e.g.
        the ids of R103 ``request`` findings). When given, "who asked me"
        groups only those records; when ``None`` every incoming communication
        in scope is treated as a candidate ask.

    Returns
    -------
    :class:`CommunicationSearchResult` — matching records (metadata +
    ``content_ref`` only), the parsed filters, echoed matched keywords, and
    per-person request groups for "who asked me" queries.
    """
    now = _utc(now) if now is not None else datetime.now(UTC)
    service = get_communications_service()
    communications = service.list_communications()
    person_names = {
        p.person_id: (p.display_name or "")
        for p in service.list_people()
        if p.display_name
    }

    named_channel = _channel_from_query(query)
    effective_channel = channel if channel is not None else named_channel
    window_start = _window_start(query, now=now, within_week=within_week)
    window_end = _window_end(query, now=now)
    keywords = _keyword_terms(query)
    request_intent = _request_intent(query)

    # A person named in the query (display name) scopes the search when no
    # explicit person_id was given — conservative: whole-word, first match.
    scoped_person: str | None = person_id
    if scoped_person is None and person_names:
        low = query.lower()
        for pid, name in sorted(person_names.items()):
            if name and re.search(rf"\b{re.escape(name.lower())}\b", low):
                scoped_person = pid
                break

    matched: list[Communication] = []
    matched_keywords: set[str] = set()
    for communication in communications:
        text = _communication_text(communication, text_resolver)
        ok, hits = _matches_communication(
            communication,
            person_id=scoped_person,
            channel=effective_channel,
            window_start=window_start,
            window_end=window_end,
            keywords=keywords,
            text=text,
            person_names=person_names,
        )
        if ok:
            matched.append(communication)
            matched_keywords.update(hits)

    matched = sorted(
        matched, key=lambda c: (_utc(c.timestamp), c.communication_id), reverse=True
    )

    def _request_groups_from(candidates: list[Communication]) -> list[PersonRequestGroup]:
        allowed = (
            set(request_communication_ids) if request_communication_ids is not None else None
        )
        per_person: dict[str, list[Communication]] = {}
        for c in candidates:
            if c.direction is not CommunicationDirection.INCOMING or not c.person_id:
                continue
            if allowed is not None and c.communication_id not in allowed:
                continue
            per_person.setdefault(c.person_id, []).append(c)
        groups: list[PersonRequestGroup] = []
        for pid, comms in sorted(per_person.items()):
            comms = sorted(comms, key=lambda c: _utc(c.timestamp))
            groups.append(
                PersonRequestGroup(
                    person_id=pid,
                    display_name=person_names.get(pid) or None,
                    communication_ids=[c.communication_id for c in comms],
                    first_request_at=_utc(comms[0].timestamp),
                    last_request_at=_utc(comms[-1].timestamp),
                )
            )
        return groups

    if request_intent:
        # §50 "who asked me for something this week": group the incoming
        # communications in scope. With keywords, only keyword-matched records
        # count; without, the horizon (default this week) is the scope.
        request_groups = _request_groups_from(matched)
    else:
        request_groups = []

    return CommunicationSearchResult(
        query=query,
        filters=SearchFilters(
            within_week=within_week,
            window_start=window_start,
            window_end=window_end,
            channel=effective_channel,
            request_intent=request_intent,
            keywords=keywords,
        ),
        communications=matched,
        matched_keywords=sorted(matched_keywords),
        request_groups=request_groups,
        total=len(matched),
    )


__all__ = [
    "CommunicationSearchResult",
    "PersonRequestGroup",
    "SearchFilters",
    "TextResolver",
    "WEEK",
    "search_communications",
]
