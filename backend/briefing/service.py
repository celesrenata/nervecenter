"""In-memory Briefing plane (prompt2 §30 — Briefings).

This is the read-only, deterministic *aggregation* plane: once a day (or on
demand) it composes the §30 brief — **Actions**, **Waiting On**, **Today**,
**Overnight** — from state that is *already committed* on the other planes
(P0 attention, §19 waiting-on, P2 calendar, P0 email, P1 messages, P2
voicemail) and returns a presentable brief. The §30 "Morning Brief":

    Sunday
    3 things need action today.
      1. Call pharmacy before 5.
      2. Reply to Alice.
      ...
    You're waiting on:
      • Sam — deployment credentials
    Today:
      • Dinner 6:30 PM
    Overnight:
      • 41 emails
      • 12 messages
      • 1 voicemail
    Nothing else requires attention.

Design notes
------------
- **Read-only, no state of its own.** Unlike the other planes this service
  never ingests, mutates, or persists. It *projects*: each section is a flat
  view over the public read methods of the planes it aggregates. There is
  nothing of its own to reset — :func:`reset_briefing_service` only re-binds
  the process singleton to a fresh ``BriefingService`` (whose dependencies
  are re-resolved lazily at compose time anyway). No LLM, no model, no
  generation step (prompt2 §30 is a projection of existing state, not a
  free-text generation task).
- **Deterministic and clock-injected.** ``compose(now=...)`` takes an
  injectable ``now`` that defaults to the wall clock. Every time-relative
  value — "days elapsed" (waiting-on), "today" (calendar), the 24-hour
  overnight window — is computed from *that* clock, so a test that pins
  ``now`` gets exact, reproducible section contents. Same convention as the
  other planes' datetime helpers.
- **Single shared instance.** :data:`get_briefing_service` returns a
  process-wide singleton so the router and tests observe the same aggregator.
  Like the other plane singletons it is *not* engine-bound; the router binds
  it onto ``app.state.briefing_service``.
- **Honest emptiness.** ``kind`` is echoed back as metadata (the four §30
  sections are identical for morning/midday/evening — the kind is which
  slot, not a filter). ``empty`` is True when every section has no content,
  so the screen can render the honest §30 closing line
  ("Nothing else requires attention.") unambiguously.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from backend.api.models import (
    Briefing,
    BriefingAction,
    BriefingEvent,
    BriefingKind,
    BriefingOvernight,
    BriefingWaitingOn,
)
from backend.attention import get_attention_service
from backend.calendar import get_calendar_service
from backend.email import get_email_service
from backend.messages import get_messages_service
from backend.voicemail import get_voicemail_service
from backend.waiting_on import get_waiting_on_service


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime | str | None) -> datetime:
    """Coerce a datetime (or ISO-8601 string) to UTC.

    Mirrors the other planes' ``_ensure_utc``; here it never returns ``None``
    because the compose clock must always be a concrete UTC instant.
    """
    if value is None:
        return _now()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _days_since(requested_at: datetime, now: datetime) -> int:
    """Whole days elapsed between ``requested_at`` and ``now`` (deterministic).

    ``now`` and ``requested_at`` are both resolved to UTC, so the count is
    exact and testable (a request from 3 days ago is 3; sub-day is 0) — the
    same convention the waiting-on plane uses to render "(n) days" (§19).
    """
    if requested_at is None:
        return 0
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=UTC)
    delta = now - requested_at
    return max(0, delta.days)


#: How many of the top-ranked attention items the brief shows (prompt2 §30
#: "3 things need action today.") Kept conservative so the brief stays a
#: *brief* — the Now screen holds the full ranked queue.
MAX_ACTIONS = 5

#: The lookback window for "overnight" activity (§31 "what happened while I
#: slept?"): new activity on each plane since ``now - 24h``.
OVERNIGHT_WINDOW = timedelta(hours=24)


class BriefingService:
    """Read-only, deterministic composer over the personal-operations planes.

    The six planes are injected as *getter* callables (defaulting to each
    plane's process singleton) so ``compose`` always reads the *live* state —
    including after a test's ``reset_*_service`` hook has swapped the
    singleton — rather than a snapshot captured at construction time.
    """

    def __init__(
        self,
        attention: Callable[[], Any] | None = None,
        email: Callable[[], Any] | None = None,
        messages: Callable[[], Any] | None = None,
        voicemail: Callable[[], Any] | None = None,
        waiting_on: Callable[[], Any] | None = None,
        calendar: Callable[[], Any] | None = None,
    ) -> None:
        self._attention = attention or get_attention_service
        self._email = email or get_email_service
        self._messages = messages or get_messages_service
        self._voicemail = voicemail or get_voicemail_service
        self._waiting_on = waiting_on or get_waiting_on_service
        self._calendar = calendar or get_calendar_service

    # -- the composition -----------------------------------------------------
    def compose(self, kind: BriefingKind | str = BriefingKind.MORNING,
                now: datetime | None = None) -> Briefing:
        """Compose one briefing from the live plane state.

        ``kind`` selects the slot (morning/midday/evening) and is echoed as
        metadata; ``now`` pins the clock for every time-relative value. The
        result is a fully-materialized :class:`~backend.api.models.Briefing`
        ready to serialize — no further I/O, no further state.
        """
        if isinstance(kind, str):
            kind = BriefingKind(kind)
        clock = _ensure_utc(now)

        actions = self._compose_actions(clock)
        waiting_on = self._compose_waiting_on(clock)
        today = self._compose_today(clock)
        overnight = self._compose_overnight(clock)

        empty = not (
            actions or waiting_on or today
            or overnight.emails or overnight.messages or overnight.voicemails
        )
        return Briefing(
            kind=kind,
            generated_at=clock,
            actions=actions,
            waiting_on=waiting_on,
            today=today,
            overnight=overnight,
            empty=empty,
        )

    # -- sections ------------------------------------------------------------
    def _compose_actions(self, now: datetime) -> list[BriefingAction]:
        """Top of the Now queue (prompt2 §26), capped, highest-priority first.

        ``label`` is the action to take (the item's ``suggested_action`` when
        the plane proposed one, else its ``reason``); ``due`` carries the item
        due time for "before 5 PM" framing. The Now screen's ordering (ranked
        by the P0 scorer) is preserved.

        AC-R15 (§58 bundling hook): when the attention plane's daily FYI
        budget bundles low-priority items into one entry, that single
        bundled entry is appended after the Now actions — nine
        low-priority items surface as exactly one briefing entry, while
        urgent items (which bypass batching) keep their own rows. The
        bundled items remain in the attention queue; nothing is deleted.
        """
        now_items = self._attention().now()[:MAX_ACTIONS]
        actions = [
            BriefingAction(
                attention_id=item.attention_id,
                label=item.suggested_action or item.reason,
                due=item.due,
            )
            for item in now_items
        ]

        attention_service = self._attention()
        decision = attention_service.policy.budget_decision(
            attention_service.list(), now=now
        )
        bundle = decision.bundle
        if bundle is not None:
            actions.append(
                BriefingAction(
                    attention_id=bundle.bundle_id,
                    label=bundle.label,
                    due=None,
                )
            )
        return actions

    def _compose_waiting_on(self, now: datetime) -> list[BriefingWaitingOn]:
        """Open outstanding requests, longest-waiting first (§19/§31)."""
        items = self._waiting_on().list_open(now=now)
        return [
            BriefingWaitingOn(
                request_id=item.request_id,
                person=item.person_display_name,
                description=item.description,
                days_elapsed=_days_since(item.requested_at, now),
            )
            for item in items
        ]

    def _compose_today(self, now: datetime) -> list[BriefingEvent]:
        """Calendar events overlapping today (§42), earliest first."""
        day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        events = self._calendar().list_events(start=day_start, end=day_end)
        return [
            BriefingEvent(event_id=ev.event_id, title=ev.title, start=ev.start)
            for ev in events
        ]

    def _compose_overnight(self, now: datetime) -> BriefingOvernight:
        """New activity since ``now - 24h`` per plane (§31).

        Counts *threads/conversations/threads* whose last activity falls in
        the window — email by ``last_timestamp``, messages by
        ``last_timestamp``, voicemail by ``last_received_at``. These are the
        "41 emails / 12 messages / 1 voicemail" lines of the §30 brief.
        """
        window_start = now - OVERNIGHT_WINDOW

        def _count(ts: datetime | None) -> bool:
            return ts is not None and ts >= window_start

        email_n = sum(1 for t in self._email().list_threads() if _count(t.last_timestamp))
        message_n = sum(
            1 for c in self._messages().list_conversations() if _count(c.last_timestamp)
        )
        voicemail_n = sum(
            1 for t in self._voicemail().list_threads() if _count(t.last_received_at)
        )
        return BriefingOvernight(
            emails=email_n, messages=message_n, voicemails=voicemail_n
        )


# -- process singleton ----------------------------------------------------
_briefing_service: BriefingService | None = None


def get_briefing_service() -> BriefingService:
    """The process-wide briefing aggregator (bound onto ``app.state``)."""
    global _briefing_service
    if _briefing_service is None:
        _briefing_service = BriefingService()
    return _briefing_service


def reset_briefing_service() -> None:
    """Test hook — replace the singleton with a fresh aggregator.

    Re-resolves the plane getters lazily at compose time, so this only needs
    a fresh ``BriefingService``; the planes' own state is untouched (there is
    none to reset here).
    """
    global _briefing_service
    _briefing_service = BriefingService()
