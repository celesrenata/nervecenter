#!/bin/bash
# Build + test NerveCenter.
#
# Delegates to scripts/test-apple.sh, the canonical Apple test command that
# defeats the stale-test-product under-run trap (see gaps3.md Phase A /
# gaps4.md Phase 1, D3). A bare `swift test` here would NOT guarantee the
# full 46-test suite runs, so this entry point routes through the guarded
# command instead.

set -euo pipefail

cd "$(dirname "$0")"
exec bash scripts/test-apple.sh
