"""In-memory Attention Engine (prompt2 Phase P0).

This is the P0 slice of the Personal Operations Plane: it holds
:class:`~backend.api.models.AttentionItem` instances in process memory and
ranks them with a **deterministic, explainable** scorer — no opaque ML
(prompt2 §21). Every item must answer "why are you showing me this?" (§22),
which is why the scorer returns both a ``priority`` and a ``why`` list of
human-readable factor contributions.

Design notes
-----------
- **No persistence yet.** P0 deliberately stores items in-process so the API,
  the Now screen, and the tests all work without ``NERVE_DB_URL``. A later
  phase back this store with a real table; the public method signatures are
  designed to survive that swap.
- **Single shared instance.** :data:`get_attention_service` returns a
  process-wide singleton so the router, a dev-mode seeder, and tests observe
  the same queue. It is *not* wired through ``build_services`` (which is
  engine-bound); the router binds it onto ``app.state.attention_service``.
- **Honest degradation.** Unlike the engine-backed services this one is always
  available, so the endpoints never return 503 for "service not configured";
  an empty queue simply returns an empty list.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from backend.api.models import AttentionItem, AttentionState
from backend.attention.policy import (
    AttentionPolicyService,
    EscalationRecord,
    get_attention_policy,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class AttentionService:
    """In-memory, deterministic, explainable Attention Engine (P0)."""

    def __init__(self, policy: AttentionPolicyService | None = None) -> None:
        self._items: dict[str, AttentionItem] = {}
        self._policy = policy or get_attention_policy()

    @property
    def policy(self) -> AttentionPolicyService:
        """The local, user-editable attention policy (AC-R15 §28/§29/§58/§59)."""
        return self._policy

    # -- internals --------------------------------------------------------
    def _sort_key(self, item: AttentionItem) -> tuple[float, datetime, str]:
        # Higher priority first; ties broken by earliest due, then stable id.
        due = item.due or datetime.max.replace(tzinfo=UTC)
        return (-item.priority, due, item.attention_id)

    # -- CRUD -------------------------------------------------------------
    def create(self, payload: dict[str, Any]) -> AttentionItem:
        """Persist a new item, applying the deterministic scorer.

        Callers may pass explicit ``urgency`` / ``importance`` / ``due`` /
        ``why``; anything left out is filled in by :meth:`_score` so the item
        always carries a defensible priority.
        """
        now = _now()
        item = AttentionItem(
            attention_id=f"att-{uuid4().hex[:12]}",
            reason=payload["reason"],
            priority=float(payload.get("priority", 0.0)),
            urgency=float(payload.get("urgency", 0.0)),
            importance=float(payload.get("importance", 0.0)),
            due=_ensure_utc(payload.get("due")),
            entities=list(payload.get("entities", [])),
            sources=list(payload.get("sources", [])),
            suggested_action=payload.get("suggested_action"),
            status=AttentionState(payload.get("status", AttentionState.NOW.value)),
            why=list(payload.get("why", [])),
            created_at=now,
            updated_at=now,
        )
        priority, why = self._score(item)
        item.priority = priority
        # Keep caller-provided reasons first, append the scorer's factors.
        item.why = list(dict.fromkeys(list(item.why) + why))
        self._items[item.attention_id] = item
        self.policy.record_action(
            item.attention_id,
            "create",
            at=now,
            reason=item.reason,
            details={"status": item.status.value},
        )
        return item

    def get(self, attention_id: str) -> AttentionItem | None:
        return self._items.get(attention_id)

    def list(self, status: AttentionState | None = None) -> list[AttentionItem]:
        items = [
            it for it in self._items.values()
            if status is None or it.status == status
        ]
        return sorted(items, key=self._sort_key)

    def now(self) -> list[AttentionItem]:
        """The Now screen's feed: items in the NOW bucket, ranked.

        prompt2 §26 — the home view prioritizes the top few NOW items and
        nothing else. SUPPRESSED and RESOLVED never appear here.
        """
        return self.list(status=AttentionState.NOW)

    def refresh(self, attention_id: str, payload: dict[str, Any]) -> AttentionItem | None:
        """Re-evaluate an existing item in place (re-escalation, prompt2 §76).

        Used by upstream planes (e.g. Messages) so a *source* keeps exactly
        one live attention item: when new activity re-triggers the item, the
        reason/urgency/why are refreshed and it re-enters the NOW bucket
        instead of stacking duplicates. Returns ``None`` if the id is unknown.
        """
        item = self._items.get(attention_id)
        if item is None:
            return None
        if "reason" in payload:
            item.reason = payload["reason"]
        if "urgency" in payload:
            item.urgency = float(payload["urgency"])
        if "importance" in payload:
            item.importance = float(payload["importance"])
        if payload.get("why"):
            item.why = list(dict.fromkeys(item.why + list(payload["why"])))
        if "status" in payload:
            item.status = AttentionState(payload["status"])
        item.updated_at = _now()
        priority, why = self._score(item)
        item.priority = priority
        item.why = list(dict.fromkeys(item.why + why))
        self.policy.record_action(
            attention_id,
            "refresh",
            at=item.updated_at,
            reason=item.reason,
        )
        return item

    def update_status(
        self,
        attention_id: str,
        status: AttentionState,
        action: str | None = None,
        reason: str = "",
    ) -> AttentionItem | None:
        item = self._items.get(attention_id)
        if item is None:
            return None
        item.status = status
        item.updated_at = _now()
        self.policy.record_action(
            attention_id,
            action or f"status:{status.value}",
            at=item.updated_at,
            reason=reason or f"status changed to {status.value}",
        )
        return item

    def resolve(self, attention_id: str) -> AttentionItem | None:
        return self.update_status(
            attention_id,
            AttentionState.RESOLVED,
            action="resolve",
            reason="user resolved the item",
        )

    def suppress(self, attention_id: str) -> AttentionItem | None:
        """Hide an item the user explicitly muted (prompt2 §23 feedback)."""
        return self.update_status(
            attention_id,
            AttentionState.SUPPRESSED,
            action="suppress",
            reason="user explicitly muted the item",
        )

    def snooze(self, attention_id: str, until: datetime | None = None) -> AttentionItem | None:
        """Defer an item (prompt2 §28 escalation / §78 follow-up).

        A snoozed item leaves the NOW bucket (→ TODAY) and records a ``due``
        so it resurfaces when the window passes. No ``until`` → 1 day out.
        """
        item = self._items.get(attention_id)
        if item is None:
            return None
        if until is None:
            until = _now() + timedelta(days=1)
        item.due = _ensure_utc(until)
        item.status = AttentionState.TODAY
        item.updated_at = _now()
        self.policy.record_action(
            attention_id,
            "snooze",
            at=item.updated_at,
            reason="user snoozed the item",
            details={"until": item.due.isoformat() if item.due else None},
        )
        return item

    def acknowledge(self, attention_id: str) -> AttentionItem | None:
        """User handled it → resolve and clear from the live queue (§23)."""
        return self.update_status(
            attention_id,
            AttentionState.RESOLVED,
            action="acknowledge",
            reason="user acknowledged the item (handled)",
        )

    def escalate_deadlines(
        self, now: datetime | str | None = None
    ) -> list[EscalationRecord]:
        """Apply deadline escalation (§28) to the live queue.

        TODAY items whose deadline proximity crosses the configured
        threshold are promoted to NOW in place (score inputs preserved, a
        deterministic reason appended to ``why``); the policy records each
        promotion in the escalation history and action history.
        """
        return self.policy.apply_deadline_escalation(list(self._items.values()), now=now)

    def count(self, status: AttentionState | None = None) -> int:
        return len(self.list(status=status))

    def clear(self) -> None:
        """Test/dev helper — drop all in-memory items."""
        self._items.clear()

    def seed_demo(self) -> list[AttentionItem]:
        """Populate a small, deterministic demo queue (dev mode only).

        Lets the Now screen render real ranked content before email ingestion
        (the rest of P0) exists. Idempotent enough for a dev server: re-seeding
        clears first so you do not stack duplicates on restart.
        """
        self.clear()
        now = _now()
        demo: list[dict[str, Any]] = [
            {
                "reason": "Call the pharmacy before their 5 PM cutoff",
                "urgency": 0.9,
                "importance": 0.7,
                "due": now + timedelta(hours=2),
                "suggested_action": "Call the pharmacy now",
                "entities": ["Pharmacy"],
                "sources": ["calendar"],
            },
            {
                "reason": "Reply to Alice about tomorrow's plan",
                "urgency": 0.6,
                "importance": 0.8,
                "due": now + timedelta(hours=6),
                "suggested_action": "Draft a reply to Alice",
                "entities": ["Alice"],
                "sources": ["email"],
            },
            {
                "reason": "Approve the HelloDJ deployment",
                "urgency": 0.5,
                "importance": 0.9,
                "due": now + timedelta(days=1),
                "suggested_action": "Review and approve the deployment",
                "entities": ["HelloDJ"],
                "sources": ["ci"],
            },
            {
                "reason": "Flight rebooking confirmation pending",
                "urgency": 0.4,
                "importance": 0.5,
                "due": now + timedelta(days=3),
                "suggested_action": "Confirm the rebooking",
                "entities": ["Travel"],
                "sources": ["email"],
            },
        ]
        created = [self.create(d) for d in demo]
        return sorted(created, key=self._sort_key)

    # -- deterministic, explainable scoring (prompt2 §21/§22) --------------
    def _score(self, item: AttentionItem) -> tuple[float, list[str]]:
        """Score an item deterministically and return ``(priority, why)``.

        Factors (subset of §21's list, all explainable):
        - base = 0.5 * importance + 0.5 * urgency (both in [0,1])
        - deadline proximity: an explicit, near due date adds up to +0.25
        - direct request: a suggested_action implies the sender asked for a
          specific act → +0.10
        - repeated/snoozed: an existing due already passed → urgency signal
        Final priority is clamped to [0,1] and rounded to 4 decimals so the
        same input always yields the same output.
        """
        why: list[str] = []
        priority = 0.5 * item.importance + 0.5 * item.urgency
        why.append(
            f"base {(0.5 * item.importance):.2f} from importance {item.importance:.2f} "
            f"+ {(0.5 * item.urgency):.2f} from urgency {item.urgency:.2f}"
        )

        if item.due is not None:
            hours = (item.due - _now()).total_seconds() / 3600.0
            if hours <= 0:
                priority += 0.25
                why.append("deadline already passed — overdue (urgency signal)")
            elif hours <= 12:
                priority += 0.20
                why.append(f"explicit deadline within {hours:.0f}h")
            elif hours <= 48:
                priority += 0.12
                why.append(f"explicit deadline within {hours:.0f}h (within 2 days)")
            else:
                priority += 0.04
                why.append(f"explicit deadline in {hours:.0f}h")

        if item.suggested_action:
            priority += 0.10
            why.append("direct request: a concrete action is expected")

        if item.sources:
            why.append("sourced from: " + ", ".join(item.sources[:4]))

        priority = max(0.0, min(1.0, priority))
        return round(priority, 4), why


# Process-wide singleton (see module docstring).
_service: AttentionService | None = None


def get_attention_service() -> AttentionService:
    global _service
    if _service is None:
        _service = AttentionService()
    return _service


def reset_attention_service() -> None:
    """Test hook — replace the singleton with a fresh, empty service."""
    global _service
    _service = AttentionService()
