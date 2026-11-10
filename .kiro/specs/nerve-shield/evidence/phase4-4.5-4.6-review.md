# Review: NerveShield Phase 4 tasks 4.5 and 4.6

Verdict: APPROVED (no blockers).

Evidence read: `backend/mail_security/escalation.py` (full), the diff of `decisions.md` (NS-ADR-031 present, PROPOSED), the injection test structure, corpus fixture, static not-wired test, and the coder's results file. Suites were not re-run; recorded results: 728 injection tests passed, 87 escalation tests recorded, ruff clean, regression run 7524 passed with only the known unrelated `test_routines` failure.

## Task 4.5
- Pure, versioned (`mail-escalation-policy@1`), no I/O, imports only `Disposition`. Version is in `log_fields()`, which carries enums and counts only.
- Cautious floors (REVIEW/RESTRICTED/INCOMPLETE) return unchanged with no model call; `_raise_to` is rank-monotonic; no path returns RESTRICTED from a model or an allow after a triggered failure (`_fail_closed` yields REVIEW/INCOMPLETE).
- Granite unavailable/failed/denied/rate-limited/disabled, evidence over budget, call or window budget exhausted all fail closed. Qwen disabled, failed or over budget returns exactly the no-Qwen result. Invalid inputs give INCOMPLETE.
- Tests are table-driven with an exhaustive product, Qwen-absence equivalence, and a driver-loop termination check.

## Task 4.6
- Corpus covers all five placements with a per-run canary. Hostile Granite/Qwen transports (tool-call shape, schema violations, mark-safe prose, exfil, extra keys, oversized output) map to typed errors or non-contributing results. Exceptions carry only the code, with no `__cause__`. The canary check spans caplog, stdout/stderr, exceptions, log fields and registry paths, and the detector is self-tested.
- Static test confirms no `backend/` module imports the four new modules and `__init__` does not.

## Constraints
- Fixtures only, no network, no new dependencies (`pyproject.toml`/`uv.lock` untouched), nothing committed, no G4 doc.
- `.agents/`, `.kiro/steering/` and the spec zip are untracked and unmodified by this work. The modified tracked build artifacts (`.pyc`, `PKG-INFO`, `web/node_modules/.package-lock.json`) have mtimes of 2026-11-06, before this step (2026-11-09), so they are not attributable to 4.5/4.6.
- No requirement or invariant weakened; the `models.py` tightening in the tree belongs to earlier tasks and makes the schema stricter.

## Non-blocking notes
- Arc-disabled with no high-impact flag yields the floor (allowed) with NO_TRIGGER. This is consistent with "escalate only on stated triggers".
- Band edges, budgets and opinion mapping are unmeasured defaults, as ADR-031 states.
