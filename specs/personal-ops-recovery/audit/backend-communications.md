# Backend Communications Audit — Personal-Ops Recovery

**Transcribed** from worker-1's inline audit result (`audit-backend-comms`) returned in the
parallel batch manifest.

| Field | Value |
| --- | --- |
| Batch id | `1ada822c-6ef8-426f-ae92-18a6294e7438` |
| Task name | `audit-backend-comms` |
| Worker | worker-1 (read-only, project-reader) |
| Date | 2026-10-27 |
| Method | Read-only source gathering. Worker-1 hit its read-only constraint, so it returned findings inline in the batch `manifest.json` (`tasks[].result`) instead of writing a file. This report faithfully transcribes that inline result; no source files were modified. |

Scope covered by worker-1: mail connectors, messages, email, voicemail, calendar, attention,
followups, communications/detection, waiting_on/briefing. Worker-1's inline result contains
evidence for the communications data model, identity/communication ingestion, person merge,
attribution, the detection pipeline, the overdue sweep, finding resolution, and pipeline wiring.
It contains **no** inline evidence for the voicemail, email, calendar, waiting_on, or briefing
subsystems — those rows are marked `Unverified` below rather than filled in from other
reports.

---

## Classification table

Subsystem classification labels are restricted to what worker-1's inline result states.
`Unverified` = no source in worker-1's manifest result.

| Subsystem | Classification | Evidence path:line | Data flow entry | Persistence | Gap to usable |
| --- | --- | --- | --- | --- | --- |
| Communications data model (channels, sensitivity, directions, identities, people, communications, conversations) | Implemented | `backend/communications/models.py` (non-empty `content_ref` validator at `backend/communications/models.py:188`) | N/A (model layer) | `content_ref` reference; bodies not embedded in findings/events | Unverified |
| Identity ingestion | Implemented | `CommunicationsService.ingest_identity()` — `backend/communications/registry.py:205` | `ingest_identity()` (normalizes/indexes identities) | Returns person/identity creation + proposed-candidate status | Unverified |
| Person merge | Implemented | `CommunicationsService.merge_people()` — `backend/communications/registry.py:306` | `merge_people()` (rejects self-merge; VIP logical OR; dedupes identities; reattributes communications; folds conversations; removes source person) | Mutations reattribute durable communications | Unverified |
| Communication ingestion | Implemented | `CommunicationsService.ingest_communication()` — `backend/communications/registry.py:381` | `ingest_communication()` (coerces enums, normalizes timestamps, generates missing IDs, upserts records, folds per-person/per-channel conversation) | Upsert of communication records | Unverified |
| Automatic attribution | Implemented | `CommunicationsService._attribute_person()` — `backend/communications/registry.py:459` | Uses channel peer identity | Person attributed only when identity resolution is `RESOLVED`; otherwise no person | Unverified |
| Detection service | Implemented | `backend/communications/detection/service.py:5` (docs: findings/events do not embed bodies); `DetectionService.detect()` — `backend/communications/detection/service.py:199` | `detect()` (suppresses marketing input; deterministic extraction; optional refine-only assist; emits finding events) | Stores only transient text behind `content_ref` | Unverified |
| Finding events | Implemented | `_finding_payload()` — `backend/communications/detection/service.py:122` | Emitted from `detect()` | Payload carries metadata only: content reference, status, confidence, due information, why, match fields | Unverified |
| Overdue sweep | Implemented | `DetectionService.sweep_overdue()` — `backend/communications/detection/service.py:341` | `sweep_overdue()` | Changes overdue commitments to `OVERDUE`, deadlines to `PASSING`, emits warning events | Does not sweep requests (stated limitation) |
| Finding resolution | Implemented | `DetectionService.resolve_finding()` — `backend/communications/detection/service.py:392` | `resolve_finding()` | Handles commitment/request resolution; updates linked attention when present | Unverified |
| Registry fallback for a communication | Implemented | `DetectionService._communication()` — `backend/communications/detection/service.py:385` | Internal fallback used by detection | Unverified | Unverified |
| Pipeline wiring | Implemented | `CommunicationPipeline.ingest()` — `backend/communications/detection/wiring.py:158`; `get_pipeline()` — `backend/communications/detection/wiring.py:277` | `ingest()` (detection → idempotent follow-up creation for qualifying outgoing commitment/request findings → due evaluation) | Process-wide composition binds detection, follow-up, and attention singletons | Unverified |
| Mail connectors | Partial (Relay-side discovery confirmed; web live UI absent) | `apple/Relay/CLI/main.swift:142-149`; `apple/Relay/Core/ChannelModels.swift:366-390` (Relay, real `GET /api/mail-connectors`; unconfigured state = empty list); `web/src/views/SettingsView.tsx:150-192` (web Settings holds documented instructions, no live connector flow) | `GET /api/mail-connectors` | Unverified | Web live connector flow Missing (per worker-2 inline result, cited in web-apple.md) |
| Messages | Not audited by worker-1 (no inline evidence) | Unverified | Unverified | Unverified | Unverified |
| Email | Not audited by worker-1 (no inline evidence) | Unverified | Unverified | Unverified | Unverified |
| Voicemail | Not audited by worker-1 (no inline evidence) | Unverified | Unverified | Unverified | Unverified |
| Calendar | Not audited by worker-1 (no inline evidence) | Unverified | Unverified | Unverified | Unverified |
| Attention | Not audited by worker-1 (no inline evidence; detection wires the attention singleton per `wiring.py:277`) | Partial | Unverified | Unverified | Unverified |
| Followups | Not audited by worker-1 (no inline evidence; pipeline creates follow-ups idempotently per `wiring.py:158`) | Partial | Unverified | Unverified | Unverified |
| Waiting_on / briefing | Not audited by worker-1 (no inline evidence) | Unverified | Unverified | Unverified | Unverified |

---

## Per-subsystem findings

Faithful transcription of worker-1's inline result, one bullet per source finding.

### Unified data model
- The unified data model defines normalized channels, sensitivity labels, directions,
  contact-identity kinds, people, communications, and conversations in
  [`backend/communications/models.py`](../../../backend/communications/models.py), including the
  non-empty `content_ref` validator in
  [`Communication._content_ref_must_not_be_empty()`](../../../backend/communications/models.py:188).

### Identity ingestion and person merge
- Identity ingestion normalizes/indexes identities and returns person/identity creation and
  proposed-candidate status in
  [`CommunicationsService.ingest_identity()`](../../../backend/communications/registry.py:205).
- Person merge rejects self-merge, preserves VIP state with logical OR, deduplicates identities,
  reattributes communications, folds conversations, and removes the source person in
  [`CommunicationsService.merge_people()`](../../../backend/communications/registry.py:306).

### Communication ingestion and attribution
- Communication ingest coerces enum fields, normalizes timestamps, generates missing IDs,
  attributes when resolvable, upserts records, and folds the per-person/per-channel conversation
  in [`CommunicationsService.ingest_communication()`](../../../backend/communications/registry.py:381).
- Automatic attribution uses the channel peer identity and returns no person unless identity
  resolution is `RESOLVED` in
  [`CommunicationsService._attribute_person()`](../../../backend/communications/registry.py:459).

### Detection
- Detection stores only transient text behind `content_ref`; the service documentation states
  findings/events do not embed bodies in
  [`backend/communications/detection/service.py`](../../../backend/communications/detection/service.py:5).
- [`DetectionService.detect()`](../../../backend/communications/detection/service.py:199)
  suppresses marketing input, executes deterministic extraction, applies optional refine-only
  assist, and emits finding events.
- Finding event payloads carry metadata including the content reference, status, confidence, due
  information, why, and match fields in
  [`_finding_payload()`](../../../backend/communications/detection/service.py:122).

### Overdue sweep and resolution
- The overdue sweep changes overdue commitments to `OVERDUE`, deadlines to `PASSING`, emits
  warning events, and does not sweep requests in
  [`DetectionService.sweep_overdue()`](../../../backend/communications/detection/service.py:341).
- [`DetectionService.resolve_finding()`](../../../backend/communications/detection/service.py:392)
  handles commitment/request resolution and updates linked attention when present.
- Registry fallback for a communication is implemented in
  [`DetectionService._communication()`](../../../backend/communications/detection/service.py:385).

### Pipeline wiring
- The injected pipeline performs detection, idempotent follow-up creation for qualifying outgoing
  commitment/request findings, and due evaluation in
  [`CommunicationPipeline.ingest()`](../../../backend/communications/detection/wiring.py:158).
- Its process-wide composition binds detection, follow-up, and attention singletons in
  [`get_pipeline()`](../../../backend/communications/detection/wiring.py:277).

---

## Ingest entry points inventory

| Entry point | Kind | Path:line | Notes (from worker-1) |
| --- | --- | --- | --- |
| `CommunicationsService.ingest_identity()` | service method | `backend/communications/registry.py:205` | Normalizes/indexes identities; returns person/identity creation and proposed-candidate status |
| `CommunicationsService.merge_people()` | service method | `backend/communications/registry.py:306` | Person merge (self-merge rejected; VIP OR; reattribution; conversation fold) |
| `CommunicationsService.ingest_communication()` | service method | `backend/communications/registry.py:381` | Enum coercion, timestamp normalization, ID generation, upsert, conversation fold |
| `CommunicationsService._attribute_person()` | internal | `backend/communications/registry.py:459` | Channel-peer attribution; person only when resolution is `RESOLVED` |
| `DetectionService.detect()` | service method | `backend/communications/detection/service.py:199` | Deterministic extraction + optional refine-only assist; emits finding events |
| `CommunicationPipeline.ingest()` | pipeline | `backend/communications/detection/wiring.py:158` | Detection → idempotent follow-up creation → due evaluation |
| `get_pipeline()` | composition root | `backend/communications/detection/wiring.py:277` | Binds detection, follow-up, and attention singletons |

HTTP-level mail-connector/mail-ingest endpoints (`GET /api/mail-connectors`,
`POST /api/messages/ingest`, `/api/email/ingest`) are documented in worker-2's result — see
[web-apple.md](web-apple.md).

---

## Persistence gaps

From worker-1's inline result only:

- **Content bodies are not persisted in the communications plane.** Detection stores only
  transient text behind `content_ref`; findings/events do not embed bodies
  (`backend/communications/detection/service.py:5`; `:122`). Body durability therefore depends
  on the object/reference layer, which this audit did not cover — **Unverified**.
- **Person attribution leaves unresolvable communications unattributed.** `_attribute_person()`
  returns no person unless identity resolution is `RESOLVED`
  (`backend/communications/registry.py:459`); the handling/persistence of such unattributed
  records beyond that behavior is **Unverified**.
- **Overdue sweep excludes requests.** `sweep_overdue()` handles commitments/deadlines only,
  explicitly not requests (`backend/communications/detection/service.py:341`) — whether requests
  need sweep coverage is a spec question; **Unverified** from this audit.

General durable-boundary findings (ORM boundary, ephemeral vs durable state) are out of
worker-1's scope; see [persistence-infra.md](persistence-infra.md) for that audit.

---

## Recommendations for the Connection Center + ingestion lift

Derived only from the findings above (no new claims):

1. **Add a web Connection Center surface.** Worker-2's inline result confirms there is no
   `/connections` route/nav entry (`web/src/components/Layout.tsx:37-47`) and the web mail
   connector UI is documented-only, not live (`web/src/views/SettingsView.tsx:150-192`). The
   ingest entry points in the Ingest inventory are the backend contract the Connection Center
   should front. *(Source: worker-1 classification table row "Mail connectors"; web-apple.md.)*
2. **Keep `content_ref` as the single body-storage seam.** All detection artifacts already
   carry metadata plus a content reference (`detection/service.py:122`); the lift must not
   start embedding bodies into findings/events, which would break the stated contract
   (`detection/service.py:5`). *(Source: worker-1, Detection findings.)*
3. **Wire unresolvable-attribution handling into the lift.** Attribution currently yields no
   person unless `RESOLVED` (`registry.py:459`); the Connection Center's identity
   proposal/merge flow (`ingest_identity()` `registry.py:205`, `merge_people()`
   `registry.py:306`) is the natural home for resolving those communications. *(Source:
   worker-1, Attribution finding.)*
4. **Decide the request-sweep scope explicitly.** The sweep deliberately skips requests
   (`detection/service.py:341`); the spec pack should state whether requests get equivalent
   `OVERDUE`/`PASSING` treatment before the lift is built. *(Source: worker-1, Overdue sweep
   finding.)*
5. **Preserve pipeline invariants in any rewiring.** Detection → idempotent follow-up → due
   evaluation (`wiring.py:158`) and the singletons binding (`wiring.py:277`) are the current
   process-wide composition; changes there are cross-plane (detection, follow-up, attention).
   *(Source: worker-1, Pipeline wiring finding.)*
