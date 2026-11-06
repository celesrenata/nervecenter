# Phase 0.3 — Ingest / detection / attention transaction + cursor semantics

**Task:** 0.3 Trace ingest→detection→attention + cursor/durable semantics; event-order diagrams; security bypass paths. **Req:** R-001, R-011, R-012, R-018. **Depends:** 0.1.
**Legend:** `[VERIFIED]` observed at HEAD; `[INFERRED]` reasoned.
**Sources:** `backend/ingest/mail_worker.py`, `backend/communications/detection/{service,wiring}.py`, `backend/api/endpoints/communications.py`.

## Summary answer (the bypass)

**YES — an attention item CAN today be minted directly from uninspected, untrusted email body text, with no security evaluation, on BOTH ingest paths.** `[VERIFIED]`

The flow is: raw body → object store → `DetectionService.register_content` (transient) → deterministic extractors over the body text → findings → `CommunicationPipeline.ingest` → `_persist_attention(...)` writes an INCOMING `request` finding into the durable `attention_items` table (reaching `GET /api/today`), and `DetectionService._feed_attention` creates an in-memory live `AttentionItem`. Nothing inspects the message for security first. This is exactly the condition R-012.5 / I-12 / threat **T-26** ("Untrusted email seen as authorizing workflow") and the design §8.3 warning address. **In SHADOW the NerveShield gate must sit BEFORE this promotion (design §4.6/§8.3), which means Phase 7's `REVIEW_GATED` seam, not a Phase 1 change — Phase 1 stays shadow-only and must not alter this path.**

## Exact per-poll sequencing — WORKER path (`MailIngestWorker._poll` → `_process_batch`) `[VERIFIED]`

1. Re-read connection + durable cursor from DB (never memory).
2. Resolve transient secret (`access_token` or `username/password`); never persisted.
3. `_run_transport_once` → `transport.run(connector, cursor, credentials=…)` returns `(items, next_cursor)`.
4. For each item in `_process_batch` (dedup by `seen` list, `{account}:{message_id}`):
   a. `object_store.put(key, content_bytes, …)` — body to object store.
   b. derive `communication_id`, `content_ref`, `subject`, sender, `classify_mail_flags`, deterministic `person_id`.
   c. `seam.ingest_communication(payload)` — in-memory upsert (sensitivity hardcoded `"PERSONAL"`).
   d. `persist_durable_communication(...)` — durable person + communication (captured, surfaced via `flush_durable`).
   e. `register_content(content_ref, body_text)` — reads body back from object store, hands **decoded text** to the detector transiently.
   f. **`seam.run_pipeline(communication, now=…)`** → `CommunicationPipeline.ingest` (detection + follow-up + attention). **← security-relevant promotion happens here.**
5. **P0-D commit-gate:** `await seam.flush_durable()`. If a durable failure → set connection `operational_status=error`, `last_error_code=commit_failed`, **no cursor advance, no seen fold** → transport re-delivers; idempotency (deterministic ids) absorbs the partial commit.
6. Only on clean flush: `imports.set_cursor(…, cursor_with_seen(next_cursor, seen))`, record import-job progress, mark connection healthy/authenticated.

**Cursor contract `[VERIFIED]`:** cursor advances **only after durable commit** (P0-D), never on successful external completion. Idempotent-for-a-given-cursor (module docstring + `seen` list, capped `MAX_SEEN_KEYS=2000`). Satisfies R-018.3 (crash between persist and ack recovers) and T-17.

## Exact sequencing — API path (`POST /api/communications/ingest`) `[VERIFIED]`

1. Validate `IngestCommunicationRequest` (sensitivity default `PERSONAL`).
2. `service.ingest_communication(payload)` — in-memory upsert.
3. `_persist_communication_durably(communication)` — **best-effort** (no cursor to gate; failure logged + surfaced via response `durable` flag, request still 200/201). **Intentional asymmetry** vs worker (documented P1-107 finding #3).
4. **`get_pipeline().ingest(communication, now=…)`** (line 379) — same promotion path as the worker. **← same bypass.**

Note: the API path has **no `register_content` step**, so unless content was registered elsewhere the detector sees empty text and yields no findings. The *worker* path does register content, so the worker is the live bypass; the API path becomes a bypass for any caller that also registers content.

## What `CommunicationPipeline.ingest` does `[VERIFIED]`

1. `detection.detect(communication, now)` → `DetectionReport` (findings + §8 events + `_feed_attention` creating **in-memory** AttentionItems per finding).
2. `_persist_commitments(findings)` — mirrors `commitment` findings to durable `commitments` (background task; idempotent by `<kind>:<finding_id>`; skipped if no `person_id`).
3. **`_persist_attention([f for f in findings if not _follows_up(f)])`** — mirrors findings NOT already covered by follow-up→attention (chiefly **INCOMING `request`** findings) into durable `attention_items` via deterministic id `at-<sha12>` (get-or-create → idempotent on replay). **This is the durable Today-visible mint from body text.**
4. For OUTGOING commitment/request with a due: `_ensure_followup` (idempotent by deterministic `created_from` key).
5. `evaluate_due(now)` promotes due follow-ups to live AttentionItems; links refreshed.

## Event-order diagram — worker happy path `[VERIFIED]`

```
transport.run
   └─ items
      └─ for each item:
         put(body)->objstore
         ingest_communication (in-mem)
         persist_durable_communication (captured)
         register_content(body text)          # transient
         run_pipeline:
            detect ──> findings + §8 events (*.detected) + in-mem AttentionItems
            persist_commitments  ──> durable commitments (bg, idempotent)
            persist_attention    ──> durable attention_items (idempotent) # Today-visible
            ensure_followup/evaluate_due ──> live AttentionItems
   flush_durable  ── fail ──> commit_failed, NO cursor advance (replay)
                  └ ok ──> set_cursor(next, seen)  ; healthy
```

## Replay / failure semantics enumerated `[VERIFIED]`

| Scenario | Behavior | Invariant |
| --- | --- | --- |
| Crash after `put` before `set_cursor` | cursor not advanced → transport re-delivers; `seen` + deterministic ids dedup | R-018.3, T-17, I-11 |
| Durable write fails | `flush_durable` returns exc → `commit_failed`, no cursor advance, retry | R-001.3, I-07 |
| Same message twice | deterministic `communication_id`/`person_id`/`at-<sha>`/`ct-<sha>` → get-or-create no-op | R-018.2, T-16 |
| Transport auth error | one OAuth refresh (single-flight lock) then `auth_expired`; no cursor advance | — |
| Transport connect/provider error | `operational_status=error` + redacted code; backoff; no cursor advance | T-14 (redaction) |
| Duplicate across two Gmail accounts | **GAP:** `communications` table has **no `account_id`**; dedup/seen is per connection `account` string, person id is per normalized handle — same message to 2 accounts could collide on person/communication ids | **R-023 / T-15 risk — see 0.4** |

## Security bypass paths (ranked)

1. **`CommunicationPipeline._persist_attention` + `_feed_attention`** mint attention/Today items from body-derived findings with **zero** security evaluation. `[VERIFIED]` — primary T-26 bypass. Shadow NerveShield must gate *before* this (Phase 7 `REVIEW_GATED`), proven across worker **and** API paths.
2. **Finding titles/`why`/`matched`** carry **quoted body fragments** on the in-memory finding (`_attention_payload` uses `finding.matched`). The durable mirrors are templated/provenance-only (P0-I), but the **in-memory** AttentionItem reason embeds `finding.matched` — a body fragment reaching a user-visible surface. `[VERIFIED]` — relevant to T-14/R-022.4 (keep attacker-controlled text out of alert titles). Durable rows are safe; in-memory/UI path must be checked when the security card is added.
3. **No security disposition anywhere** — detection has only relevance/importance axes, never threat/auth axes. `[VERIFIED]`

## Done (0.3): no guessed integrations; replay/failure semantics enumerated; bypass identified on both paths. ✅
