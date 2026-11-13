# Sent mail as outgoing (NS-ADR-066) and the SHADOW thread ledger (NS-ADR-067)

Owner direction (2026-11-12): "your job is to read my email, you can read sent emails as well". G8 item 2 (thread continuity in SHADOW) was already approved. Commit `64dea142`, deployed; migration head `021_mail_thread_ledger`.

## Defect found: sent mail and drafts ingested as incoming

The Gmail history poll has no INBOX filter, despite its docstring, so every message added to the mailbox was delivered as incoming mail. A read-only label probe (Gmail `format=minimal`, label names and counts only) over all stored communications found:

| Account | INBOX | SPAM | SENT (from owner) | INBOX+SENT (self) | DRAFT | gone (404) |
|---|---|---|---|---|---|---|
| fc4e3c57 | 39 | 3 | **304** | 1 | 0 | 0 |
| 53b03939 | 267 | 22 | 2 | 0 | 1 | 19 (owner-sent, most likely deleted drafts) |

So 304 of 348 messages on one account were the owner's own sent mail. Each was stored as an incoming communication attributed to the owner and assessed by NerveShield as an inbound threat.

## Fix forward (NS-ADR-066)

- The transport reads `labelIds` from the `format=full` response. `DRAFT` messages are marked seen and never delivered. Messages labeled `SENT` but not `INBOX` are delivered with their labels. A self-addressed copy (`SENT`+`INBOX`) stays incoming.
- The worker records sent-only mail as **OUTGOING** and attributes it to the first To/Cc recipient, which is the registry's existing counterpart rule. Recipients are the To/Cc addresses. Commitment and follow-up detection already handles outgoing mail (for example, "I'll send it Friday" becomes a promise).
- Outgoing mail gets no raw fetch, no raw object and no inbound security job. The 7.4 gate does not apply to it (`applies()` is False for outgoing), so its promises are never held while waiting for a verdict that will never come. The NerveShield backfill skips outgoing rows.
- The envelope header allow-list adds `References`, `Reply-To` and `Cc`.
- Spam is still ingested and assessed. That is useful inbound threat input.

## Thread ledger (NS-ADR-067)

- `backend/mail_security/thread_ledger.py` is a runtime port of the spike 8.2 logic. It does not import the research code, which the guard test still forbids.
- `CONTINUOUS` requires a cited parent in this account's ledger **and** a sender domain among that thread's participants. Participants are every sender plus the recipients of the owner's own sent mail, so replies to the owner's messages can be judged.
- Signals:
  - UNKNOWN_PARENT (1);
  - REPLY_SUBJECT_WITHOUT_REFERENCES (1);
  - NEW_PARTICIPANT_DOMAIN (1);
  - REPLY_TO_DIVERGES (2);
  - LOOKALIKE_OF_PARTICIPANT (3).

  A score of 3 or more is BROKEN; 1–2 is SUSPECT. An unknown parent always abstains (UNKNOWN).
- Not ported: AUTH_DOWNGRADE, because the trusted-receiver boundary is empty, and PAYMENT_DETAILS_CHANGED, because there is no payee extraction.
- Table `mail_thread_ledger` (migration 021) holds per-account HMAC fingerprints of message ids and thread roots, registrable domains, and status and signal codes. Inbound rows also carry the opaque body `content_ref`. It stores no subject, body, local part or address; a test checks this.
- The key is `NERVE_MAIL_SECURITY_LEDGER_KEY` in the out-of-band Secret `nervecenter-ledger-key`, separate from the object key. It is 32 random bytes, created in-cluster and never printed. Without the key the ledger is inert. The ledger is derived data, so losing the key only resets it, and no backup was taken.
- The ledger is fed by the mail worker for every message in both directions. It is best-effort: a failure is logged and never affects ingest.
- **SHADOW only.** Review queue `mail-review-queue@2` adds THREAD_BROKEN (+3, MEDIUM and visible on its own) and THREAD_SUSPECT (+1, LOW on its own, so it goes to the digest unless the message is protected). The queue loader also selects flagged assessments whose disposition is not normally queued. **Dispositions, gate decisions and attention items do not change.** The UI has plain-language labels for both reasons.

## Seeding

Owner tool `backend/owner_tools/seed_thread_ledger.py` lists INBOX and SENT messages (no drafts) from the last 180 days, up to 2000 per account. It fetches **headers only** (`format=metadata`: Message-ID, From, To, Cc, Reply-To, References, In-Reply-To) and records them oldest-first without assessing, so seeded rows have status NULL and nothing is queued. It is idempotent and backs off on Gmail rate limits; the first run hit them on one account, and the rerun finished with 0 errors.

| Account | Sent rows | Inbound rows |
|---|---|---|
| 53b03939 | 7 | 1550 |
| fc4e3c57 | 315 | 1022 |

## Verification

- Tests:
  - `test_mail_outgoing_classification.py` (12): label rule, transport classification, draft skip, no raw fetch for sent mail, header allow-list, recipient parsing, outgoing attribution, no job, self-copy stays incoming, backfill excludes outgoing.
  - `test_mail_security_thread_ledger.py` (8): key parsing, salting, observe, all statuses, per-account isolation and replay, privacy, and a lookalike reply reaching the review queue as THREAD_BROKEN with the disposition unchanged; a genuine reply is CONTINUOUS and not queued; with no key nothing is stored.
  - The 7.4 test now asserts outgoing commitments are never held.
- Full suite: 8564 passed, plus the 1 known unrelated failure. e2e: 104 passed.
- Live:
  - migration at head;
  - api 2/2 with the key present;
  - the first new inbound message after the deploy was recorded in the ledger (`IN`, `NOT_A_REPLY`) and raw-captured;
  - 79 `poll_ok` lines and no ledger or raw-capture failures.
- **Not yet observed live:** a newly sent message landing as OUTGOING, which needs the owner to send mail, and a live SUSPECT/BROKEN result.

## Open

- **Historical data is not corrected.** The 305 already-stored owner-sent or draft messages are still incoming and attributed to the owner, and they still have inbound NerveShield assessments. Fixing them changes stored communications, people links and derived attention, so it needs an owner decision.
- Expected false positives: a legitimate new external participant is SUSPECT by design (spike 8.2). That is why SUSPECT goes to the digest.

## Historical correction (NS-ADR-068, owner: "all in that order", 2026-11-13)

**Snapshot first.** A one-off in-cluster pod ran a full `pg_dump -Fc` to `/volume1/Backups/nervecenter/nervecenter-db-pre-sent-fix-20261113T014406Z.dump`: 20 MB, data for 62 tables, mode 0600, `umask 077`. The data never left the cluster or the NAS. Restore with `pg_restore`.

**Tool.** `backend/owner_tools/fix_sent_mail_history.py`. It defaults to a dry run; `--apply` writes everything in ONE transaction. For each stored Gmail communication attributed to one of the owner's accounts, it asks Gmail for labels and From/To/Cc only (`format=metadata`), then:
1. **FIX** (SENT without INBOX): sets the direction to outgoing and re-attributes the message to the first To/Cc recipient, creating the person, identity and conversation if missing. It uses the same deterministic person id as the live worker.
2. **DROP** (DRAFT, or a 404 in Gmail): deletes the row. The encrypted body object is kept.
3. **CLEAN** both sets: deletes their NerveShield jobs, assessments, evidence, findings, feedback, holds and ledger rows, plus the attention items and follow-ups whose refs name them.

**Dry run** (it matches the earlier label probe exactly):

| Account | DROP | FIX | KEEP |
|---|---|---|---|
| 53b03939 | 20 | 2 | 0 |
| fc4e3c57 | 0 | 304 | 1 (self-addressed copy) |

**First apply failed and rolled back.** Postgres rejected the conversations insert with an FK violation: with no ORM relationships declared, the unit of work flushed the conversation before the new person. SQLite in the test doesn't enforce foreign keys. A check confirmed nothing had changed (664 incoming, 664 jobs). The fix flushes in FK order. The test now sets `PRAGMA foreign_keys=ON` and was shown to fail on the old ordering.

**Applied:**

| Account | Fixed (re-attributed) | People created | Dropped | Jobs / assessments / evidence removed | Attention removed |
|---|---|---|---|---|---|
| fc4e3c57 | 304 | 11 | 0 | 304 each | 1 |
| 53b03939 | 2 | 0 | 20 | 22 each | 44 |

Feedback, findings, holds and ledger rows removed: 0 each. The api was restarted so its in-memory view reloads.

**After:**
- 306 outgoing and 338 incoming communications.
- Exactly 338 jobs (one per incoming message), 0 jobs for outgoing mail, 0 orphan jobs.
- 1 communication is still attributed to the owner (the self-addressed copy).
- Backfill scans run without re-creating anything.
- api 2/2.

**Not done (left as is):**
- Outgoing commitments and follow-ups were not re-derived for the 306 corrected messages. Detection runs at ingest, and these rows were not re-ingested.
- The 20 dropped rows' encrypted body objects remain on the NAS.
