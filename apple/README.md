# NerveCenter — Apple Shared Package

Shared Swift package (`NerveShared`) consumed by the iPhone, Watch, and Widgets
applications (spec §52–55). Builds on the M5 Max macOS development system with
Swift Package Manager — **zero external dependencies** (spec §54).

## Layout

```text
apple/
├── Package.swift
├── build_and_test.sh        # sign the relay binary, then the guarded test suite
├── CONTRACT_NOTES.md        # wire-contract notes per module
├── Shared/
│   └── Sources/
│       ├── NerveModels/     # v0 record contracts (Event, Task, Goal, Claim,
│       │                    #   SynchronizedState, AnyCodable, NerveJSON, …)
│       ├── NerveAPI/        # APIClient, APITransport, MockTransport, errors
│       ├── NerveAuth/       # CredentialStore, BiometricAuthenticator, Approval
│       ├── NervePersistence/ # LocalCache (actor) + CachedState snapshot
│       ├── NerveRealtime/   # RealtimeClient (WebSocket/SSE)
│       └── NerveDesign/     # design tokens
├── Relay/                   # macOS Personal Relay (spec §46–48) — see Relay/INSTALL.md
│   ├── CLI/                 # nerve-personal-relay (init-config/status/run/install/
│   │                        #   uninstall/pair/health/import) + keychain credential store
│   ├── Core/                # NerveRelayCore: store reader, outbox, engines, config
│   └── Distribution/        # relay_launchagent.plist.template (+ manifest tooling)
└── Tests/
    └── NerveSharedTests/
```

## Targets

| Target | Path | Notes |
| --- | --- | --- |
| `NerveModels` | `Shared/Sources/NerveModels` | Strict concurrency enabled |
| `NerveAPI` | `Shared/Sources/NerveAPI` | depends on NerveModels |
| `NerveAuth` | `Shared/Sources/NerveAuth` | depends on NerveModels |
| `NervePersistence` | `Shared/Sources/NervePersistence` | actor-isolated cache |
| `NerveRealtime` | `Shared/Sources/NerveRealtime` | event streaming |
| `NerveDesign` | `Shared/Sources/NerveDesign` | design tokens |
| `NerveRelayCore` | `Relay/Core` | platform-free relay engine (store/outbox/normalizer/config) |
| `NervePersonalRelay` | `Relay/CLI` | macOS relay CLI binary + keychain credential store |
| `NerveSharedTests` | `Tests` | XCTest suite |

## Personal Relay (macOS)

The durable Mac Messages relay — guided setup (build → config → Full Disk
Access → pair → LaunchAgent → consented import → health) is documented in
[Relay/INSTALL.md](Relay/INSTALL.md). `build_and_test.sh` signs the release
binary first (ad-hoc by default, `NERVE_RELAY_SIGN_IDENTITY` to override),
then runs the guarded Apple test suite. Python contract tests for the
LaunchAgent template and the pairing flow live in
[`tests/apple/`](../tests/apple/test_relay_launchagent.py) (run with
`uv run --with pytest python -m pytest tests/apple/`).

## Testing

```bash
cd apple
./build_and_test.sh     # or: swift build && swift test
```

The `NerveSharedTests` target runs 13 test cases across 5 test classes
(`swift test`: *13 tests, 0 failures*):

| Test class | Cases | Covers |
| --- | --- | --- |
| `EventTests` | 3 | Event JSON roundtrip; `verifyIntegrity()` / `computeIntegrityHash()` / `stampIntegrity()` (spec §10 sha256 `hash`); deterministic canonical digest |
| `TaskStateTests` | 2 | all 8 `TaskState` cases and their wire values (spec §22) |
| `SynchronizedStateTests` | 2 | Phone ↔ Watch snapshot payload + JSON roundtrip (spec §69) |
| `MockTransportTests` | 3 | `APIClient` + `MockTransport`: recorded call (URL/method/headers), fixed JSON response decode, §29 correlation headers |
| `CacheTests` | 3 | `LocalCache` actor: snapshot replace + disk reload, `update` mutation, atomic write integrity (spec §73) |

Notes:

- Event integrity: the digest is computed over the canonical wire
  serialization (snake_case keys, ISO-8601 dates, sorted keys) of every field
  **except `hash`**, using `CryptoKit.SHA256` (system framework — no external
  dependency). Unsigned events (`hash == nil`) verify as `true`.
- Roundtrip tests for timestamped records use integer-second timestamps: the
  shared `.iso8601` wire strategy does not carry sub-second precision.
- `LocalCache` is an actor; tests access it with `async`/`await` and verify
  persistence by re-instantiating the cache over the same directory.
