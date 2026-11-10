# Phase 4 task 4.3 results: Granite observer (fixtures only)

Status: implemented, not committed. No review.json existed, so this is the initial implementation.

## Files changed
- NEW `backend/mail_security/granite_observer.py` (library; no network imports; not wired)
- NEW `tests/backend/test_mail_security_granite_observer.py` (fixture registry and transports)
- EDIT `tests/backend/test_mail_security_route_guard.py` (static "not wired" scan now also skips `granite_observer.py`, which legitimately imports route_guard)
- EDIT `.kiro/specs/nerve-shield/decisions.md` (NS-ADR-029)
- NEW this file

## What was run
- `uv run pytest tests/backend/test_mail_security_granite_observer.py tests/backend/test_mail_security_route_guard.py -q`: all passed after fixing two test-authoring bugs.
- `uv run ruff check` and `ruff format --check` on the module and both test files: clean.
- `uv run pytest tests/ -m "not integration" -q`: 6620 passed, 2 skipped, 7 deselected, 1 failed. The only failure is the known `tests/backend/test_routines.py::TestWellnessSleepSeam::test_returns_iso_records_with_source`.

## Coverage
Valid output accepted; tool-free fixed request keys; delimiter-neutralised prompt; rejected with typed codes: non-text, None, prose, fenced, truncated, NaN, duplicate keys, deep nesting, array, string, oversize, extra top-level and nested keys, overlong gist, wrong types, coerced confidence string, bad enum/version, missing field, free-text refs, tool-call shapes (top and nested), role spoof, wrong source_ref, invented evidence ref. Retry bound (cap 3), recovery on retry, no retry for hostile output, timeout and transport errors typed with text dropped, deny (remote leaf, unknown alias) sends nothing and is not retried, input budget rejections, log capture shows only outcome/digest/prompt version/attempts/latency with no content, static no-network/not-wired check.

## Not verified
Granite 4.2 / `hybrid/reader` real route, real transport, model quality and latency. Budgets are unmeasured defaults. Nothing committed or pushed; no network, cluster or real mail touched.
