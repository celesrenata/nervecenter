"""Data models for the NerveCenter API."""
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Status(StrEnum):
    """Service status enumeration."""
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class ServiceHealth(BaseModel):
    """Health check result for a service."""
    name: str
    status: Status
    version: str | None = None
    message: str | None = None
    details: dict[str, Any] | None = None
    checked_at: datetime = Field(default_factory=datetime.utcnow)


class HealthResponse(BaseModel):
    """Health check response."""
    status: Status
    services: list[ServiceHealth]
    total_services: int
    healthy_services: int


class MetricsPoint(BaseModel):
    """Single metrics data point."""
    timestamp: datetime
    name: str
    value: float
    labels: dict[str, str] | None = None


class MetricsResponse(BaseModel):
    """Metrics response."""
    metrics: list[MetricsPoint]
    summary: dict[str, Any] | None = None


class TraceSpan(BaseModel):
    """Trace span representation."""
    span_id: str
    trace_id: str
    operation: str
    start_time: datetime
    end_time: datetime
    duration_ms: float
    status: str
    attributes: dict[str, Any] | None = None
    parent_span_id: str | None = None


class TraceResponse(BaseModel):
    """Trace response."""
    trace_id: str
    spans: list[TraceSpan]
    root_span: TraceSpan | None = None


class AuthRequest(BaseModel):
    """Authentication request."""
    username: str
    password: str


class AuthResponse(BaseModel):
    """Authentication response."""
    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_in: int
    user: dict[str, Any]


class TokenRefreshRequest(BaseModel):
    """Token refresh request."""
    refresh_token: str


class TokenRefreshResponse(BaseModel):
    """Token refresh response."""
    access_token: str
    token_type: str = "Bearer"
    expires_in: int


class ErrorDetail(BaseModel):
    """Error detail."""
    code: str
    message: str
    field: str | None = None
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    """Error response."""
    error: ErrorDetail
    request_id: str


class VersionInfo(BaseModel):
    """API version information."""
    version: str
    release_date: str | None = None
    compatible_versions: list[str] = []


class ApiResponseMeta(BaseModel):
    """API response metadata."""
    version: str
    request_id: str
    timestamp: datetime
    pagination: dict[str, int] | None = None


class ListResponse(BaseModel):
    """Generic list response."""
    data: list[Any]
    meta: ApiResponseMeta


class EntityCreateRequest(BaseModel):
    """Request body for creating a new Entity."""
    entity_type: str
    name: str
    description: str | None = None
    attributes: dict[str, Any] = {}
    aliases: list[str] = []
    confidence: float = 1.0
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_evidence: list[str] = []


class EntityUpdateRequest(BaseModel):
    """Request body for updating an Entity."""
    name: str | None = None
    description: str | None = None
    entity_type: str | None = None
    attributes: dict[str, Any] | None = None
    aliases: list[str] | None = None
    confidence: float | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_evidence: list[str] | None = None


class GoalCreateRequest(BaseModel):
    """Request body for creating a new Goal."""
    title: str
    description: str | None = None
    status: str = "active"
    progress: float = 0.0
    criteria: list[str] = []
    subgoal_ids: list[str] = []
    priority: int = 0
    deadline: datetime | None = None
    related_entity_ids: list[str] = []


class GoalUpdateRequest(BaseModel):
    """Request body for updating a Goal."""
    title: str | None = None
    description: str | None = None
    status: str | None = None
    progress: float | None = None
    criteria: list[str] | None = None
    subgoal_ids: list[str] | None = None
    priority: int | None = None
    deadline: datetime | None = None
    related_entity_ids: list[str] | None = None


class TaskCreateRequest(BaseModel):
    """Request body for creating a new Task."""
    title: str
    description: str | None = None
    status: str = "pending"
    priority: int = 0
    goal_id: str | None = None
    decision_id: str | None = None
    assignee_agent_id: str | None = None
    due_at: datetime | None = None
    scheduled_at: datetime | None = None
    inputs: dict[str, Any] = {}
    dependencies: list[str] = []


class TaskUpdateRequest(BaseModel):
    """Request body for updating a Task."""
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: int | None = None
    goal_id: str | None = None
    decision_id: str | None = None
    assignee_agent_id: str | None = None
    due_at: datetime | None = None
    scheduled_at: datetime | None = None
    inputs: dict[str, Any] | None = None
    dependencies: list[str] | None = None


class RelationshipCreateRequest(BaseModel):
    """Request body for creating a new Relationship."""
    source_entity_id: str
    target_entity_id: str
    relation_type: str
    properties: dict[str, Any] = {}
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    confidence: float = 1.0
    evidence: list[str] = []


class ClaimsCreateRequest(BaseModel):
    """Request body for creating a Claim (501 for now)."""
    statement: str
    confidence: float = 1.0
    entity_id: str | None = None


class AttentionState(StrEnum):
    """Attention lifecycle states (prompt2 section 25).

    ``NOW`` is the highest-visibility bucket (the Now screen); ``RESOLVED``
    items are archived out of the live queue; ``SUPPRESSED`` hides an item the
    user explicitly muted.
    """

    NOW = "NOW"
    TODAY = "TODAY"
    SOON = "SOON"
    WAITING_ON = "WAITING_ON"
    FYI = "FYI"
    SUPPRESSED = "SUPPRESSED"
    RESOLVED = "RESOLVED"


class AttentionItem(BaseModel):
    """An AttentionItem (prompt2 section 20).

    The principal output of the Personal Operations Plane: one ranked,
    explainable thing that needs the user's attention right now. Scoring is
    deterministic and explainable (prompt2 section 21) -- no opaque ML -- so
    ``reason`` and ``why`` answer "why are you showing me this?" (section 22).
    """

    attention_id: str
    reason: str
    priority: float = Field(default=0.0, ge=0.0, le=1.0)
    urgency: float = Field(default=0.0, ge=0.0, le=1.0)
    importance: float = Field(default=0.0, ge=0.0, le=1.0)
    due: datetime | None = None
    entities: list[str] = []
    sources: list[str] = []
    suggested_action: str | None = None
    status: AttentionState = AttentionState.NOW
    # Explainability (section 22): the human-readable reasons behind ranking.
    why: list[str] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Messages (prompt2 Phase P1)
# ---------------------------------------------------------------------------


class MessageDirection(StrEnum):
    """Which side of the conversation the message came from (prompt2 §76)."""

    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class Message(BaseModel):
    """One ingested message (prompt2 §76 — message ingestion).

    The macOS Personal Relay (``nerve-personal-relay``) posts normalized
    messages here; NerveCenter never touches source message storage directly
    (prompt2 §47). ``peer_identifier`` is the raw contact id (phone number,
    iMessage handle, ...); ``peer_name`` is the *resolved* display name after
    cross-channel person resolution, which may be ``None`` when no person has
    been matched yet.
    """

    message_id: str
    conversation_id: str
    direction: MessageDirection
    peer_identifier: str
    peer_name: str | None = None
    body: str
    channel: str = "imessage"
    timestamp: datetime
    # Provenance: which ingestion source produced this message
    # (prompt2 §49 — retention is per-source).
    source: str = "personal-relay"


class Conversation(BaseModel):
    """A reconstructed message thread (prompt2 §76 — thread reconstruction).

    A conversation is keyed by (peer, channel): one thread per contact per
    channel. ``unread`` counts INBOUND messages not yet acknowledged, which
    drives the AttentionItem the thread can produce (§76 acceptance).
    """

    conversation_id: str
    peer_identifier: str
    peer_name: str | None = None
    channel: str = "imessage"
    message_count: int = 0
    unread: int = 0
    last_body: str | None = None
    last_direction: MessageDirection | None = None
    last_timestamp: datetime | None = None
    attention_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CalendarEvent(BaseModel):
    """A normalized calendar event (prompt2 §42 Calendar Integration).

    NerveCenter never owns the calendar — the events are synced/read-only
    copies from the user's provider (Apple/Google/Microsoft/CalDAV, §42) or
    ingested as normalized entries. The calendar exists to answer
    *availability* questions: availability, appointments, deadlines,
    travel time, meeting prep, follow-up timing (§42), and conflict
    reasoning such as "Dinner tomorrow?" → "I can do after 7:30" (§43).

    ``start``/``end`` are ``None`` for all-day events (``all_day=True``);
    those block the entire calendar day.
    """

    event_id: str
    title: str
    description: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    location: str | None = None
    participants: list[str] = Field(default_factory=list)
    reminder_at: datetime | None = None
    source: str = "calendar-sync"
    sync_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Email (prompt2 Phase P0 — Email + Attention)
# ---------------------------------------------------------------------------


class EmailDirection(StrEnum):
    """Which side of the thread the email came from (prompt2 §75)."""

    RECEIVED = "RECEIVED"
    SENT = "SENT"


class EmailMessage(BaseModel):
    """One ingested email (prompt2 §75 — email ingestion).

    The macOS Personal Relay (``nerve-personal-relay``) posts normalized email
    events here; NerveCenter never touches a mailbox (EWS/IMAP) directly
    (prompt2 §47). ``from_address`` is the raw sender address; ``sender_name``
    is the resolved display name, which may be ``None`` when the sender has
    not been matched yet.
    """

    message_id: str
    thread_id: str
    direction: EmailDirection
    from_address: str
    to_address: str
    subject: str
    body: str = ""
    timestamp: datetime
    # Provenance: which ingestion source produced this message
    # (prompt2 §49 — retention is per-source).
    source: str = "personal-relay"


class EmailThread(BaseModel):
    """A reconstructed email thread (prompt2 §75 — thread reconstruction).

    A thread is keyed by (sender, subject): one thread per sender per
    subject line. ``unread`` counts RECEIVED messages not yet acknowledged,
    which drives the AttentionItem the thread can produce (§75 acceptance).
    """

    thread_id: str
    from_address: str
    to_address: str
    sender_name: str | None = None
    subject: str
    message_count: int = 0
    unread: int = 0
    last_body: str | None = None
    last_direction: EmailDirection | None = None
    last_timestamp: datetime | None = None
    attention_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Mail connectors (prompt2 §46 — connector registry + liveness)
# ---------------------------------------------------------------------------


class MailAuthMethod(StrEnum):
    """How a connector authenticates to its mailbox (§47).

    The *secret* (OAuth access token / app password / password) is never
    persisted by NerveCenter — it is supplied by the browser/relay at
    connection-test time and only a *reference* to where it is held
    (``token_ref``) is stored. This keeps the backend secret-free; the macOS
    Personal Relay is the only long-lived holder of mail credentials (§47).
    """

    OAUTH2 = "oauth2"
    APP_PASSWORD = "app-password"
    BASIC = "basic"


class MailConnectorStatus(StrEnum):
    """Liveness of a connector as last probed."""

    PENDING = "pending"          # registered, never probed
    CONNECTED = "connected"      # last probe succeeded (CAPABILITY ok)
    AUTH_EXPIRED = "auth-expired"  # token/password rejected — refresh needed
    ERROR = "error"              # probe failed (host/port/network)
    DISABLED = "disabled"        # user turned it off


class MailConnector(BaseModel):
    """A registered mail connector (prompt2 §46 — connector registry).

    This is a *description + liveness record*: where the relay should read
    from and whether that mailbox currently answers. NerveCenter does **not**
    hold the socket — the macOS Personal Relay does (§47). ``token_ref`` is an
    opaque pointer to where the credential is kept (keychain item id / vault
    path); it is never the secret itself.
    """

    connector_id: str
    account: str                                # human label: "Work", "Personal"…
    mailbox: str                                # mailbox / account id (user@domain)
    host: str                                   # IMAP host
    port: int = 993
    ssl: bool = True
    auth_method: MailAuthMethod = MailAuthMethod.OAUTH2
    status: MailConnectorStatus = MailConnectorStatus.PENDING
    token_ref: str | None = None                # keychain item id / vault path
    scope: str | None = None                    # OAuth scope (informational)
    last_checked_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Voicemail (prompt2 Phase P2 — Personal Operations Plane)
# ---------------------------------------------------------------------------


class VoicemailSource(StrEnum):
    """Provenance of an ingested voicemail (prompt2 §49 — retention is per-source).

    Which ingestion path produced the normalized event. The NerveCenter plane
    only ever sees these normalized sources — the raw capture (iOS Share Sheet,
    the carrier's voicemail email, or a manual import) is done by the Personal
    Relay and is *out of scope for this deliverable* (§46/§47). Typing the
    value as an enum makes an unknown source a Pydantic 422, not a stored error.
    """

    SHARE_SHEET = "share-sheet"
    CARRIER_EMAIL = "carrier-email"
    IMPORTED = "imported"


class Voicemail(BaseModel):
    """One ingested voicemail (prompt2 §77 — voicemail ingestion).

    The macOS Personal Relay posts normalized voicemail events here with the
    transcript and provenance *already computed* (Whisper transcription, audio
    file storage, and carrier access are all source-side deliverables,
    §46/§47 — out of scope for this deliverable). ``caller_phone`` is the raw
    caller number in its display format; the thread key is derived by stripping
    non-digits, so the display format is kept separately. ``caller_name`` is
    the *resolved* display name, which may be ``None`` when the caller has not
    been matched yet.
    """

    voicemail_id: str
    thread_id: str
    caller_phone: str
    caller_name: str | None = None
    transcript: str = ""
    duration_seconds: int = 0
    received_at: datetime
    read: bool = False
    needs_callback: bool = False
    callback_reason: str | None = None
    # Provenance: which ingestion source produced this voicemail (prompt2 §49 —
    # retention is per-source).
    source: VoicemailSource = VoicemailSource.SHARE_SHEET


class VoicemailThread(BaseModel):
    """A reconstructed voicemail thread (prompt2 §77 — thread reconstruction).

    A thread is keyed by caller phone number (non-digits stripped for the key,
    display format kept separately): one thread per caller, chronological.
    ``unread`` counts received voicemails not yet acknowledged, which drives
    the AttentionItem the thread can produce (§77 acceptance). ``needs_callback``
    reflects the latest voicemail in the thread so a callback request surfaces
    on the list and its AttentionItem.
    """

    thread_id: str
    caller_phone: str
    caller_name: str | None = None
    voicemail_count: int = 0
    unread: int = 0
    last_transcript: str | None = None
    last_duration_seconds: int = 0
    last_received_at: datetime | None = None
    needs_callback: bool = False
    callback_reason: str | None = None
    attention_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Contacts (prompt2 Phase P4 — cross-channel person resolution + VIP)
# ---------------------------------------------------------------------------


class ContactSource(StrEnum):
    """Provenance of an ingested person record (prompt2 §49 — per-source).

    Which ingestion path produced the normalized contact record. The
    NerveCenter plane only ever sees these normalized sources — the raw
    capture (the macOS local address book, an import, or a manual entry) is
    done by the source-side Personal Relay and is *out of scope for this
    deliverable* (§46/§47). Typing the value as an enum makes an unknown
    source a Pydantic 422 at the boundary, not a stored error.
    """

    RELAY = "personal-relay"
    MANUAL = "manual"
    IMPORT = "import"


class Contact(BaseModel):
    """One resolved person (prompt2 §79 — unified person model).

    A contact is a cross-channel *person-resolution* record: ``person_id`` is
    the stable slug for the human (one person, not one address), and
    ``emails``/``phones`` are the merged address sets accumulated across
    channels. Re-ingesting the same ``person_id`` *updates* this record
    (idempotent upsert) rather than creating a duplicate. ``is_vip`` is the
    VIP flag the attention engine weighs heavier (§12/§22/§45); the contacts
    service only stores and returns it. ``source`` names the ingestion path
    that last produced/updated the record.
    """

    person_id: str
    display_name: str
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    notes: str | None = None
    is_vip: bool = False
    # Provenance: which ingestion source produced this person record
    # (prompt2 §49 — retention/provenance is per-source).
    source: ContactSource = ContactSource.RELAY
    created_at: datetime | None = None
    last_updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Waiting-On (prompt2 §19 — Waiting-On Detection / §31 "What am I waiting for?")
# ---------------------------------------------------------------------------


class WaitingOnSource(StrEnum):
    """Provenance of an outstanding request (prompt2 §19 — Waiting-On Detection).

    Which channel carried the *outgoing* request that received no response.
    The relay/personal-ops layers detect the outgoing request and post a
    normalized record to this plane; NerveCenter only tracks it, so the raw
    channel access (mailbox, message store, carrier) stays source-side and is
    *out of scope for this deliverable* (§46/§47). Typing the value as an
    enum makes an unknown source a Pydantic 422 at the boundary, not a
    stored error.
    """

    MESSAGES = "messages"
    EMAIL = "email"
    VOICEMAIL = "voicemail"
    MANUAL = "manual"


class WaitingOnStatus(StrEnum):
    """Lifecycle of an outstanding request (prompt2 §19).

    ``OPEN`` requests are live and each carry exactly one live
    AttentionItem on the Now screen; ``RESOLVED`` when the counterpart
    answered (or the matter closed); ``CANCELLED`` when the request no
    longer matters.
    """

    OPEN = "open"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class WaitingOnItem(BaseModel):
    """One outgoing request that received no response (prompt2 §19).

    The §19 example: you asked Sam for the deployment credentials; 3 days,
    no response → "WAITING ON / Sam / Deployment credentials / 3 days /
    [Follow up]". ``request_id`` is the stable key the relay posts for
    idempotent ingest (re-posting a known id is a no-op — §48 replay
    safety). ``person_display_name`` is the human to follow up with
    (required); ``person_id`` optionally links that human to the contacts
    plane's resolved person (§79). ``attention_id`` carries the one live
    AttentionItem an OPEN request produces on the Now screen (§21/§22);
    resolving the request resolves the item.
    """

    request_id: str
    person_display_name: str
    # Optional link to the contacts plane's resolved person (§79).
    person_id: str | None = None
    # What was requested, e.g. "deployment credentials" (§19).
    description: str
    requested_at: datetime
    # Provenance: which channel carried the outgoing request (prompt2 §49 —
    # per-source provenance convention).
    source: WaitingOnSource = WaitingOnSource.MESSAGES
    # The thread/conversation the outgoing request lives in, if known.
    source_thread_id: str | None = None
    status: WaitingOnStatus = WaitingOnStatus.OPEN
    resolved_at: datetime | None = None
    note: str | None = None
    # Follow-up bookkeeping (§19 [Follow up] action; §13 drafts, not sends).
    follow_up_count: int = 0
    last_follow_up_at: datetime | None = None
    # The one live AttentionItem this OPEN request produces on /now.
    attention_id: str | None = None
    # Optional escalation policy (mirrors the P0 AttentionItem contract,
    # §22/§45): how/when this request escalates. ``{"priority": "urgent"}``
    # means "follow up within 24h, or escalate to a VIP if unresolved".
    # Producer-supplied (the relay posts it); NerveCenter only stores and
    # surfaces it, so it defaults to ``None`` when absent.
    escalation_policy: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Briefing (prompt2 §30 — Briefings)
# ---------------------------------------------------------------------------


class BriefingKind(StrEnum):
    """Which briefing of the day (prompt2 §30 — Briefings).

    ``MORNING`` is the full daily brief (the §30 "Morning Brief" example);
    ``MIDDAY`` is the optional midday check-in; ``EVENING`` is the wrap-up.
    The §30 output shape is identical for all three — the same four sections
    — so the kind is echoed back as metadata, not a filter. An unknown kind is
    a Pydantic 422 at the boundary, not a stored error.
    """

    MORNING = "morning"
    MIDDAY = "midday"
    EVENING = "evening"


class BriefingAction(BaseModel):
    """One action item for the briefing (prompt2 §30 — "things need action").

    A flat projection of an :class:`AttentionItem` from the Now bucket (§26):
    ``label`` is the human-readable action (the item's ``suggested_action``
    when it has one, otherwise its ``reason``), and ``due`` carries the item's
    due timestamp so the screen can render "before 5 PM" style framing.
    """

    attention_id: str
    label: str
    due: datetime | None = None


class BriefingWaitingOn(BaseModel):
    """One outstanding request for the briefing (prompt2 §30 — "you're waiting on").

    A flat projection of an OPEN :class:`WaitingOnItem`: the §19 example
    "WAITING ON / Sam / Deployment credentials / 3 days" rendered inline.
    ``days_elapsed`` is computed deterministically from the briefing's
    injected clock (never the wall clock), so the count is exact in tests.
    """

    request_id: str
    person: str
    description: str
    days_elapsed: int


class BriefingEvent(BaseModel):
    """One of today's calendar events for the briefing (prompt2 §30 — "today").

    A flat projection of a :class:`CalendarEvent` that overlaps today: the
    title for the line and its start for the "6:30 PM" framing. All-day events
    carry ``start`` at 00:00 UTC of that day.
    """

    event_id: str
    title: str
    start: datetime | None = None


class BriefingOvernight(BaseModel):
    """The overnight activity counts for the briefing (prompt2 §30 — "overnight").

    "What happened while I slept?" (§31): how much new activity arrived on
    each plane in the last 24 hours, counted deterministically from the
    briefing's injected clock. ``emails`` counts threads active since
    ``now - 24h`` (email's ``last_timestamp``), ``messages`` conversations the
    same way (``last_timestamp``), ``voicemails`` threads by their
    ``last_received_at``.
    """

    emails: int = 0
    messages: int = 0
    voicemails: int = 0


class Briefing(BaseModel):
    """The composed Briefing (prompt2 §30 — Briefings).

    A read-only, deterministic aggregation across the attention, waiting-on,
    email, messages, voicemail, and calendar planes. It owns *no state of its
    own* — every section is projected from already-committed plane state at
    compose time, and ``now`` is injectable so the same fixtures produce the
    same brief. ``empty`` is True when no section has content, which lets the
    screen render the §30 honest closing ("Nothing else requires attention.")
    unambiguously.
    """

    kind: BriefingKind
    generated_at: datetime
    actions: list[BriefingAction] = Field(default_factory=list)
    waiting_on: list[BriefingWaitingOn] = Field(default_factory=list)
    today: list[BriefingEvent] = Field(default_factory=list)
    overnight: BriefingOvernight = Field(default_factory=BriefingOvernight)
    empty: bool = False


# ---------------------------------------------------------------------------
# Daily Operational State (prompt2 §52 — AC-R17, R109)
# ---------------------------------------------------------------------------
class OperationalState(BaseModel):
    """The composed Daily Operational State (prompt2 §52 — AC-R17).

    A flat, read-only rollup consumed by the ``nerve today`` CLI command
    (AC-R11) and the §64 ``get_daily_brief`` MCP tool (AC-R10). It is *not*
    wrapped in the §46 ``ListResponse`` envelope — the CLI reads the bare
    object — and it owns no state: every section is a flat projection of its
    owning service at compose time. ``as_of`` pins the clock the time-relative
    sections were computed against, so the rollup is deterministic and
    reproducible. Sections whose owning service is not wired (no
    ``NERVE_DB_URL``) render as empty lists, never invented data.
    """

    as_of: datetime
    open_commitments: list[dict[str, Any]] = Field(default_factory=list)
    incoming_requests: list[dict[str, Any]] = Field(default_factory=list)
    waiting_on: list[dict[str, Any]] = Field(default_factory=list)
    upcoming_deadlines: list[dict[str, Any]] = Field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = Field(default_factory=list)
    calendar_today: list[dict[str, Any]] = Field(default_factory=list)
    active_incidents: list[dict[str, Any]] = Field(default_factory=list)
    blocked_goals: list[dict[str, Any]] = Field(default_factory=list)
    empty: bool = False
