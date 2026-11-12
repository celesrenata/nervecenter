# Full-email capture (G8 item 1) and ClamAV (G8 item 5)

Owner decisions at G8: "1. full email capture" approved; "5. clamav?" folded into item 1 because under BODY_ONLY there were no attachments to scan. Records: NS-ADR-064 (capture) and NS-ADR-065 (clamd). Commit `b512ffc3`, deployed 2026-11-12 15:26 UTC.

## Full capture (NS-ADR-064)

**What changed.**
- `GmailClient(capture_raw=True)` makes a second `messages.get?format=raw` call per new message, decodes the base64url `raw` field and attaches it as `MailItem.raw_bytes`. With the flag off the request sequence and the items are the same as before. Any failure (non-200, timeout, bad JSON, bad base64, empty, over the cap) returns `None`, logs a safe code and leaves the message on the body-only path. It never fails the poll or touches the cursor.
- The mail worker stores the raw bytes as a **separate** object `raw/<conn>/<sha256(conn:mail_id)[:24]>` (content type `message/rfc822`) through the same encrypted object store as bodies (NS-ADR-062). It then schedules the job with `meta.raw_ref = content://raw/...` and `acquisition_mode = GMAIL_RAW`. `meta.content_ref` is still the decoded body ref, so the 7.1 safety-card join (`source_key_for_content_ref`) and the 7.4 gate join (`latest_verdict`) are unchanged. A failed raw write leaves the message body-only.
- `acquisition.py`: the global `RAW_CAPTURE_SEAM_ENABLED=False` is replaced by an allow-list `RAW_SEAM_MODES = {GMAIL_RAW}`. A raw mode survives on write and on read only if it is allow-listed **and** the meta carries a well-formed `content://raw/` ref. IMAP_RFC822, TAKEOUT_MBOX, EML and GMAIL_FULL stay demoted to LEGACY_BODY_ONLY, even with a raw ref.
- Security worker: scans `scan_ref_for(meta)`, which is the raw ref when the trusted mode is raw and the body ref otherwise. A missing raw object fails closed (INCOMPLETE, `CONTENT_UNRESOLVED`); it never falls back to the body. For a raw capture the manifest records `raw_content_ref` = raw object and `normalized_content_ref` = body.
- Flag `NERVE_MAIL_SECURITY_FULL_CAPTURE`. It only takes effect together with `NERVE_MAIL_SECURITY_ENABLED`. Size cap `RAW_CAPTURE_MAX_BYTES` = 32 MiB, checked before decoding and again after. Only `gmail_history` workers raw-capture.
- Scope: **new mail only**. The 641 stored messages stay BODY_ONLY. A historical raw backfill is possible later and would need its own decision.

**What did not change.** SHADOW only. No model sees raw bytes (item 3 is still deferred). The trusted-receiver boundary is still empty, so every `Authentication-Results` header stays UNTRUSTED even with raw headers. Trusting Gmail's own `mx.google.com` results is a separate decision (see Open items).

**Tests.** `tests/backend/test_mail_security_full_capture.py` has 25 tests:
- flag matrix;
- supervisor wiring;
- transport off/on, the resync path, every raw-failure mode, and the size cap;
- end to end:
  - flag off gives the same meta as an item with no raw bytes;
  - flag on gives the raw object, GMAIL_RAW and ORIGINAL_RFC822, with both joins resolving through the body ref and not through the raw ref;
  - a non-Gmail cursor never captures;
  - a raw write failure, an oversized raw message and a missing raw object all behave as above;
  - raw objects are ciphertext at rest under `EncryptedObjectStore`.

`test_mail_security_fidelity.py` was updated: only GMAIL_RAW with a raw ref reaches ORIGINAL_RFC822, and IMAP_RFC822 or GMAIL_FULL with the same ref stay BODY_ONLY. Full suite: 8544 passed, plus the 1 known unrelated failure (`test_routines.py::TestWellnessSleepSeam::test_returns_iso_records_with_source`).

## ClamAV (NS-ADR-065)

**Shape.** The client (`clamav.py`) only accepts a UNIX socket or loopback, so attachment bytes cannot leave the pod. The client was **not** relaxed. Instead:
- **clamd**: a native sidecar (init container with `restartPolicy: Always`) in the api pod. It listens on `/run/clamav/clamd.sock` in a pod-private in-memory emptyDir and has no TCP listener. It runs as uid 10001, non-root, with a read-only root filesystem and all capabilities dropped. It reads signatures read-only and waits for a database instead of crash-looping, so an empty cache cannot block the api; the scanner reports UNAVAILABLE until clamd answers. Limits: `StreamMaxLength 20M`, above the client's 16 MiB cap. `SelfCheck 600` reloads updated signatures.
- **freshclam**: a separate `clamav-freshclam` Deployment. It is the only ClamAV part with egress, never sees attachment bytes or api secrets, and writes to its own NFS volume `/volume1/Kubernetes/NerveCenter/clamav`. Its NetworkPolicy allows no ingress, and egress only to DNS and TCP 80/443 on non-private addresses. Checks run every 2 h, well inside the client's 3-day signature-age limit.
- Image: `clamav/clamav:1.5.4_base`, pinned by digest `sha256:64ca9166…85ce5`.
- `NERVE_CLAMD_SOCKET=/run/clamav/clamd.sock` in the ConfigMap.

**Live verification (2026-11-12).**
- freshclam downloaded daily 28181, main 63 and bytecode 339, and each passed its database test.
- NetworkPolicy is enforced: from the freshclam pod, `api.nervecenter.svc:8000` is blocked and `database.clamav.net` is reachable.
- The clamd sidecar loaded the database (about 946 MiB resident). The socket is `srw-rw----` owned by uid 10001.
- The **first test against a real clamd** used the production `scanner_from_env()` inside the api container: `ping` True; EICAR → `DETECTED`; a plain-text payload → `CLEAN_ENGINE_VERDICT`. The engine version was parsed as `1.5.4-sig28181-20261112`, so VERSION parsing and the signature-freshness check work against real clamd. This closes the "NOT VERIFIED AGAINST REAL CLAMD" item in `clamav.py`.

**Tradeoff.** clamd shares the api pod, so a clamd crash marks the api pod NotReady until it restarts (about a minute to reload signatures). The alternative was a separate clamd pod, which would have meant relaxing the local-only client rule. The memory limit is 4 GiB, which allows for two copies of the signatures during a reload.

## Live full-capture verification

First new Gmail message after the roll, at 15:42 UTC. Every check ran in-pod and printed only counts, booleans, header names and codes, never content.
- **Jobs:** 642 total. 641 are LEGACY_BODY_ONLY (historical). 1 is `GMAIL_RAW` with `raw_ref` and state DONE.
- **Objects:**
  - Raw object under `raw/`: 50,086 bytes, multipart with 3 parts and 0 attachments.
  - Body object under `mail/`: 41,411 bytes. It is unchanged, because the body path did not change.
  - The raw object on the NAS differs from the plaintext and contains no plaintext header names. It is ciphertext from the `EncryptedObjectStore`.
- **Headers now present** (previously absent under BODY_ONLY): Received, Authentication-Results, ARC-Authentication-Results, DKIM-Signature, Return-Path, Message-ID, Received-SPF, Delivered-To.
- **Assessment:**
  - Fidelity `ORIGINAL_RFC822`, state ASSESSED, disposition LOW_PRIORITY.
  - `missing` and `checks_not_run` are both empty.
  - The manifest is `GMAIL_RAW`/`ORIGINAL_RFC822`, with `raw_content_ref` pointing at the raw object, `normalized_content_ref` pointing at the body, and `missing_parts` empty.
- **Auth axes:** spf, dkim and dmarc are `UNTRUSTED_HEADER`; arc is `UNKNOWN`. This is expected while the trusted-receiver boundary is empty. Headers are evaluated but never trusted.
- **Joins:**
  - 7.1 `source_key_for_content_ref(body ref)` returns the job's source key.
  - 7.4 `latest_verdict(body ref)` returns LOW_PRIORITY.
- **Logs:** no `gmail.raw_fetch_*`, `ingest.raw_capture_failed` or scanner-config warnings since the roll. Every `mail_ingest` line is `poll_ok`.
- **Not yet observed:** a live message with an attachment, so no real attachment has gone through clamd yet. The EICAR and clean round-trips above prove the scanner path itself.

## Open items

- Trusted-receiver boundary for Gmail (`mx.google.com`) is still empty, so authentication axes remain UNKNOWN or UNTRUSTED. It needs an owner decision and its own tests, because Gmail's own Authentication-Results header is only trustworthy if it is the topmost one.
- Historical raw backfill for the 641 BODY_ONLY messages is not done.
- 8.2 thread continuity in SHADOW (G8 item 2) is next. It needs headers, which raw capture now provides for new mail.
