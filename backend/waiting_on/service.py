"""In-memory Waiting-On service (prompt2 §19 — Waiting-On Detection).

This is the Personal Operations Plane slice for tracking *outgoing requests
that received no response*: you asked Sam for the deployment credentials, 3
days pass with no reply, and NerveCenter surfaces "WAITING ON / Sam /
Deployment credentials / 3 days / [Follow up]" (§19) and answers "what am I
waiting for?" (§31).

Design notes
------------
- **NerveCenter never sends or detects messages.** The relay / personal-ops
  layers are the ingestion mechanism: they detect an outgoing request that got
  no response and post a normalized record to this plane via
  ``POST /api/waiting-on/ingest``. That channel-side detection (mailbox /
  message-store / carrier bridges, §46/§47-style) is *out of scope for this
  deliverable*; this service only ever sees normalized waiting-on records and
  tracks them. **No message is ever sent from here** — a follow-up produces a
  rule-based *draft* the human reviews and sends (prompt2 §13: "AI drafts,
  human sends").
- **No persistence yet.** In-memory, like the email / voicemail / contacts
  planes: the API, the Waiting-On screen, and the tests all work without
  ``NERVE_DB_URL``. Public method signatures are designed to survive a later
  swap to a real table.
- **Idempotent ingest.** ``request_id`` is the stable key the relay posts;
  re-posting a known ``request_id`` is a no-op (mirrors email's
  201-new/200-update, no-op-on-repost — §48 replay safety), so a replayed
  event never double-counts a follow-up or stacks a second AttentionItem.
- **One live AttentionItem per OPEN request.** Exactly as the email/messages
  planes keep one live item per source, an OPEN waiting-on request creates (or
  refreshes) a single :class:`~backend.api.models.AttentionItem` on the Now
  screen; resolving the request resolves that item. Scoring reuses the P0
  :class:`~backend.attention.service.AttentionService` scorer, so "why is this
  on my Now screen?" is always answerable (§21/§22).
- **Deterministic elapsed time.** Elapsed days come from an *injectable*
  ``now`` argument that defaults to the wall clock, so tests pin the clock
  and the day count is exact — no wall-clock jitter (same convention as the
  other planes' datetime helpers).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from backend.api.models import (
    AttentionState,
    WaitingOnItem,
    WaitingOnStatus,
)
from backend.attention.service import AttentionService, get_attention_service


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime | str | None) -> datetime | None:
    """Coerce a datetime (or ISO-8601 string, as the relay posts) to UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _days_since(requested_at: datetime, now: datetime) -> int:
    """Whole days elapsed between ``requested_at`` and ``now`` (deterministic).

    ``now`` is injected by the caller (defaults to the wall clock) so the day
    count is exact and testable; a sub-day request is 0, a request from 3 days
    ago is 3 — no fractional drift, no wall-clock jitter.
    """
    requested = _ensure_utc(requested_at) or _now()
    delta = now - requested
    return max(0, delta.days)


def _person_short(name: str) -> str:
    """First token of a display name, for terse "why" factors ("Sam")."""
    return name.strip().split()[0] if name.strip() else name.strip()


class WaitingOnService:
    """In-memory waiting-on tracking, follow-up drafting, and attention
    generation (prompt2 §19/§31)."""

    def __init__(self, attention: AttentionService | None = None) -> None:
        self._attention = attention or get_attention_service()
        self._items: dict[str, WaitingOnItem] = {}

    # -- ingestion -----------------------------------------------------------
    def ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ingest one waiting-on request (as posted by the relay).

        Returns ``{"item": WaitingOnItem, "new_attention": AttentionItem |
        None, "duplicate": bool}``. Re-ingesting an already-seen
        ``request_id`` returns ``duplicate=True`` and changes nothing (§48
        replay safety) — no second AttentionItem, no follow-up bump.
        """
        request_id = str(payload["request_id"]).strip()
        if request_id in self._items:
            item = self._items[request_id]
            attention = self._attention.get(item.attention_id) if item.attention_id else None
            return {"item": item, "new_attention": attention, "duplicate": True}

        person = str(payload["person_display_name"]).strip()
        description = str(payload["description"]).strip()
        requested_at = _ensure_utc(payload.get("requested_at")) or _now()

        item = WaitingOnItem(
            request_id=request_id,
            person_display_name=person,
            person_id=(payload.get("person_id") or None),
            description=description,
            requested_at=requested_at,
            source=payload.get("source", "messages"),
            source_thread_id=(payload.get("source_thread_id") or None),
            status=WaitingOnStatus.OPEN,
            # Escalation policy mirrors the P0 AttentionItem contract (§22/§45):
            # the relay may post ``{"priority": "urgent"}`` to mean "follow up
            # within 24h, or escalate to a VIP if unresolved". We store it as
            # posted and surface it in the API/UI; NerveCenter never sends.
            escalation_policy=(payload.get("escalation_policy") or None),
        )
        self._items[request_id] = item

        # One live AttentionItem while the request is OPEN (mirrors the email /
        # messages planes: create on first sight, never stack duplicates).
        new_attention = self._upsert_attention(item, now=_now())
        return {"item": item, "new_attention": new_attention, "duplicate": False}

    def _upsert_attention(self, item: WaitingOnItem, *, now: datetime) -> Any:
        """Create (or refresh) the single live AttentionItem for an OPEN
        request. Returns the item, or ``None`` if none is live."""
        payload = self._attention_payload(item, now=now)
        existing = self._attention.get(item.attention_id) if item.attention_id else None
        still_live = (
            existing is not None
            and existing.status
            in (
                AttentionState.NOW,
                AttentionState.TODAY,
                AttentionState.SOON,
                AttentionState.WAITING_ON,
            )
        )
        if still_live and item.attention_id is not None:
            refreshed = self._attention.refresh(item.attention_id, payload)
        else:
            created = self._attention.create(payload)
            item.attention_id = created.attention_id
            refreshed = created
        return refreshed

    def _attention_payload(self, item: WaitingOnItem, *, now: datetime) -> dict[str, Any]:
        """The explainable payload for this request's live AttentionItem."""
        days = _days_since(item.requested_at, now)
        factor = f"waiting on {item.person_display_name} for {item.description} ({days} days)"
        # An un-answered request is a real, time-pressing open loop; urgency
        # climbs with how long it has sat unanswered (capped), importance is a
        # steady mid value.
        urgency = min(0.9, 0.5 + 0.1 * days)
        payload: dict[str, Any] = {
            "reason": f"Waiting on {item.person_display_name} — {item.description}",
            "urgency": urgency,
            "importance": 0.7,
            "entities": [item.person_display_name],
            "sources": [item.source.value if hasattr(item.source, "value") else str(item.source)],
            "suggested_action": f"Follow up with {item.person_display_name}",
            "status": AttentionState.NOW.value,
            "why": [factor],
        }
        if days >= 1:
            payload["why"].append(f"unanswered for {days} day{'s' if days != 1 else ''}")
        return payload

    # -- queries -------------------------------------------------------------
    def list_open(self, *, now: datetime | None = None) -> list[WaitingOnItem]:
        """OPEN requests, longest-waiting first.

        Ordered by days elapsed descending, then by ``requested_at`` ascending
        (older first among ties), so the screen leads with what has been
        waiting the longest.
        """
        now = now or _now()
        open_items = [i for i in self._items.values() if i.status is WaitingOnStatus.OPEN]
        return sorted(
            open_items,
            key=lambda i: (-_days_since(i.requested_at, now), i.requested_at),
        )

    def list_all(self, status: WaitingOnStatus | None = None) -> list[WaitingOnItem]:
        """All items, optionally filtered by status.

        ``status=None`` returns everything, ordered by ``requested_at``
        ascending (oldest first) so the audit trail reads chronologically.
        """
        items = [
            i for i in self._items.values()
            if status is None or i.status is status
        ]
        return sorted(items, key=lambda i: i.requested_at)

    def get(self, request_id: str) -> WaitingOnItem | None:
        return self._items.get(request_id)

    def days_elapsed(self, request_id: str, *, now: datetime | None = None) -> int | None:
        """Whole days elapsed for a request (``None`` if unknown)."""
        item = self.get(request_id)
        if item is None:
            return None
        return _days_since(item.requested_at, now or _now())

    # -- resolve / follow-up --------------------------------------------------
    def resolve(self, request_id: str, note: str | None = None) -> WaitingOnItem | None:
        """Mark a request RESOLVED and resolve its live AttentionItem.

        The human acted (the counterpart answered, or the matter closed), so
        the item leaves the Now screen — mirroring email's acknowledge
        (clears unread + resolves the item). Returns ``None`` for an unknown
        ``request_id`` (the router maps that to a 404).
        """
        item = self._items.get(request_id)
        if item is None:
            return None
        item.status = WaitingOnStatus.RESOLVED
        item.resolved_at = _now()
        if note:
            item.note = str(note)
        if item.attention_id:
            self._attention.resolve(item.attention_id)
        return item

    def follow_up(self, request_id: str) -> dict[str, Any] | None:
        """Bump the follow-up count and return a rule-based draft follow-up.

        Deterministic and transparent: this slice is a rule-based drafter
        (§13: "AI drafts, human sends") — it composes the text and returns it
        explicitly labelled draft-only; **nothing is ever sent**. The item
        stays OPEN (following up does not resolve the loop) and re-surfaces
        with a refreshed AttentionItem. Returns ``None`` for an unknown id.
        """
        item = self._items.get(request_id)
        if item is None:
            return None
        item.follow_up_count += 1
        item.last_follow_up_at = _now()
        now = _now()
        draft = self._draft_follow_up(item, now=now)
        # Re-escalate: the request is still OPEN, so refresh its live item so
        # the Now screen reflects the follow-up.
        attention = self._upsert_attention(item, now=now)
        return {
            "request_id": item.request_id,
            "draft": draft,
            "follow_up_count": item.follow_up_count,
            "last_follow_up_at": item.last_follow_up_at,
            "status": item.status.value,
            "draft_only": True,
            "basis": "rule-based follow-up draft (never sent; §13)",
            "new_attention": attention,
        }

    def _draft_follow_up(self, item: WaitingOnItem, *, now: datetime) -> str:
        """Compose the draft follow-up text (rule-based, §13 — never sent).

        The base line matches the §19/§13 example ("Hi Sam — following up on
        deployment credentials. Any update?"); once the request has sat, the
        draft folds in the (deterministic, injectable-``now``) day count so
        the human sees how long it has been waiting.
        """
        first = _person_short(item.person_display_name)
        days = _days_since(item.requested_at, now)
        if days == 0:
            return f"Hi {first} — following up on {item.description}. Any update?"
        if days == 1:
            timing = "1 day, no reply yet"
        else:
            timing = f"{days} days, no reply yet"
        return (
            f"Hi {first} — following up on {item.description} "
            f"({timing}). Any update?"
        )

    # -- seeding / tests ------------------------------------------------------
    def seed_demo(self) -> None:
        """Dev-mode seeder (NERVE_SEED_WAITING_ON=1): a small, realistic set of
        requests so the Waiting-On screen has content before the relay posts
        real traffic.

        Mirrors the prompt2 §19 example (Sam / deployment credentials) plus a
        resolved request, so both card states render in mock/dev mode.
        """
        now = _now()
        self.ingest({
            "request_id": "wo-seed-sam-creds",
            "person_display_name": "Sam",
            "description": "deployment credentials",
            "requested_at": now - timedelta(days=3),
            "source": "messages",
            "source_thread_id": "thread-sam",
            # §19 escalation: an unanswered request this old is urgent —
            # follow up within 24h, or escalate to a VIP if still unresolved.
            "escalation_policy": {"priority": "urgent"},
        })
        self.ingest({
            "request_id": "wo-seed-insurance-claim",
            "person_display_name": "Insurance",
            "description": "claim response",
            "requested_at": now - timedelta(days=1),
            "source": "email",
        })
        # A request that has already been answered (RESOLVED) so the screen
        # shows a closed loop too.
        resolved = self.ingest({
            "request_id": "wo-seed-alice-review",
            "person_display_name": "Alice Nguyen",
            "description": "deck review",
            "requested_at": now - timedelta(days=2),
            "source": "email",
        })
        self.resolve(resolved["item"].request_id, note="Alice reviewed the deck")


# -- process-wide singleton (mirrors the email / attention services) ----------
_service: WaitingOnService | None = None


def get_waiting_on_service() -> WaitingOnService:
    global _service
    if _service is None:
        _service = WaitingOnService()
    return _service


def reset_waiting_on_service() -> None:
    """Test hook — replace the singleton with a fresh, empty service."""
    global _service
    _service = WaitingOnService()
