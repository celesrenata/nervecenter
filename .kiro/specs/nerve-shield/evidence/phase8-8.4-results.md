# Spike 8.4 — Cross-channel trust adapter (research; contract/adapter only)

Date: 2026-11-12. Status: contract plus an in-process adapter in `backend/mail_security/research/cross_channel.py`. It is not wired and adds no connector, and no SMS/iMessage/RCS/voicemail data was read.

## Question

Can email security evidence be correlated with other channels without cross-contaminating permissions, and without assuming any new external connector?

## Contract

**Input: only what NerveCenter already has.** The adapter takes the existing normalized `backend.communications.models.Communication` records through a caller-supplied `ChannelSource.list_metadata(account_scope, channel, since)`. It imports no connector, ingest, device, Apple, HTTP, socket or subprocess module, and a test enforces this.

**Authorization: both of these, per channel, per account, per purpose.**
1. An unexpired `ChannelGrant(account_scope, channel, purpose="SECURITY_CORRELATION", granted_by, expires_at)`.
2. The **existing PDP** allowing `resource.read:security.correlation` on `nc://personal/nervecenter/<channel>/correlation/security`, which is a valid `nc-grammar v1` URI. With no policy, the PDP default-denies.

Only human principals are accepted. In practice this means:
- An email grant never implies SMS.
- A grant on account A never reads account B.
- An ungranted channel is **never fetched** and is reported only as `NOT_EVALUATED`, with no count and no signal.

**Metadata only.** The adapter reads the person ref, channel, timestamp and a per-adapter HMAC of the sender handle. It never resolves `content_ref`. Handles and refs don't appear in the report (test).

**Output: per-channel views and no universal score.** `CrossChannelReport` has no score or trust field (test). The two signals can only *add* review reasons:
- `NEW_HANDLE_FOR_KNOWN_PERSON`: a known person appears from a handle not seen before on that channel.
- `CROSS_CHANNEL_PRESSURE`: within 24 h after a *flagged* email, the same person reaches out on another channel from a new handle (the classic "I also texted you").

## Tests (`tests/backend/test_mail_security_research_8_4.py`: 13 passed)

- No grants: zero source calls, and every channel is `NOT_EVALUATED` with no signals.
- An SMS grant on account A: exactly one fetch, `(A, sms)`. iMessage stays `NOT_EVALUATED`, account B is untouched, and both signals fire on the new handle 2 h after the flagged email.
- A grant on A with a request for B: zero fetches.
- An expired grant, or a valid grant with no ALLOW policy (PDP default deny): zero fetches.
- Five machine principal kinds: zero fetches.
- No flagged email: only `NEW_HANDLE_FOR_KNOWN_PERSON`. The report has no score or trust field, and the signal set is closed.
- The report contains no phone numbers, iCloud handles or `enc://` refs.
- Every channel resource validates against the frozen URI grammar. This test was added after a 4-segment URI made the PDP fail closed with `malformed_request`, which was the correct behavior but caught my bug.
- The import guard holds (no connector or network modules).

## Findings

1. **Permission isolation is easy to make structural.** If the adapter's only data path is a source call made *after* both the grant and the PDP pass, then "no access without explicit authorization" can be checked with a spy source (zero calls) instead of being a convention.
2. **Useful cross-channel signals don't need content.** New-handle and pressure-timing signals come from metadata the platform already stores. The value depends on person resolution being correct across channels. A wrong `person_id` link would create false pressure signals, and that hasn't been measured.
3. **There's no universal sender trust score by design.** Email authentication says nothing about an SMS handle; combining them would launder trust across channels.

## Limits and prerequisites for a product

- **No grant store or UI.** Grants are in-memory objects. A product needs a durable, auditable grant record with owner-only creation and revocation.
- **No real data.** Behavior on live iMessage/SMS volumes (shared family numbers, short codes, carrier rewrites) is unknown.
- **Calendar and account-activity channels** need their own evidence models; they aren't `Communication` records.

## Proposed follow-on (needs a separate G8 approval)

A durable `channel_grants` table with owner-only grant and revoke, PDP policies per channel, and then SHADOW-only review reasons in the 7.3 queue.
