# Contract Notes

## NerveModels

### RecordBase
All persisted records share these fields:
- `id`: UUID string (stable unique identifier)
- `created_at`: ISO-8601 timestamp (UTC)
- `schema_version`: Always "v0"
- `hash`: Optional SHA-256 digest of canonical JSON (excluding this field)

### Event
- `event_type`: String (e.g., "entity.created")
- `category`: One of `system|knowledge|evidence|goal|task|inference|policy|orchestration|connector`
- `occurred_at`: ISO-8601 timestamp
- `source`: Agent ID, connector ID, or "internal"
- `subject`: Optional entity ID
- `payload`: `[String: AnyCodable]` - type-specific document
- `severity`: One of `debug|info|warning|error|critical`
- `trace_id`, `correlation_id`: Optional correlation headers
- Integrity (spec §10 `hash`): `computeIntegrityHash()` -> lowercase hex SHA-256
  (CryptoKit) over the canonical wire serialization of all fields **except
  `hash`** (snake_case keys, ISO-8601 dates, sorted keys).
  `verifyIntegrity()` -> `true` when `hash` is nil (unsigned) or matches the
  recomputed digest; `stampIntegrity()` stamps the digest.

### Task
- `title`: Short human-readable task title
- `description`: Optional full description
- `status`: One of `pending|scheduled|in_progress|completed|failed|cancelled`
- `priority`: Integer (0-10)
- `goal_id`: Optional goal ID
- `dependencies`: List of task IDs this task depends on
- `inputs`: `[String: AnyCodable]` - executor-specific inputs

### Goal
- `title`: Short statement of the goal
- `status`: One of `active|paused|achieved|abandoned`
- `progress`: Float (0.0-1.0)
- `success_criteria`: List of criteria strings
- `tasks`: List of task IDs

### Claim
- `statement`: The claim, as a single sentence
- `status`: One of `open|supported|contradicted|superseded|rejected`
- `confidence`: Float (0.0-1.0)
- `classification`: One of `FACT|DERIVED_FACT|HYPOTHESIS|RECOMMENDATION`
- `supporting_evidence`: List of evidence IDs

### SynchronizedState (Phone ↔ Watch)
- `system_health`: String (e.g., "healthy")
- `active_incidents`: Integer
- `running_agents`: Integer
- `pending_approvals`: Integer
- `primary_goal_progress`: Float (0.0-1.0)
- `updated_at`: ISO-8601 timestamp

## NerveAPI

### CorrelationMetadata
Serialized as HTTP headers:
- `nerve_trace_id`
- `nerve_goal_id`
- `nerve_task_id`
- `nerve_agent_id`
- `nerve_agent_run_id`
- `verification_policy`

### NerveAPIError
- `http(statusCode: Int, data: Data?)`
- `network(underlying: String)`
- `decoding(underlying: String)`
- `invalidConfiguration(String)`
- `timeout`
- `unexpectedResponse(String)`

## NerveAuth

### CredentialStore
Protocol for storing/retrieving device credentials.

### BiometricAuthenticator
Protocol for biometric authorization (spec §72).

### Approval
- `planned_change_id`: ID of the change being approved
- `approver_type`: One of `human|policy|orchestrator|agent`
- `decision`: One of `approved|denied|no_response`

## NervePersistence

### LocalCache (actor)
- `CachedState`: Compact phone↔watch sync snapshot
- Atomic file writes (`.atomic` replace on write)

## NerveRealtime

### RealtimeClient
Protocol for event streaming:
- WebSocket support
- SSE fallback

## NerveApps

### PersonalStateWire → App Group `cached-state.json` (spec §68/§69/§27)
The phone merges `PersonalStateWire` into the shared App Group
(`group.nerv.center`) `cached-state.json` via
`PhoneEnvironment.writePendingPersonalStateIfAny()` (loss-preserving:
existing top-level keys are kept). Wire keys are snake_case:
- `attention_count`: Integer (queue size; omitted when the fetch failed)
- `attention`: Array of attention items — the full open feed the Watch
  renders (`WatchAttentionJSON.decodeAttentionFile`). Each item:
  `attention_id`, `reason`, `urgency`, `importance`, `priority` (Float
  0.0-1.0), `due` (ISO-8601, optional), `status`
  (`NOW|TODAY|SOON|WAITING_ON|FYI|SUPPRESSED|RESOLVED`), `why` (list),
  `suggested_action` (optional), `entities` (list). `null` members are
  omitted; an absent `attention` key means "not published" (the watch
  shows "–"), while `[]` means "genuinely empty" ("✓ CLEAR").
- `top_attention_title` / `top_attention_urgency`: top item projection
- `top_commitment_title` / `top_commitment_due_at`: open follow-up
- `waiting_on_count`: Integer

### Watch attention wire (`WatchAttentionJSON`, spec §27/§60/§67)
- `cached-state.json` → `attention` key (above); `watch-actions.json`
  carries watch → phone action markers (`attention_id`, `action`
  `snooze|resolve|escalate`, `requested_at`, optional `snooze_minutes` /
  `note`). The engine (`WatchAttentionEngine`) drops `SUPPRESSED` /
  `RESOLVED` items from the live count; "n NEED YOU" counts open
  `NOW`-bucket items only.
