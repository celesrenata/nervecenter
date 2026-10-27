#!/bin/bash
# Build + test NerveCenter.
#
# Delegates to scripts/test-apple.sh, the canonical Apple test command that
# defeats the stale-test-product under-run trap (see gaps3.md Phase A /
# gaps4.md Phase 1, D3). A bare `swift test` here would NOT guarantee the
# full 46-test suite runs, so this entry point routes through the guarded
# command instead.
#
# After the test pass, the Personal Relay CLI binary is code-signed so the
# installed LaunchAgent runs a signed release binary (spec
# personal-ops-recovery §5 "signed release binary"). Identity selection:
#   NERVE_RELAY_SIGN_IDENTITY   Developer ID / explicit identity name
#   (unset)                     ad-hoc signature ("-") — the default
# Fails loudly when codesign is unavailable or the signature does not
# verify; never silently skips signing.

set -euo pipefail

cd "$(dirname "$0")"

# -- sign the release CLI binary BEFORE the test pass so the binary on disk
# is the signed artifact users install via `nerve-personal-relay install`.
RELAY_BIN=".build/debug/NervePersonalRelay"
if command -v swift >/dev/null 2>&1; then
    echo "==> [build_and_test] building the Personal Relay binary..."
    swift build --product NervePersonalRelay
fi

if [ -x "$RELAY_BIN" ]; then
    if ! command -v codesign >/dev/null 2>&1; then
        echo "ERROR: codesign is not available on this host; cannot sign the release binary." >&2
        exit 1
    fi
    SIGN_IDENTITY="${NERVE_RELAY_SIGN_IDENTITY:--}"
    echo "==> [build_and_test] codesigning $RELAY_BIN (identity: ${NERVE_RELAY_SIGN_IDENTITY:-<ad-hoc>})"
    codesign --force --sign "$SIGN_IDENTITY" "$RELAY_BIN"
    codesign --verify --strict "$RELAY_BIN"
    echo "==> [build_and_test] signature verified."
else
    echo "WARNING: $RELAY_BIN not found or not executable — skipping signing (build the package first)." >&2
fi

exec bash scripts/test-apple.sh
