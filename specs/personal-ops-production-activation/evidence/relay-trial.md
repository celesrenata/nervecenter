# Messages relay trial (P1-004, P1-201..203), 2026-11-14

Owner approved reading Messages history on this Mac ("yes, lets try"). Counts only; no message content was read by the agent or written to any log.

## Result

The relay reads a real `chat.db`, pairs with production, and stores real messages. It was **never run against a real Messages database before**: it had only ever been tested against fixtures, which encoded wrong assumptions. Everything below was found by running it.

| Step | Outcome |
|---|---|
| Pair (via `kubectl port-forward`, SSO bypassed safely) | PASS; device `f08267d9`, `authenticated` |
| Full Disk Access under launchd | PASS (grant works only for the launchd-started binary; see notes) |
| Real schema check | PASS after fixes (see defects) |
| Import, last 72 h | 1 message stored |
| Import, last 30 days | 68 stored, 1 duplicate skipped, 243 unreadable skipped (241 attachment-only, 2 empty) |
| Durable rows | `communications` +69 (35 outbound / 34 inbound), `relay_seen_messages` 69 |
| Connection status | `healthy` with a success on record |
| Idempotency | re-import stored 0; the overlapping message was deduplicated |
| `attention_items` / `commitments` | unchanged (79 / 0): the 69 messages produced none |
| Full history (about 15,800 readable messages) | **not imported** (see open items) |

Database snapshot before importing: `/volume1/Backups/nervecenter/nervecenter-db-pre-relay-import-20261114T112134Z.dump`.

## Defects found and fixed (commit `8d7ea0a4`)

1. **`health` hung forever.** `launchAgentIsLoaded()` ran `launchctl print gui/501 <label>` with domain and label as two arguments. That prints the whole GUI domain (135 KB) and the code waited for exit before reading the pipe (64 KB buffer): a deadlock. Now one target, and the pipe is drained first.
2. **Schema guard rejected every real database.** It required `message.id` and a `chat_message` table. Real databases use `message.ROWID` and `chat_message_join`. A permission denial also surfaced as "cannot introspect table message", which looked like a schema problem. The guard accepts either spelling and still fails closed on anything unknown.
3. **About a third of messages were unreadable.** On recent macOS, `text` is NULL for most messages and the text lives in the `attributedBody` blob (5,142 of 16,028 ordinary rows here: 10,884 with `text`, 5,142 blob-only, 2 empty). Added `AttributedBodyDecoder` (typedstream `NSString` payload). Against the real database it reads **4,901 of 5,142 (95%)**; the other 241 are attachment-only messages.
4. **One unreadable row aborted the whole scan**, and skipping alone would have stalled the cursor. Rows with no readable body or a NULL guid/peer/service are now skipped and counted, and `scan()` reports the highest row scanned so the engine and the importer move past them.
5. **Tests:** 15 new ones (decoder, real-shaped schema, all-unreadable batch, engine cursor). Apple suite 128 tests, 0 failures; `scripts/test-apple.sh` expected counts updated (relay 74 to 89).

## Found along the way (not relay)

**`import_jobs` leak (fixed, `8d7ea0a4`, deployed).** Every 30 s mail poll opened and closed a new import job even when no mail arrived: 53,905 rows and 164 MB after nine days, about 5,700 rows a day per mailbox. Empty polls no longer open a job; verified live (0 growth in 150 s; polling unaffected). The 53,919 existing rows are still there (purge pending owner approval).

## Notes for whoever runs this next

- **Run the relay from launchd.** macOS attributes file access to the app that launched a process, so from a terminal or an editor the relay reports `full disk access: NOT GRANTED (sqlite rc=23)` even after the grant. Test with a one-shot launchd job; the installed LaunchAgent inherits the grant.
- **Sign with a stable identity** (`Apple Development: ...`), not ad-hoc. An ad-hoc signature changes every rebuild and silently voids the Full Disk Access grant. Keep the binary at a fixed path (`~/.nervcenter/bin/NervePersonalRelay`).
- **A finished import cannot be re-run with a bigger window**: it saves `import-cursor.json` at the end. Move that file aside to re-import (server dedup makes it safe).
- **`pair` prints the one-time token on its own line.** Redirect all of its output.
- **The relay needs a route that skips SSO.** The public URL is behind oauth2-proxy and the relay uses a device bearer token. This trial used `kubectl port-forward svc/api 8000:8000`.

## Open items

1. **Message bodies are not durable.** `_relay_store_message` writes only metadata and a hash `content_ref` (`relay://<device>/msg/<sha256>`); the body is passed to the in-memory Messages service and is lost on an API restart. Mail stores bodies in the encrypted object store; the relay does not. Recommendation: store relay bodies the same way before importing full history.
2. **Permanent operation** needs an ingress path for `/api/relay/messages` that bypasses SSO and relies on the device token (`/pair` stays SSO-gated and is run once through a port-forward). That is a change to the public ingress and needs the owner's decision.
3. **Full history import** (about 15,800 readable messages): held until bodies are durable.
4. **UI check:** the API's message and attention endpoints are scoped to a logged-in user, so what appears on Today and in Messages could not be verified from here.
5. **`import_jobs` purge:** 53,919 old rows; a purge keeping the latest per connection would reclaim about 164 MB.

## Permanent operation, verified live (2026-11-14, later)

Owner: durable bodies "yes", permanent route "sure", full history "no, only new messages", old import rows purge "no" (NS-ADR-091..093).

- **Agent:** `com.nervcenter.relay` LaunchAgent, `~/.nervcenter/bin/NervePersonalRelay` signed with the owner's Apple Development identity, mode `relay-device`, target `https://nervecenter.celestium.life`; one process, `runs = 1`.
- **Public route** (verified from outside the cluster): `POST /api/relay/messages` answers 401 (no or bad token) / 422 (garbage); pair, register, import-request, health, the legacy `/api/messages/ingest`, the trailing-slash form and all other paths still return the SSO 302; a 5 MiB body returns 413; 179 of 300 rapid requests returned 429.
- **Real message, end to end:** the owner texted `test` to themselves at 14:27 UTC. The relay logged `captured=1 sent=1 failed=0 backlog=0` twice (outbound id 17312, inbound id 17313). The server gained 2 `communications` (`content://relay/...` refs) and 2 `relay_seen_messages`; the connection is `healthy`. Both bodies read back from the encrypted store as exactly `test` (4 bytes); none of the 4 relay object files on disk contains the plaintext.
- **No attention item** was created by the message (79 attention items, 0 commitments before and after).

### Not verified
- Behaviour across a Mac reboot (the plist has `RunAtLoad` and `KeepAlive`; not rebooted).
- What the owner sees in the Messages and Today screens (the API scopes reads to a logged-in user).
- A relay outage with a populated backlog replayed against production (covered by unit tests only).
- The 69 trial messages imported before bodies were durable have metadata-only refs.
