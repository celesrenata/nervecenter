# Phase 0.2 — Gmail + IMAP source fidelity (EXP-01 matrix)

**Task:** 0.2 Trace Gmail/IMAP source fidelity. **Req:** R-002, R-004. **Depends:** 0.1.
**Legend:** `[VERIFIED]` observed in code at HEAD; `[INFERRED]` reasoned; `[UNKNOWN]` not provable from HEAD.
**Primary source:** `backend/connectors/mail/mail_transport.py`, `backend/ingest/mail_worker.py`, `backend/communications/detection/service.py`.

## Summary answer

At HEAD, **NerveCenter does NOT possess original RFC822 / full MIME bytes or DKIM signatures in any durably usable form.** The Gmail path fetches a *decoded text body only* (best `text/plain` else `text/html`) and the IMAP path fetches full RFC822 transiently (`BODY.PEEK[]`) but immediately reduces it to the decoded payload. Both store `MailItem.content_bytes` (decoded body text) to the object store; **attachments, raw MIME structure, and the full header set are discarded before persistence.** Headers are an allowlisted subset that **excludes** `Authentication-Results`, `Received`, `Return-Path`, and `DKIM-Signature`. Therefore SPF/DKIM/DMARC/ARC cannot be read, let alone independently verified — authentication evidence fidelity is `BODY_ONLY` / `METADATA_ONLY`, never `ORIGINAL_RFC822`.

## What `MailItem.content_bytes` actually contains `[VERIFIED]`

`MailItem` (frozen dataclass, `mail_transport.py`): `message_id, account, subject, content_bytes, timestamp, headers, source`.

- **Gmail (`GmailClient`)** `[VERIFIED]`: uses the **history resource + `users/me/messages` + `messages.get`**, and `_extract_text_body(payload)` walks `payload.parts`, preferring `text/plain` over `text/html`, decoding `part.body.data` (base64url). `content_bytes` = that decoded text, UTF-8 encoded. The module comment is explicit: attachment parts carry `attachmentId` and **no inline `data`**, so they are skipped. **No `format=raw` is requested anywhere** — so full RFC822 is never fetched on the Gmail path.
- **IMAP (`ImapPollClient`)** `[VERIFIED]`: fetches `UID … (BODY.PEEK[])` → full RFC822 bytes land in `raw` transiently, parsed via `email.message_from_bytes(raw)`. But `content_bytes` = `_payload_bytes(message, raw)` = `message.get_payload(decode=True)` (the **decoded body**, falling back to raw only if decode yields neither bytes nor str). The full `raw` is **not** returned on `MailItem`; only `content_bytes` + the allowlisted `headers` survive the function.

## Headers retained (allowlist) `[VERIFIED]`

Both transports keep exactly: `From, To, Subject, Date, Message-ID, In-Reply-To` plus the FEAT-001 bulk/automated set `List-Unsubscribe, List-Id, Precedence, Auto-Submitted`.

**Absent (critical for auth):** `Authentication-Results`, `Received`, `Return-Path`, `DKIM-Signature`, `ARC-*`, `Reply-To`. `[VERIFIED]` by reading the `headers={k: … for k,v in message.items() if k in (…)}` dict-comprehensions in both clients.

## Persistence path `[VERIFIED]`

`mail_worker._process_batch`: for each item, `object_store.put(key, content_bytes, content_type="message/rfc822", metadata={account, message_id})`. **The declared `content_type` is `message/rfc822` but the bytes are the decoded body text, not RFC822** — a fidelity mislabel to flag (NS-ADR-002 relevance). Key = `mail/{connection_id}/{sha256(connection:mail_id)[:24]}`. Graph keeps only `content://<key>`. Detection later reads it back via `register_content` → `DetectionService._text_for`.

## Trusted authentication evidence `[VERIFIED]`

There is **no** SPF/DKIM/DMARC/ARC parsing, verification, or storage anywhere in the mail path. `grep` for `Authentication-Results`, `DKIM`, `SPF`, `DMARC`, `ARC` in `backend/` yields nothing in connectors/ingest. Any future auth axis must default to `UNKNOWN` (R-002.5, R-004.5, threat T-28) because the evidence is not even captured.

## Evidence-fidelity matrix (EXP-01)

| Evidence element | Gmail path | IMAP path | Durably preserved? | Fidelity status |
| --- | --- | --- | --- | --- |
| Acquisition mode | history + `messages.get` (decoded parts) | `UID FETCH BODY.PEEK[]` (full RFC822 transient) | — | Gmail `PROVIDER_FULL`-ish body; IMAP `IMAP_RFC822` transient only |
| Full RFC822 bytes | **never fetched** (`[VERIFIED]`) | fetched transiently, **not persisted** (`[VERIFIED]`) | **No** | `BODY_ONLY` |
| MIME structure / parts | flattened to best text part | parsed then discarded | **No** | lost |
| Attachments (bytes/descriptors) | skipped (no inline data) | not extracted | **No** | `MISSING` |
| Selected headers | allowlist (6+4) | allowlist (6+4) | **Yes** (as `classification`/flags, booleans only) | partial |
| `Authentication-Results` | not captured | not captured | **No** | `UNKNOWN` (T-01/T-28) |
| `Received` / envelope IP | not captured | not captured | **No** | `UNKNOWN` |
| `Return-Path` / `Reply-To` | not captured | not captured | **No** | `UNKNOWN` |
| DKIM signature bytes | not captured | discarded with raw | **No** | cannot verify (R-002.5) |
| Source UID / history id | in durable cursor (`imap_seq`/`gmail_history`) | in durable cursor | **Yes** (cursor row) | `VERIFIED` position only |
| Folder/label | IMAP folder in cursor; Gmail `INBOX` label scoping | IMAP folder in cursor | partial | `PROVIDER_REPORTED` |
| `sha256` of stored body | object-store `etag` (of decoded body, not RFC822) | same | partial | of lossy form only |

## Answers to research-agenda Q1 (transport fidelity)

- **Gmail API format?** `[VERIFIED]` history resource + `messages.get` returning the parsed payload tree; `_extract_text_body` consumes `part.body.data`. **Not** `format=raw`, **not** `format=full` RFC822 bytes persisted.
- **content_bytes meaning?** `[VERIFIED]` decoded best-text body (plain>html), UTF-8; never the signed MIME.
- **Full RFC822 / original DKIM ever available?** `[VERIFIED]` IMAP momentarily has full bytes (`BODY.PEEK[]`) but discards them; Gmail never. **No path preserves them today.**
- **History-token expiry?** `[VERIFIED]` `_HistoryExpired` (400/404 on `history.list`) → bounded full resync from `users/me/profile` historyId, then incremental; first-sync baseline-reject degrades to `TransportConnectError` (no cursor advance).

## Done (0.2): evidence-level matrix exists; verified vs inferred marked. ✅

## Implications (for 0.6)
- NS-ADR-002 (preserve raw RFC822 when available): **Phase 1 needs an optional evidence-capture seam.** Gmail would need an added `format=raw` fetch (or `messages.get?format=raw`); IMAP would need to persist the `raw` it already has. Both are **extensions**, behind a flag, after audit — not present today.
- Any auth axis must be `UNKNOWN`/`PROVIDER_REPORTED` only; `INDEPENDENT` is unreachable without new transport evidence.
