# nerve-personal-relay — Guided Install (macOS host)

Durable relay daemon (spec §46/§47/§48 + `personal-ops-recovery` §5):
tails `~/Library/Messages/chat.db` **read-only** and forwards normalized
messages to NerveCenter. The guided flow below takes a fresh Mac from zero
to a paired, running, verifiable relay:

```
1  build      2  init-config   3  Full Disk Access   4  pair
5  install    6  import          7  health
```

Zero external dependencies — pure Swift + the system SQLite C library.

## 0. What the subcommands do

| Subcommand | What it does |
| --- | --- |
| `init-config [path]` | Write the config template (default `~/.nervcenter/relay.conf`) |
| `status` | Open store + outbox, report schema/cursor/backlog (FDA required) |
| `run` / `run --once` | Capture + flush loop (Messages first, then enabled D5' channels) |
| `install` | Render + load the `com.nervcenter.relay` LaunchAgent (idempotent) |
| `uninstall` | bootout + remove the plist (idempotent) |
| `pair` | `POST /api/relay/pair` → store the one-time token → `POST /api/relay/register` |
| `health` | Local diagnostics (binary/agent/FDA) + server per-device status |
| `import --consent` | Initial history import (refused without the explicit flag) |

Exit codes: `0` ok · `2` usage · `3` config · `4` refused (FDA/schema) ·
`5` transport (backend unreachable — outbox keeps the data) ·
`6` pairing/register/import failure.

## 1. Build

```bash
cd apple
swift build                 # artifact: .build/debug/NervePersonalRelay
./build_and_test.sh         # or: build + sign + the full guarded test suite
```

`build_and_test.sh` signs the release binary **before** the test pass
(spec §5 "signed release binary"): identity comes from
`NERVE_RELAY_SIGN_IDENTITY` (Developer ID / named identity), defaulting to
an **ad-hoc signature** (`-`). Signing failures are loud — it never skips
silently.

## 2. Configure (server URL + token for the engine loop)

```bash
.build/debug/NervePersonalRelay init-config
# → writes ~/.nervcenter/relay.conf (0600); then edit:
```

```ini
base_url = http://127.0.0.1:8000        # or https://…
token = PASTE_YOUR_ACCESS_TOKEN

# optional overrides (env vars win over these)
store_path = ~/Library/Messages/chat.db
outbox_path = ~/.nervcenter/relay/outbox.db
batch_size = 200
interval_seconds = 5
```

Two tokens live on this machine — do not mix them up:

- **Engine token** (`token = …` in the config, or `NERVE_RELAY_TOKEN`):
  the NerveCenter access token used by the incremental `run` loop when it
  flushes to `POST /api/messages/ingest`.
- **Pairing token** (from step 4): returned **exactly once** by
  `POST /api/relay/pair`, stored in the secure credential store
  (keychain `com.nervcenter.relay` / `relay-token`, 0600
  `~/Library/Application Support/NerveCenterRelay/credentials.json`
  fallback). It authenticates `pair`/`register`/`import`. It is **never**
  written to the config file and never printed again — `health` reports
  only `token: present/missing`.

## 3. Grant Full Disk Access (required)

`~/Library/Messages/chat.db` is protected by macOS TCC. Until the *binary
path* has FDA, `status`/`run`/`import` fail closed
(`permission denied` / `schema guard refused`) — by design: the reader
opens the DB `SQLITE_OPEN_READONLY | NOMUTEX | FAILIFREFUSED` and refuses
to run on a stale copy.

One-time grant (add the *binary*; re-grant after a `swift build` only if
the path or signature changes):

```
System Settings → Privacy & Security → Full Disk Access → +
  → pick apple/.build/debug/NervePersonalRelay
```

> The LaunchAgent runs under your user GUI session, so the FDA grant on
> the binary is inherited — no sudo, no root.

## 4. Pair

```bash
NERVE_RELAY_BASE_URL=https://nervcenter.example .build/debug/NervePersonalRelay pair
```

- `POST /api/relay/pair` → the server mints `device_id` + a **one-time
  token** (printed ONCE on stdout — it is stored in the keychain and never
  echoed again).
- `POST /api/relay/register` → `auth_status=authenticated`.
- The identity (`device_id`, `connection_id`, base URL) is persisted to
  `~/Library/Application Support/NerveCenterRelay/identity.json` (0600) so
  `health`/`import` can find the device.

**The freeze point:** a successful pair does *not* make the relay
operational. `operational_status` stays `never_synced` until the first
message is synced through `POST /api/relay/messages` (spec §3: a login
probe with no ingested data must never be labeled healthy). In practice:
run the guided `import` (step 5) or send a new message through the manual
relay batch — then `health` shows `healthy`.

## 5. Install as a LaunchAgent + import history

```bash
.build/debug/NervePersonalRelay install
# writes ~/Library/LaunchAgents/com.nervcenter.relay.plist
# (rendered from Distribution/relay_launchagent.plist.template — a built-in
#  fallback keeps the same keys if that file is missing)
# launches via: launchctl bootstrap gui/$UID  … (idempotent re-install)
```

The agent runs `nerve-personal-relay run` with `KeepAlive` + `RunAtLoad`,
logs to `~/Library/Logs/nervcenter/relay.{out,err}.log`, throttled
restarts (30 s), user-account scoped — no remote shell (§47).

Then import the historical window (explicit consent is mandatory — the
server 422s with stable code `consent_required` without it):

```bash
.build/debug/NervePersonalRelay import --consent            # full history
.build/debug/NervePersonalRelay import --consent --window-hours 720
```

The import tails `chat.db` in batches and posts stable-id
(`item_message` GUID) messages to `POST /api/relay/messages`; the server
dedups by `(device_id, message_id)`, so a re-run is a no-op. A durable
import cursor (`…/NerveCenterRelay/import-cursor.json`) makes interrupted
imports resumable. The incremental `run` loop has its own separate,
already-durable capture cursor (outbox SQLite `meta`), so the two never
collide.

## 6. Verify

```bash
.build/debug/NervePersonalRelay health
```

Local: binary OK · LaunchAgent LOADED · Full Disk Access OK ·
token present. Server: `auth_status`, `operational_status`
(`never_synced` → `healthy` after the first synced message), last seen,
pending backlog. Every local failure prints the exact fix command.

## 7. Ops notes

- **First `run`** seeds the capture cursor at the *current* `chat.db` max
  id — old history is not replayed (use the guided `import` for history).
- **Backend down** → entries stay in `outbox.db`, FIFO; the next good
  cycle drains oldest-first. Transport failures never lose data (§70).
- **4xx/5xx from ingest** → batch stays queued, `relay.err.log` shows the
  status. A 401 means the token is wrong; the relay keeps failing loudly
  by design — do not "heal" it by deleting the outbox.
- **Schema guard**: if `chat.db` moves to a version the reader does not
  know, tailing is refused and the process exits (the outbox survives).
- **Uninstall**: `nerve-personal-relay uninstall` (bootout + plist
  removal; logs and the stored token are intentionally kept — re-run
  `pair` to re-pair).

## 8. Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `openFailed: permission denied` | FDA not granted to *this* binary path — re-grant (step 3). |
| `schemaVersionMismatch` | macOS changed `chat.db` — inspect `PRAGMA user_version`, update the reader. |
| `401 Unauthorized` in `relay.err.log` | Engine token wrong/expired — rotate, restart the agent. |
| `backlog` grows, `sent` flat | Backend down/slow — wait; do not drop the outbox. |
| `operational: never_synced` after pair | Expected until the first message syncs (step 5 / freeze point). |
| `token: MISSING` in `health` | Keychain item lost (e.g. re-imaged Mac) — re-run `pair`. |
| `status` shows `cursor 0` | Never ran yet (or outbox deleted) — first `run` seeds it. |
| LaunchAgent not loaded | `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.nervcenter.relay.plist` |
