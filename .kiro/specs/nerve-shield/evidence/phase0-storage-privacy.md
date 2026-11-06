# Phase 0.4 — Storage encryption / PII / DB / isolation / backups audit

**Task:** 0.4 Audit object-store encryption, PII redaction, DB/migrations, account isolation, backups, deployment config. **Req:** R-017, R-023. **Depends:** 0.1.
**Legend:** `[VERIFIED]` code/config at HEAD; `[INFERRED]`; `[UNKNOWN]` = unverifiable from the repo.
**Sources:** `backend/storage/{config,contracts,s3,memory,errors}.py`, `backend/persistence/alembic/versions/*`, `backend/persistence/models/*`, `backend/ingest/mail_worker.py`.

## Summary answer

**Object-store encryption is NOT configured or implemented in code.** `[VERIFIED]` The `S3ObjectStore` writes **plaintext bytes** with AWS SigV4 request signing; there is no client-side encryption and no server-side-encryption (SSE) header is ever set. Encryption-at-rest therefore depends entirely on bucket-side configuration that does **not** live in this repo, and transport is **plaintext HTTP** whenever the endpoint scheme is `http://` (the default `secure` flag follows the scheme). The design's own warning — *"a `content_ref` does not prove encryption"* — is confirmed true at HEAD. For NerveShield, Zone P0 raw-content encryption is an **unverifiable guarantee** today.

## Object store — what is actually configured `[VERIFIED]`

`backend/storage/config.py`:
- Env contract: `NERVECENTER_OBJECT_STORE_URL` (schemes `s3://`/`http://`/`https://`/`memory://`), `_BUCKET`, `_ACCESS_KEY`, `_SECRET_KEY`, `_REGION` (default `us-east-1`), `_SECURE` (`0`/`1` override).
- **Fail-honest:** missing URL → `ObjectStoreNotConfigured`; S3 endpoint missing bucket/creds → `ObjectStoreConfigError(missing=…)`. Never silently substitutes memory. `[VERIFIED]`
- `secure = scheme != "http"` → **`http://` endpoints are dialed in plaintext** unless `_SECURE=1`. `[VERIFIED]` (TLS for `s3://`/`https://`.)

`backend/storage/s3.py`:
- `put(key, data, content_type, metadata)` PUTs raw `data` bytes; headers = `x-amz-meta-*` + optional `Content-Type` + SigV4 auth. **No `x-amz-server-side-encryption` header, no SSE-KMS, no client-side crypto.** `[VERIFIED]`
- No key management, no envelope encryption, no per-account key scoping. `[VERIFIED]`

## Data-classification matrix (NerveShield zones vs HEAD reality)

| Zone | Content | Where at HEAD | Encryption at HEAD | Status |
| --- | --- | --- | --- | --- |
| P0 raw body | mail `content_bytes` (decoded body, mislabeled `message/rfc822`) | object store key `mail/{conn}/{sha}` | **none in code**; bucket SSE `[UNKNOWN]`; `http://`=plaintext transit | **unverifiable guarantee** |
| P1 derived personal | person identities, flags (booleans), `content_ref` | Postgres `communications`, `person_identities` | DB-level only `[UNKNOWN]` | partial |
| P2 metadata/verdicts | *(NerveShield assessments — do not exist yet)* | — | — | N/A (Phase 1) |
| P3 datasets | none | — | — | N/A |
| P4 weights/vectors | pgvector enabled (dep) but no mail model | — | — | N/A |

## PII redaction `[VERIFIED]`

- **Transport errors:** `redact_transport_error` → `backend.connectors.redact.redact_message` strips `user:pass@` userinfo and token-shaped substrings, bounds length. Applied before any `last_error_message` persist/log. `[VERIFIED]`
- **Flags are boolean-only:** `classify_mail_flags` returns `{bulk, automated, no_reply}` — never a raw header value or body (§7). `[VERIFIED]`
- **Durable attention/commitment rows** carry templated, provenance-only strings; quoted body fragments stay on the in-memory finding only (P0-I). `[VERIFIED]` **Caveat:** the in-memory `AttentionItem.reason` DOES embed `finding.matched` (a body fragment) — see 0.3 bypass #2; not persisted, but reaches the UI/API read model.
- **No raw body/subject/address in events:** §8 event payload (`_finding_payload`) is metadata + `matched` (a quoted fragment). `matched` on the bus event IS a body fragment — flag for R-022.4/T-14 when a security card/events are added.

## DB / migrations / tenancy `[VERIFIED]`

- Alembic, forward-numbered `001_initial` … `015_inference_providers`; mail/communications tables in `006_personal_plane.py` (`people`, `person_identities`, `communications`, `attention_items`, …).
- **`communications` has NO `account_id` / tenant column** (`006`: columns include `content_ref String(512)`, `person_id` FK, `channel`, `direction`, `received_at`, `subject`, `flags`). Isolation is by `person_id` and (for the nc_platform resources/principals) a `namespace` column. `[VERIFIED]`
- The PDP (`backend/policy/pdp.py`) enforces **namespace isolation** as a platform invariant (deny on principal/resource namespace mismatch) — but that governs `nc://` resources, **not** the mail `communications` rows, which are not account-namespaced. `[VERIFIED]`
- Existing per-account pattern DOES exist elsewhere: `calendar_events.account_id` FK + `ix_calendar_events_account_id` (`personal.py`). `[VERIFIED]` → a usable precedent for account-scoping NerveShield tables.

## Account isolation assessment (R-023)

| R-023 expectation | HEAD reality | Status |
| --- | --- | --- |
| Same message in 2 accounts kept separate | mail `communications` keyed by deterministic `person_id`(handle) + `communication_id`(connection+mail-id). Connection differs per account, so `communication_id` differs; but **person id is per normalized handle, shared across accounts** | **partial / risk** `[VERIFIED]` |
| Deny cross-account read by id/hash | no account column to scope mail reads; nc_platform namespace isolation exists but is not applied to `communications` | **GAP** (T-15) |
| Revocation cleanup | connection delete exists; derived-record cleanup plan `[UNKNOWN]` | unverified |

**Unverifiable guarantees (must be recorded, R-025.2):** (a) object-store encryption-at-rest; (b) TLS for object-store traffic in the deployed config; (c) DB encryption-at-rest; (d) backup contents/encryption; (e) key custody/rotation. None are provable from repo code/config → all `NOT DEPLOYED / UNKNOWN` until deployment manifests are inspected.

## Backups / deployment config `[UNKNOWN]`

No backup or object-store SSE configuration is present in the audited Python/config surface. Deployment lives under `deployments/` (ruff-excluded, not audited here) — `[INFERRED]` likely holds k3s/Nix manifests, but the audit scope did not open them; object-store SSE and DB/backup encryption remain `UNKNOWN` pending that review.

## Done (0.4): data-classification matrix delivered; assertions backed by code (s3.py/config.py/migrations), not comments; unverifiable guarantees listed. ✅
