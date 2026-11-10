# Phase 4 task 4.4 results: Qwen challenger (fixtures only)
Status: implemented, not committed (no commit allowed). No 4.4 review.json existed, so this is the initial implementation.
## Files changed
- NEW `backend/mail_security/qwen_challenger.py` (library; no network imports; not wired; granite_observer.py unchanged)
- NEW `tests/backend/test_mail_security_qwen_challenger.py`
- EDIT `tests/backend/test_mail_security_route_guard.py` and `tests/backend/test_mail_security_granite_observer.py` (static "not wired" scans now also skip `qwen_challenger.py`, which legitimately imports those modules; the route_guard edit was also reformatted by ruff)
- EDIT `.kiro/specs/nerve-shield/decisions.md` (NS-ADR-030)
- NEW this file
## Design summary
- Reuses granite_observer's EvidencePacket, sanitiser regexes, JSON/tool-shape helpers, ObserverConfig and ObserverTransport. Adds a `ChallengeEnvelope` (strict, extra forbidden): verdict AGREE/DISAGREE/INSUFFICIENT, closed-enum rationale codes (max 8, unique), `revised_observation` null unless DISAGREE (then a strict `SemanticObservation` with role QWEN_CHALLENGER, matching source_ref, only supplied evidence refs).
- Granite observation is re-validated and passed as untrusted data between separate delimiters (neutralised, 16000-char cap).
- Route guard on every attempt; deny sends nothing and is not retried. Retry cap 3; hostile/identity/ref/inconsistent output never retried.
- Optional: `transport=None` gives UNAVAILABLE/NOT_DEPLOYED; unknown alias or registry down maps to UNAVAILABLE; `challenge_optional` never raises.
- `resolve_cautious(floor, challenge)`: absent/failed/AGREE returns floor; floors other than SAFE/LOW are never changed; SAFE/LOW + DISAGREE => REVIEW, + INSUFFICIENT => INCOMPLETE; never lowers, never allows, never RESTRICTED; invalid floor fails closed to INCOMPLETE.
## What was run
- `uv run pytest tests/backend/test_mail_security_qwen_challenger.py tests/backend/test_mail_security_granite_observer.py tests/backend/test_mail_security_route_guard.py -q`: 245 passed (after fixing three test-authoring bugs: a wrong enum fixture value, a TEST-NET address that Python classes as private, and an over-strict `__suppress_context__` assertion).
- `uv run ruff check` and `ruff format --check` on the module and the three test files: clean.
- `uv run pytest tests/ -m "not integration" -q`: 6709 passed, 2 skipped, 7 deselected, 1 failed. The only failure is the known `tests/backend/test_routines.py::TestWellnessSleepSeam::test_returns_iso_records_with_source`.
## Coverage
AGREE, DISAGREE (with and without distinct revised observation), INSUFFICIENT; 27 malformed/hostile reply shapes (non-text, prose, fenced, truncated, NaN, duplicate keys, deep nesting, oversize, extra keys, bad/lowercase verdict, free-text or duplicate or too many codes, bad version, missing field, invalid revised observation, tool-call shapes top and nested, role spoof, wrong source_ref, revised role spoof, unknown evidence ref, AGREE with revision, DISAGREE with no reason) each with a typed code and no chained cause; non-retry of hostile output; retry bound (3) and recovery; transport/timeout typing with text dropped; deny routes (remote hosts) send nothing and are not retried; NOT_DEPLOYED, unknown alias and down registry give UNAVAILABLE; `challenge_optional` never raises (invalid input, invalid packet, junk, denied, internal error); Granite input mismatch rejected; delimiter neutralisation; observation size cap; full 15-row cautious-resolution table plus no-challenge and failed-status rows for every Disposition (never lowers, never allow, never RESTRICTED); log capture and result fields contain no body, subject, gist, raw output, exception text, secret or source ref; static no-network and no-import-into-pipeline check.
## Not verified
No real Qwen model or `hybrid/code` route was contacted; model quality, latency, prompt effectiveness and the noise rate of INSUFFICIENT => INCOMPLETE are unmeasured. Nothing committed or pushed; no network, cluster or real mail touched.
