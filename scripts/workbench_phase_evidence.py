#!/usr/bin/env python3
"""Run a Workbench phase's automated tests and write gate evidence.

Usage: scripts/workbench_phase_evidence.py PHASE [--with-web] [--out-dir DIR]

Writes docs/workbench/evidence-phase-N.md with test count, pass count, fail
count and run date. Exits non-zero (and marks the phase INCOMPLETE) when any
test fails, errors, or no tests ran. Requirements 11.4, 11.5.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND_TESTS = "tests/backend/workbench"
WEB_SPEC = "tests/e2e/workbench.spec.ts"
PHASES = (1, 2, 3)


@dataclass(frozen=True)
class Counts:
    total: int = 0
    passed: int = 0
    failed: int = 0  # failures + errors
    skipped: int = 0

    def __add__(self, other: "Counts") -> "Counts":
        return Counts(
            self.total + other.total,
            self.passed + other.passed,
            self.failed + other.failed,
            self.skipped + other.skipped,
        )


def parse_junit(path: Path) -> Counts:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = failed = skipped = 0
    for s in suites:
        total += int(s.get("tests", 0))
        failed += int(s.get("failures", 0)) + int(s.get("errors", 0))
        skipped += int(s.get("skipped", 0))
    return Counts(total, total - failed - skipped, failed, skipped)


def run_backend() -> tuple[Counts, int]:
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "junit.xml"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", BACKEND_TESTS, "-q", f"--junitxml={xml}"],
            cwd=REPO,
        )
        if not xml.exists():
            return Counts(failed=1, total=1), proc.returncode or 1
        counts = parse_junit(xml)
        # Collection errors etc. must never read as a pass.
        rc = proc.returncode
        if rc != 0 and counts.failed == 0:
            counts = counts + Counts(total=1, failed=1)
        return counts, rc


def run_web() -> tuple[Counts, int]:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "pw.xml"
        proc = subprocess.run(
            ["npx", "playwright", "test", WEB_SPEC, "--reporter=junit"],
            cwd=REPO / "web",
            env={**__import__("os").environ, "PLAYWRIGHT_JUNIT_OUTPUT_NAME": str(out)},
        )
        if not out.exists():
            return Counts(failed=1, total=1), proc.returncode or 1
        counts = parse_junit(out)
        if proc.returncode != 0 and counts.failed == 0:
            counts = counts + Counts(total=1, failed=1)
        return counts, proc.returncode


def render(phase: int, counts: Counts, parts: dict[str, Counts], when: str) -> str:
    complete = counts.failed == 0 and counts.total > 0
    status = "COMPLETE (pending Owner review)" if complete else "INCOMPLETE"
    lines = [
        f"# Workbench Phase {phase} evidence",
        "",
        f"- Run date: {when}",
        f"- Test count: {counts.total}",
        f"- Pass count: {counts.passed}",
        f"- Fail count: {counts.failed}",
        f"- Skipped: {counts.skipped}",
        f"- Phase status: **{status}**",
        "",
        "| Suite | Tests | Passed | Failed |",
        "|---|---|---|---|",
    ]
    for name, c in parts.items():
        lines.append(f"| {name} | {c.total} | {c.passed} | {c.failed} |")
    lines += [
        "",
        "The Owner raises `WORKBENCH_PHASE` only after reviewing this file. "
        "A non-zero fail count keeps the phase incomplete.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("phase", type=int, choices=PHASES)
    ap.add_argument("--with-web", action="store_true",
                    help="also run the Playwright workbench spec (needs live stack)")
    ap.add_argument("--out-dir", type=Path, default=REPO / "docs" / "workbench")
    args = ap.parse_args(argv)

    parts: dict[str, Counts] = {}
    rc = 0
    c, r = run_backend()
    parts["backend (tests/backend/workbench)"] = c
    rc |= r
    if args.with_web:
        c, r = run_web()
        parts["web (Playwright workbench spec)"] = c
        rc |= r

    total = Counts()
    for c in parts.values():
        total = total + c
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"evidence-phase-{args.phase}.md"
    out.write_text(render(args.phase, total, parts, when), encoding="utf-8")
    print(f"wrote {out}")

    if rc != 0 or total.failed > 0 or total.total == 0:
        print(f"Phase {args.phase} INCOMPLETE", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
