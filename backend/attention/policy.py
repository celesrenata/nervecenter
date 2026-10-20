"""Local, user-controlled attention policy (AC-R15 — spec §28/§29/§58/§59).

Adds a policy layer *around* the existing attention service without deleting
or hiding items. All state is in-process, deterministic, explainable, and
user-editable — nothing is inferred remotely (spec §70 local-first):

- **Daily budget (§58).** A per-day cap on individually-notified
  FYI/low-priority items. Low-priority items beyond the cap are *bundled*
  into a single briefing entry (9 low-priority items → 1 bundled entry);
  urgent items (``urgency >= urgent_threshold``) and NOW items bypass
  batching entirely — the budget never suppresses them.
- **Quiet periods (§59).** Local preferences for sleep, meetings, focus,
  driving, and manual DND. A quiet period suppresses *notification flags
  only* — items remain in the NOW feed and keep their status/priority.
- **Deadline escalation (§28).** A TODAY item whose deadline proximity
  crosses the configured threshold is promoted to NOW. The promotion
  carries a deterministic reason (kept in ``why`` and in the escalation
  history) and preserves every score input (urgency/importance/priority).
- **Adaptive escalation (§29).** Per-person/per-channel escalation
  preferences stored locally (e.g. "this person only responds to Watch
  prompts") and applied when deciding the notification mode.
- **Audit.** Every item's action history (create / refresh / snooze /
  resolve / suppress / acknowledge / escalate), its current notification
  state, its escalation reason, and its adaptive decision are exposed
  through :meth:`AttentionPolicyService.audit_response`.

Determinism: identical items + identical clock + identical configuration
always produce identical decisions and reason strings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from backend.api.models import AttentionItem, AttentionState

# -- defaults (all user-editable at runtime) ------------------------------
URGENT_THRESHOLD = 0.7
LOW_PRIORITY_THRESHOLD = 0.4
DEFAULT_DAILY_FYI_CAP = 3
DEFAULT_DEADLINE_PROMOTION_HOURS = 24.0

#: States that never receive a notification and never get bundled.
_INACTIVE = (AttentionState.RESOLVED, AttentionState.SUPPRESSED)


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(dt: datetime | str | None) -> datetime | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _rank(items: list[AttentionItem]) -> list[AttentionItem]:
    """Rank highest-priority first; ties by earliest due, then stable id."""
    fallback = datetime.max.replace(tzinfo=UTC)
    return sorted(items, key=lambda i: (-i.priority, i.due or fallback, i.attention_id))


# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------


class QuietPeriodKind(StrEnum):
    """Local quiet-period sources (§59)."""

    SLEEP = "sleep"
    MEETING = "meeting"
    FOCUS = "focus"
    DRIVING = "driving"
    MANUAL_DND = "manual_dnd"


class QuietPeriod(BaseModel):
    """One local quiet window: notifications suppressed inside it."""

    kind: QuietPeriodKind
    starts_at: datetime
    ends_at: datetime
    reason: str = ""


class AttentionBudget(BaseModel):
    """Per-day budget for FYI/low-priority notification load (§58).

    ``daily_fyi_cap`` is the number of *individual* low-priority
    notification flags allowed per day; the excess is bundled into one
    briefing entry. NOW items and urgent items are never counted against
    or suppressed by the cap.
    """

    daily_fyi_cap: int = Field(default=DEFAULT_DAILY_FYI_CAP, ge=0)
    low_priority_threshold: float = Field(default=LOW_PRIORITY_THRESHOLD, ge=0.0, le=1.0)
    urgent_threshold: float = Field(default=URGENT_THRESHOLD, ge=0.0, le=1.0)


class EscalationMode(StrEnum):
    """How an item reaches the user (adaptive escalation, §29)."""

    NOTIFICATION = "notification"
    WATCH_PROMPT = "watch_prompt"
    ESCALATE = "escalate"
    SILENT = "silent"


class EscalationPreference(BaseModel):
    """A local per-person/per-channel escalation preference (§29).

    ``person`` matches the item's first entity; ``channel`` matches the
    item's first source. Either may be ``None`` (matches anything on that
    dimension). The most specific matching preference wins:
    person+channel > person-only > channel-only.
    """

    person: str | None = None
    channel: str | None = None
    mode: EscalationMode = EscalationMode.NOTIFICATION
    reason: str = ""

    @property
    def preference_id(self) -> str:
        return f"{self.person or '*'}:{self.channel or '*'}"


# ---------------------------------------------------------------------------
# Decision / audit models
# ---------------------------------------------------------------------------


class BudgetEntry(BaseModel):
    """The single bundled briefing entry for over-budget items (§58)."""

    bundle_id: str
    label: str
    count: int
    attention_ids: list[str]


class BudgetDecision(BaseModel):
    """Outcome of applying the daily budget to a set of items."""

    individual: list[str] = Field(default_factory=list)
    bundled: list[str] = Field(default_factory=list)
    urgent: list[str] = Field(default_factory=list)
    bundle: BudgetEntry | None = None
    reasons: list[str] = Field(default_factory=list)


class NotificationState(BaseModel):
    """The notification flag for one item, with deterministic reasons."""

    notify: bool
    reasons: list[str] = Field(default_factory=list)
    quiet_period: QuietPeriod | None = None


class ActionRecord(BaseModel):
    """One entry in an item's local action history."""

    action: str
    at: datetime
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class EscalationRecord(BaseModel):
    """One deadline-escalation event (§28), with its deterministic reason."""

    attention_id: str
    from_status: AttentionState
    to_status: AttentionState
    at: datetime
    reason: str


class AdaptiveDecision(BaseModel):
    """The adaptive-escalation decision applied to one item (§29)."""

    mode: EscalationMode
    preference_id: str | None = None
    person: str | None = None
    channel: str | None = None
    reason: str


class EscalationInfo(BaseModel):
    """The most recent escalation event for an item, plus history depth."""

    reason: str
    promoted_to: AttentionState
    at: datetime
    count: int


class AttentionAudit(BaseModel):
    """The full local audit response for one item (AC-R15 step 5)."""

    attention_id: str
    actions: list[ActionRecord] = Field(default_factory=list)
    notification: NotificationState
    escalation: EscalationInfo | None = None
    adaptive: AdaptiveDecision
    budget: BudgetDecision


# ---------------------------------------------------------------------------
# Policy service
# ---------------------------------------------------------------------------


class AttentionPolicyService:
    """In-memory, deterministic, user-editable attention policy (AC-R15).

    State (all local): the budget, quiet periods, escalation preferences,
    per-item action histories, and escalation histories. The service never
    mutates an item's queue position — except for the explicit
    deadline-promotion path — and never removes items from any feed.
    """

    def __init__(self) -> None:
        self._budget = AttentionBudget()
        self._quiet: list[QuietPeriod] = []
        self._preferences: list[EscalationPreference] = []
        self._promotion_threshold = DEFAULT_DEADLINE_PROMOTION_HOURS
        self._actions: dict[str, list[ActionRecord]] = {}
        self._escalations: dict[str, list[EscalationRecord]] = {}

    # -- budget (§58) -------------------------------------------------------
    def budget(self) -> AttentionBudget:
        return self._budget

    def set_budget(
        self,
        *,
        daily_fyi_cap: int | None = None,
        low_priority_threshold: float | None = None,
        urgent_threshold: float | None = None,
    ) -> AttentionBudget:
        """User-editable budget configuration; ``None`` leaves a value as-is."""
        if daily_fyi_cap is not None:
            self._budget.daily_fyi_cap = int(daily_fyi_cap)
        if low_priority_threshold is not None:
            self._budget.low_priority_threshold = float(low_priority_threshold)
        if urgent_threshold is not None:
            self._budget.urgent_threshold = float(urgent_threshold)
        return self._budget

    def _is_urgent(self, item: AttentionItem) -> bool:
        return item.urgency >= self._budget.urgent_threshold

    def _budget_eligible(self, item: AttentionItem) -> bool:
        """May this item count against (or be suppressed by) the budget?

        NOW items are never suppressed (§26 is the highest-visibility
        bucket) and urgent items bypass batching outright — both are
        therefore ineligible for bundling.
        """
        if item.status == AttentionState.NOW:
            return False
        if self._is_urgent(item):
            return False
        return (
            item.status == AttentionState.FYI
            or item.priority < self._budget.low_priority_threshold
        )

    def budget_decision(
        self, items: list[AttentionItem], now: datetime | str | None = None
    ) -> BudgetDecision:
        """Apply the daily budget to the live set of items.

        The top-ranked low-priority items (up to ``daily_fyi_cap``) keep
        individual notification flags; the remainder is collapsed into a
        single :class:`BudgetEntry`. Urgent and NOW items never appear in
        ``bundled``.
        """
        clock = _now() if now is None else _ensure_utc(now)
        ranked = [i for i in _rank(items) if i.status not in _INACTIVE]
        urgent_ids = [i.attention_id for i in ranked if self._is_urgent(i)]
        eligible = [i for i in ranked if self._budget_eligible(i)]
        cap = self._budget.daily_fyi_cap
        individual = eligible[:cap]
        bundled = eligible[cap:]

        reasons: list[str] = []
        bundle: BudgetEntry | None = None
        if bundled:
            assert clock is not None
            n = len(bundled)
            bundle = BudgetEntry(
                bundle_id=f"fyi-bundle-{clock:%Y-%m-%d}",
                label=(
                    f"{n} low-priority item{'s' if n != 1 else ''} "
                    f"bundled — review in the briefing"
                ),
                count=n,
                attention_ids=[i.attention_id for i in bundled],
            )
            reasons.append(
                f"budget: {n} low-priority item(s) exceed the daily FYI cap ({cap}) "
                f"— bundled into one briefing entry"
            )
        if urgent_ids:
            reasons.append(
                f"budget: {len(urgent_ids)} urgent item(s) bypass batching "
                f"(urgency >= {self._budget.urgent_threshold:g})"
            )
        return BudgetDecision(
            individual=[i.attention_id for i in individual],
            bundled=[i.attention_id for i in bundled],
            urgent=urgent_ids,
            bundle=bundle,
            reasons=reasons,
        )

    # -- quiet periods (§59) ------------------------------------------------
    def quiet_periods(self) -> list[QuietPeriod]:
        return list(self._quiet)

    def add_quiet_period(
        self,
        kind: QuietPeriodKind | str,
        starts_at: datetime | str,
        ends_at: datetime | str,
        reason: str = "",
    ) -> QuietPeriod:
        """Add a local quiet window (sleep / meeting / focus / driving / manual DND)."""
        period = QuietPeriod(
            kind=QuietPeriodKind(kind),
            starts_at=_ensure_utc(starts_at),  # type: ignore[arg-type]
            ends_at=_ensure_utc(ends_at),  # type: ignore[arg-type]
            reason=reason,
        )
        self._quiet.append(period)
        return period

    def clear_quiet_periods(self) -> None:
        self._quiet.clear()

    def active_quiet_period(self, now: datetime | str | None = None) -> QuietPeriod | None:
        """The first quiet window covering ``now`` (insertion order, deterministic)."""
        clock = _now() if now is None else _ensure_utc(now)
        assert clock is not None
        for period in self._quiet:
            if period.starts_at <= clock < period.ends_at:
                return period
        return None

    # -- adaptive escalation (§29) ------------------------------------------
    def escalation_preferences(self) -> list[EscalationPreference]:
        return list(self._preferences)

    def set_escalation_preference(
        self,
        mode: EscalationMode | str,
        person: str | None = None,
        channel: str | None = None,
        reason: str = "",
    ) -> EscalationPreference:
        """Set (or replace) a local per-person/per-channel escalation preference."""
        pref = EscalationPreference(
            person=person,
            channel=channel,
            mode=EscalationMode(mode),
            reason=reason,
        )
        self._preferences = [
            p for p in self._preferences
            if not (p.person == person and p.channel == channel)
        ]
        self._preferences.append(pref)
        return pref

    def remove_escalation_preference(
        self, person: str | None = None, channel: str | None = None
    ) -> bool:
        before = len(self._preferences)
        self._preferences = [
            p for p in self._preferences
            if not (p.person == person and p.channel == channel)
        ]
        return len(self._preferences) != before

    def adaptive_escalation(self, item: AttentionItem) -> AdaptiveDecision:
        """Resolve the escalation mode for one item from local preferences.

        Person comes from the item's first entity, channel from its first
        source. Specificity: person+channel > person-only > channel-only >
        default notification. The reason is a deterministic string.
        """
        person = item.entities[0] if item.entities else None
        channel = item.sources[0] if item.sources else None

        match: EscalationPreference | None = None
        if person is not None and channel is not None:
            match = next(
                (p for p in self._preferences if p.person == person and p.channel == channel),
                None,
            )
        if match is None and person is not None:
            match = next(
                (p for p in self._preferences if p.person == person and p.channel is None),
                None,
            )
        if match is None and channel is not None:
            match = next(
                (p for p in self._preferences if p.channel == channel and p.person is None),
                None,
            )

        if match is not None:
            suffix = f" ({match.reason})" if match.reason else ""
            return AdaptiveDecision(
                mode=match.mode,
                preference_id=match.preference_id,
                person=person,
                channel=channel,
                reason=(
                    f"adaptive escalation: preference '{match.preference_id}' "
                    f"-> {match.mode.value}{suffix}"
                ),
            )
        return AdaptiveDecision(
            mode=EscalationMode.NOTIFICATION,
            preference_id=None,
            person=person,
            channel=channel,
            reason="adaptive escalation: no local preference — default notification mode",
        )

    # -- notification flags (§58/§59) -----------------------------------------
    def notification_flags(
        self, items: list[AttentionItem], now: datetime | str | None = None
    ) -> dict[str, NotificationState]:
        """Per-item notification flag with deterministic reasons.

        A quiet period suppresses every flag (urgent items included) but
        never changes an item's status or its feed membership. The budget
        suppresses flags for over-cap low-priority items (they are
        bundled into the briefing instead). NOW items, urgent items, and
        resolved/suppressed items are handled explicitly above.
        """
        clock = _now() if now is None else _ensure_utc(now)
        decision = self.budget_decision(items, now=clock)
        quiet = self.active_quiet_period(clock)

        flags: dict[str, NotificationState] = {}
        for item in _rank(items):
            reasons: list[str] = []
            if item.status in _INACTIVE:
                notify = False
                reasons.append(f"status {item.status.value}: no notification")
            elif quiet is not None:
                notify = False
                reasons.append(
                    f"quiet period ({quiet.kind.value}): notification suppressed — "
                    f"item remains in its feed"
                )
                if self._is_urgent(item):
                    reasons.append(
                        f"urgent item (urgency {item.urgency:.2f}): flag suppressed by the "
                        f"quiet period, urgent state unchanged"
                    )
            elif item.attention_id in decision.bundled:
                notify = False
                assert decision.bundle is not None
                reasons.append(
                    f"budget: bundled into briefing entry {decision.bundle.bundle_id} "
                    f"(daily FYI cap {self._budget.daily_fyi_cap})"
                )
            elif self._is_urgent(item):
                notify = True
                reasons.append(
                    f"urgent item (urgency {item.urgency:.2f}): bypasses budget batching"
                )
            elif item.status == AttentionState.NOW:
                notify = True
                reasons.append("NOW item: never suppressed by the budget")
            elif item.attention_id in decision.individual:
                notify = True
                reasons.append("budget: individual FYI/low-priority slot within the daily cap")
            else:
                notify = True
                reasons.append("within budget")

            adaptive = self.adaptive_escalation(item)
            if adaptive.mode is EscalationMode.SILENT:
                notify = False
                reasons.append(
                    f"adaptive escalation: silent preference '{adaptive.preference_id}' "
                    f"suppresses the notification"
                )

            flags[item.attention_id] = NotificationState(
                notify=notify,
                reasons=reasons,
                quiet_period=quiet,
            )
        return flags

    # -- deadline escalation (§28) --------------------------------------------
    def deadline_promotion_threshold(self) -> float:
        return self._promotion_threshold

    def set_deadline_promotion_threshold(self, hours: float) -> float:
        """User-editable proximity threshold (hours) for TODAY→NOW promotion."""
        self._promotion_threshold = float(hours)
        return self._promotion_threshold

    def apply_deadline_escalation(
        self,
        items: list[AttentionItem],
        now: datetime | str | None = None,
        mutate: bool = True,
    ) -> list[EscalationRecord]:
        """Promote TODAY items whose deadline proximity crosses the threshold.

        Only items already in the TODAY bucket with a concrete ``due`` are
        considered; the deadline may be near or already overdue. Score
        inputs (urgency / importance / priority) and the existing ``why``
        entries are preserved — the promotion appends exactly one
        deterministic reason to ``why``. Every promotion is recorded in
        the escalation history and the item's action history.
        """
        clock = _now() if now is None else _ensure_utc(now)
        records: list[EscalationRecord] = []
        threshold = self._promotion_threshold
        for item in _rank(items):
            if item.status is not AttentionState.TODAY or item.due is None:
                continue
            hours_until = (item.due - clock).total_seconds() / 3600.0
            if hours_until > threshold:
                continue
            if hours_until <= 0:
                reason = (
                    f"deadline escalation: deadline {abs(hours_until):.1f}h overdue "
                    f"(threshold {threshold:g}h) — promoted TODAY->NOW"
                )
            else:
                reason = (
                    f"deadline escalation: deadline {hours_until:.1f}h away "
                    f"(threshold {threshold:g}h) — promoted TODAY->NOW"
                )
            if mutate:
                item.status = AttentionState.NOW
                item.why = list(dict.fromkeys(list(item.why) + [reason]))
                item.updated_at = clock
            record = EscalationRecord(
                attention_id=item.attention_id,
                from_status=AttentionState.TODAY,
                to_status=AttentionState.NOW,
                at=clock,
                reason=reason,
            )
            self._escalations.setdefault(item.attention_id, []).append(record)
            self.record_action(
                item.attention_id,
                "escalate",
                at=clock,
                reason=reason,
                details={"from": "TODAY", "to": "NOW"},
            )
            records.append(record)
        return records

    # -- action history & audit ----------------------------------------------
    def record_action(
        self,
        attention_id: str,
        action: str,
        at: datetime | str | None = None,
        reason: str = "",
        details: dict[str, Any] | None = None,
    ) -> ActionRecord:
        entry = ActionRecord(
            action=action,
            at=_now() if at is None else _ensure_utc(at),  # type: ignore[arg-type]
            reason=reason,
            details=details or {},
        )
        self._actions.setdefault(attention_id, []).append(entry)
        return entry

    def action_history(self, attention_id: str) -> list[ActionRecord]:
        """The item's local action history (preserved across resolve/snooze)."""
        return list(self._actions.get(attention_id, []))

    def escalation_history(self, attention_id: str) -> list[EscalationRecord]:
        return list(self._escalations.get(attention_id, []))

    def audit_response(
        self, item: AttentionItem, now: datetime | str | None = None
    ) -> AttentionAudit:
        """Full local audit response: notification state, escalation reason,
        adaptive decision, budget outcome, and the complete action history."""
        clock = _now() if now is None else _ensure_utc(now)
        flags = self.notification_flags([item], now=clock)
        records = self._escalations.get(item.attention_id, [])
        escalation: EscalationInfo | None = None
        if records:
            last = records[-1]
            escalation = EscalationInfo(
                reason=last.reason,
                promoted_to=last.to_status,
                at=last.at,
                count=len(records),
            )
        return AttentionAudit(
            attention_id=item.attention_id,
            actions=self.action_history(item.attention_id),
            notification=flags[item.attention_id],
            escalation=escalation,
            adaptive=self.adaptive_escalation(item),
            budget=self.budget_decision([item], now=clock),
        )

    # -- reset -----------------------------------------------------------------
    def reset(self) -> None:
        """Test/dev hook — drop all local policy state (back to defaults)."""
        self._budget = AttentionBudget()
        self._quiet.clear()
        self._preferences.clear()
        self._promotion_threshold = DEFAULT_DEADLINE_PROMOTION_HOURS
        self._actions.clear()
        self._escalations.clear()


# Process-wide singleton (mirrors the attention plane's convention).
_service: AttentionPolicyService | None = None


def get_attention_policy() -> AttentionPolicyService:
    global _service
    if _service is None:
        _service = AttentionPolicyService()
    return _service


def reset_attention_policy() -> None:
    """Test hook — replace the policy singleton with a fresh, empty one."""
    global _service
    _service = AttentionPolicyService()
