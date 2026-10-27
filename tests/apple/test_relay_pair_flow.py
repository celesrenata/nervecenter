"""Pairing-flow contract tests (workstream D — guided pair/import).

Covers the `nerve-personal-relay pair` surface that is Python-testable
(no Swift build, no network required):

  1. Credential-store API drift:
     `apple/Relay/CLI/RelayCredentialStore.swift` mirrors the canonical
     `CredentialStore` protocol in
     `apple/Shared/Sources/NerveAuth/CredentialStore.swift`. The CLI target
     links only `NerveRelayCore`, so it carries its own keychain-backed
     implementation of the same three operations, keyed by `deviceID`.
     If the canonical protocol renames or adds an operation, the mirror
     must change with it — this test fails loudly on drift.
  2. The one-time-token hard rule (spec ``personal-ops-recovery`` §5):
     the pairing token from ``POST /api/relay/pair`` is printed by
     `pair` EXACTLY ONCE, stored in the secure credential store before
     registration, and never echoed by any other subcommand (`health`
     reports only present/hidden).
  3. The store itself: keychain item constants, AfterFirstUnlock
     accessibility, and the 0600 file fallback location/permissions.

No network calls; pure source reads + regex.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RELAY_STORE = REPO_ROOT / "apple" / "Relay" / "CLI" / "RelayCredentialStore.swift"
CANON_STORE = REPO_ROOT / "apple" / "Shared" / "Sources" / "NerveAuth" / "CredentialStore.swift"
MAIN_SWIFT = REPO_ROOT / "apple" / "Relay" / "CLI" / "main.swift"


def _brace_balanced_block(text: str, header: str) -> str:
    """Extract the brace-balanced body of the first declaration whose
    declaration line starts with `header`."""
    start = text.index(header)
    brace = text.index("{", start)
    depth = 0
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace + 1 : i]
    raise AssertionError(f"unbalanced braces after {header}")


def _declared_functions(body: str) -> set[str]:
    """Function names declared as `func NAME(...)` inside a declaration body."""
    return set(re.findall(r"^\s*func\s+(\w+)\s*\(", body, flags=re.MULTILINE))


def test_both_stores_exist():
    assert CANON_STORE.exists(), f"missing canonical store: {CANON_STORE}"
    assert RELAY_STORE.exists(), f"missing relay store: {RELAY_STORE}"


def test_relay_protocol_mirrors_canonical_operation_set():
    """Same three operations (store/retrieve/delete) keyed by device —
    a rename in either file is a silent contract break."""
    canonical = _brace_balanced_block(
        CANON_STORE.read_text(encoding="utf-8"), "public protocol CredentialStore"
    )
    relay = _brace_balanced_block(
        RELAY_STORE.read_text(encoding="utf-8"), "public protocol RelayCredentialStoreAPI"
    )
    canon_ops = _declared_functions(canonical)
    relay_ops = _declared_functions(relay)
    assert canon_ops == {"storeCredential", "retrieveCredential", "deleteCredential"}, (
        f"canonical CredentialStore ops changed: {sorted(canon_ops)}"
    )
    assert relay_ops == canon_ops, (
        f"relay mirror ops {sorted(relay_ops)} != canonical {sorted(canon_ops)} — "
        "keep RelayCredentialStoreAPI in sync with NerveAuth/CredentialStore"
    )


def test_canonical_credential_fields_are_covered_by_relay_store_signature():
    """The relay store's `storeCredential(deviceID:value:expiresAt:)` must
    carry every non-optional field of the canonical `Credential` struct
    (deviceID, value, expiresAt). `scope` is intentionally dropped (the
    relay token has no scope) but must stay OPTIONAL in the canonical
    struct — re-adding a required scope would be caught here."""
    canon = CANON_STORE.read_text(encoding="utf-8")
    struct_body = _brace_balanced_block(canon, "public struct Credential")
    fields = re.findall(r"public var (\w+): (\w+\??)", struct_body)
    names = [name for name, _ in fields]
    assert "deviceID" in names and "value" in names and "expiresAt" in names, (
        f"canonical Credential fields changed: {names}"
    )
    scope_type = dict(fields).get("scope")
    assert scope_type is None or scope_type.endswith("?"), (
        "scope must stay optional in the canonical Credential struct"
    )

    relay = RELAY_STORE.read_text(encoding="utf-8")
    match = re.search(r"public func storeCredential\(([^)]*)\)\s*throws", relay)
    assert match is not None, "relay storeCredential signature not found"
    store_sig = match.group(1)
    for field in ("deviceID", "value", "expiresAt"):
        assert field in store_sig, (
            f"relay storeCredential signature lost the {field!r} parameter"
        )


def test_keychain_item_constants_and_accessibility():
    """The keychain item must stay in a stable, user-scoped location with
    AfterFirstUnlock accessibility (a LaunchAgent reads it after login)."""
    relay = RELAY_STORE.read_text(encoding="utf-8")
    assert 'public static let service = "com.nervcenter.relay"' in relay
    assert 'public static let tokenAccount = "relay-token"' in relay
    assert "kSecAttrAccessibleAfterFirstUnlock" in relay, (
        "token must be readable after first unlock (LaunchAgent session)"
    )
    assert "kSecClassGenericPassword" in relay


def test_fallback_file_is_private_and_stable():
    """Keychain failure falls back to a 0600 JSON file next to the relay
    identity data — the value must never land anywhere world-readable."""
    relay = RELAY_STORE.read_text(encoding="utf-8")
    assert "NerveCenterRelay" in relay and "credentials.json" in relay
    assert "0o600" in relay, "fallback credential file must be chmod 0600"
    assert ".atomic" in relay, "fallback write must be atomic"


def _case_block(text: str, case_name: str) -> str:
    start = text.index(f'case "{case_name}":')
    nxt = text.find('case "', start + 1)
    return text[start: nxt if nxt != -1 else len(text)]


def test_pair_prints_the_token_exactly_once_repo_wide():
    """Hard rule: the pairing token is shown ONCE, by `pair`, and never by
    any other subcommand (health reports present/hidden only)."""
    main = MAIN_SWIFT.read_text(encoding="utf-8")
    # The single token echo — the literal Swift interpolation print("  \(token)").
    echo = 'print("  \\(token)")'
    assert main.count(echo) == 1, (
        f"token is printed {main.count(echo)} times — it must be shown exactly once"
    )
    pair_block = _case_block(main, "pair")
    assert echo in pair_block, (
        "the one-time echo must live in the `pair` subcommand"
    )


def test_pair_stores_before_registering():
    """The token must reach the secure store BEFORE registration
    (registration re-uses it; a crash between the two must not lose it)."""
    main = MAIN_SWIFT.read_text(encoding="utf-8")
    pair_block = _case_block(main, "pair")
    store_at = pair_block.index("storeToken(")
    register_at = pair_block.index("relay/register")
    assert store_at < register_at, "token must be stored before /api/relay/register"


def test_pair_persists_identity_for_health_and_import():
    main = MAIN_SWIFT.read_text(encoding="utf-8")
    pair_block = _case_block(main, "pair")
    assert "saveIdentity(" in pair_block
    health_block = _case_block(main, "health")
    assert "loadIdentity()" in health_block
    assert "loadStoredToken(" in health_block


def test_health_never_echoes_the_token_value():
    """`health` may report `token: present/missing` — never the value."""
    main = MAIN_SWIFT.read_text(encoding="utf-8")
    health_block = _case_block(main, "health")
    assert "value hidden" in health_block
    assert "token: MISSING" in health_block
    # No Swift `\(token)` interpolation anywhere in the health block.
    assert "\\(token)" not in health_block, (
        "health must not interpolate the token value"
    )


def test_import_requires_explicit_consent_flag():
    """`import` without `--consent` refuses (exit .refused) — history import
    requires explicit consent (spec §5 hard rule)."""
    main = MAIN_SWIFT.read_text(encoding="utf-8")
    import_block = _case_block(main, "import")
    assert "--consent" in import_block
    assert 'guard args.contains("--consent")' in import_block
    assert "Exit.refused" in import_block
    assert 'consent": true' in import_block, "import must post consent: true"


def test_import_posts_to_relay_messages_for_operational_status():
    """The import posts to POST /api/relay/messages — the endpoint that
    advances `operational_status` via record_sync_success. (The durable
    incremental loop's flush endpoint, api/messages/ingest, does NOT
    advance it — health shows `never_synced` until import or a manual
    relay batch lands.)"""
    main = MAIN_SWIFT.read_text(encoding="utf-8")
    import_block = _case_block(main, "import")
    assert 'relayURL(base, "relay/messages")' in import_block
