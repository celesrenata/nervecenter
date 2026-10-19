#!/bin/bash
# Canonical Apple test command — defeats the stale-test-product under-run trap.
#
# History: bare `swift test` has repeatedly under-run (once "2 of 46", once
# "0 of 46") when SPM test products were stale relative to their sources;
# the failure mode degrades silently to green. This script makes the
# under-run impossible:
#
#   (a) touch every test source file   -> test products must rebuild;
#   (b) swift build --build-tests      -> rebuild test products first;
#   (c) swift test                     -> run the full suite (one pass);
#   (d) assert per-bundle counts       -> the under-run can never be green
#                                          again (fail non-zero if any
#                                          count is missing or off).
#
# Expected per-bundle counts (XCTest, as of 2026-10-19):
#   NerveSharedTests  13
#   NerveRelayTests   31
#   NerveAppsTests     5
#   Total             49, 0 failures
#
# Success criteria are structural, not summary-line dependent: each
# 'Test Suite "<Bundle>.xctest" passed' line is paired with its
# 'Executed N tests, with F failures' summary, and any 'Test Case ...
# failed' line is a hard failure. Older toolchains that only emit a global
# summary fall back to the total 'Executed 49 tests' check.
#
# Usage:
#   bash apple/scripts/test-apple.sh
#
# `apple/build_and_test.sh` delegates to this script, so every entry point
# gets the same guarantees.

set -euo pipefail

# Run from the SPM package root (apple/), regardless of CWD.
cd "$(dirname "$0")/.."

echo "==> [test-apple] (a) touching test sources to force a fresh build..."
# -print0/xargs -r handles any path; there are always files, so -r is safe.
find Tests -name '*.swift' -print0 | xargs -0 -r touch

echo "==> [test-apple] (b) swift build --build-tests ..."
swift build --build-tests

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

echo "==> [test-apple] (c) swift test ..."
swift test 2>&1 | tee "$OUT"

echo "==> [test-apple] (d) asserting per-bundle counts..."
python3 - "$OUT" <<'PY'
import re, sys

expected = {"NerveSharedTests": 13, "NerveRelayTests": 31, "NerveAppsTests": 5}
TOTAL = sum(expected.values())
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
lines = text.splitlines()

# Any failing test case is a hard failure regardless of suite status.
failed_cases = [ln for ln in lines if re.search(r"Test Case .* failed", ln)]
if failed_cases:
    for ln in failed_cases[:5]:
        print(f"FAIL: failing test case: {ln}", file=sys.stderr)
    sys.exit(1)

# Per-bundle: 'Test Suite "<Bundle>.xctest" passed' -> next 'Executed N tests'
counts = {}
for i, ln in enumerate(lines):
    m = re.search(r"Test Suite '([^']+)\.xctest' passed", ln)
    if m:
        bundle = m.group(1)
        for j in range(i + 1, min(i + 4, len(lines))):
            em = re.search(r"Executed (\d+) tests, with (\d+) failures", lines[j])
            if em:
                counts[bundle] = (int(em.group(1)), int(em.group(2)))
                break

if counts:
    ok = True
    for name, want in expected.items():
        got = counts.get(name)
        if got is None:
            print(f"FAIL: {name}: no test summary found (under-run?)", file=sys.stderr)
            ok = False
        elif got[0] != want or got[1] != 0:
            print(f"FAIL: {name}: got {got[0]} tests / {got[1]} failures, want {want} / 0", file=sys.stderr)
            ok = False
        else:
            print(f"OK:   {name}: {got[0]} tests, 0 failures")
    total = sum(n for n, _ in counts.values())
    failures = sum(f for _, f in counts.values())
    if ok and (total != TOTAL or failures != 0):
        print(f"FAIL: total {total} tests / {failures} failures, want {TOTAL} / 0", file=sys.stderr)
        ok = False
    if not ok:
        sys.exit(1)
    print(f"PASS: {total} tests total (13/31/5), 0 failures")
else:
    # Fallback (older toolchain): single global summary line.
    ms = list(re.finditer(r"Executed (\d+) tests, with (\d+) failures", text))
    if not ms or ms[-1].group(1) != TOTAL or ms[-1].group(2) != "0":
        tail = ms[-1].group(0) if ms else "no 'Executed N tests' summary found"
        print(f"FAIL: expected exactly {TOTAL} tests / 0 failures; got {tail}", file=sys.stderr)
        sys.exit(1)
    print(f"PASS: {TOTAL} tests total (global summary), 0 failures")
PY

echo "==> [test-apple] all checks passed."
