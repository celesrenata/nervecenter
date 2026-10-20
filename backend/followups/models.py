"""Follow-up domain models (R104 — AC-R07, spec §57).

A :class:`FollowUp` is a first-class, due-scheduled action item created from an
unanswered outgoing request or an unfulfilled commitment. §57: "You asked Sam
for the document two days ago. No response yet." — NerveCenter surfaces that as
a due follow-up with Follow up / Wait / Forget actions.

State machine
-------------
``OPEN <-> SNOOZED`` (deferral either way) and ``OPEN|SNOOZED -> COMPLETED`` or
``OPEN|SNOOZED -> FORGOTTEN``. ``COMPLETED`` and ``FORGOTTEN`` are terminal:
once reached, a follow-up is immutable (any further transition raises
:class:`FollowUpTransitionError`).

Design notes
------------
- **Refs only.** ``target_kind`` / ``target_id`` point at the originating
  record (a waiting-on request id, a commitment finding id, ...);
  ``person_id`` optionally links the contacts plane's resolved person. No raw
  third-party content is ever stored on a follow-up.
- **``created_from``** is the provenance string of what created the follow-up
  (e.g. ``waiting_on:request-abc123``, ``commitment:fnd-xyz``), mirroring the
  per-source provenance convention (§49).
- **``audit_history``** records every lifecycle step
  (:class:`FollowUpAction`) with its timestamp and a short, secret-free
  detail, so "why is this follow-up where it is?" is always answerable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# -- state machine (AC-R07: OPEN ↔ SNOOZED; OPEN|SNOOZED → terminal) ----------


class FollowUpStatus:
    """Follow-up lifecycle states.

    ``open`` is the default on creation; ``snoozed`` is a deferred open item;
    ``completed`` / ``forgotten`` are terminal and immutable.
    """

    OPEN = "OPEN"
    SNOOZED = "SNOOZED"
    COMPLETED = "COMPLETED"
    FORGOTTEN = "FORGOTTEN"

    ALL = (OPEN, SNOOZED, COMPLETED, FORGOTTEN)
    TERMINAL = (COMPLETED, FORGOTTEN)

    @classmethod
    def parse(cls, value: str) -> str:
        v = str(value or "").strip().upper()
        if v not in cls.ALL:
            raise ValueError(f"unknown follow-up status {value!r}")
        return v


#: Allowed state transitions: source -> set of legal destinations.
#: Anything else (and everything out of a terminal state) is illegal.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    FollowUpStatus.OPEN: frozenset(
        {FollowUpStatus.SNOOZED, FollowUpStatus.COMPLETED, FollowUpStatus.FORGOTTEN}
    ),
    FollowUpStatus.SNOOZED: frozenset(
        {FollowUpStatus.OPEN, FollowUpStatus.COMPLETED, FollowUpStatus.FORGOTTEN}
    ),
    FollowUpStatus.COMPLETED: frozenset(),
    FollowUpStatus.FORGOTTEN: frozenset(),
}


class FollowUpTransitionError(ValueError):
    """Raised when a follow-up is moved to an illegal state.

    Includes any transition from a terminal state (``COMPLETED`` /
    ``FORGOTTEN``), which are immutable.
    """


def can_transition(current: str, target: str) -> bool:
    """True when ``current -> target`` is a legal follow-up transition."""
    try:
        cur = FollowUpStatus.parse(current)
        tgt = FollowUpStatus.parse(target)
    except ValueError:
        return False
    return tgt in _ALLOWED_TRANSITIONS[cur]


def _ensure_utc(value: datetime | str | None) -> datetime | None:
    """Coerce a datetime (or ISO-8601 string) to UTC, house convention."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# -- models -------------------------------------------------------------------


class FollowUpAction(BaseModel):
    """One entry in a follow-up's auditable history.

    ``detail`` is a short, secret-free reason ("snoozed to <iso>",
    "triggered: overdue 1 day", "completed via api", ...). It must never carry
    raw third-party communication content.
    """

    action: str = Field(..., description="created | updated | triggered | completed | forgotten")
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    detail: str | None = None


class FollowUp(BaseModel):
    """One follow-up: a due action item on an unanswered request / unmet
    commitment (R104 — AC-R07, §57).

    ``target_kind`` / ``target_id`` identify what the follow-up is about
    (``"waiting_on"`` + a request id, ``"commitment"`` + a finding id, ...);
    ``person_id`` optionally links the contacts plane. ``due_at`` is when the
    deterministic due-evaluation hook should promote it to an
    :class:`~backend.api.models.AttentionItem`. ``attention_id`` carries the
    one live AttentionItem a triggered (live) follow-up produces on the Now
    screen (§21/§22).
    """

    id: str
    target_kind: str
    target_id: str
    person_id: str | None = None
    due_at: datetime
    reason: str
    created_from: str | None = None
    status: str = FollowUpStatus.OPEN
    attention_id: str | None = Field(
        default=None,
        description=(
            "Live AttentionItem id while this follow-up is triggered; "
            "None if never promoted."
        ),
    )
    audit_history: list[FollowUpAction] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("target_kind")
    @classmethod
    def _nonempty_kind(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("target_kind is required")
        return v

    @field_validator("target_id")
    @classmethod
    def _nonempty_target(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("target_id is required")
        return v

    @field_validator("reason")
    @classmethod
    def _nonempty_reason(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("reason is required")
        return v

    @property
    def is_terminal(self) -> bool:
        return self.status in FollowUpStatus.TERMINAL

    def record(self, action: str, *, at: datetime | None = None,
               detail: str | None = None) -> FollowUpAction:
        """Append one auditable lifecycle step; returns the new entry."""
        entry = FollowUpAction(
            action=action,
            at=_ensure_utc(at) or datetime.now(UTC),
            detail=detail,
        )
        self.audit_history.append(entry)
        return entry


def make_followup_id() -> str:
    """A stable, sortable follow-up id (``fu-<12 hex>``)."""
    from uuid import uuid4

    return f"fu-{uuid4().hex[:12]}"


def followup_payload(fu: FollowUp, **extra: Any) -> dict[str, Any]:
    """A wire-safe dict of one follow-up (events / payloads, no plaintext)."""
    data = fu.model_dump(mode="json")
    data.update(extra)
    return data
