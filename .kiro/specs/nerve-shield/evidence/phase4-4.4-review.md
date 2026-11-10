# Phase 4 task 4.4 review: Qwen challenger

Verdict: APPROVED. No blockers.

Evidence checked (read, suites not re-run; recorded run: 245 targeted passed, full suite 6709 passed, one known unrelated failure in test_routines):
- qwen_challenger.py reuses granite_observer helpers (evidence packet, sanitiser regexes, output scanners, config, transport protocol, retry set); no duplication. granite_observer.py is untracked from 4.3, so "unchanged" cannot be diffed; the 4.4 notes and the module's read-only imports are consistent with that.
- Strict `ChallengeEnvelope` (extra forbidden, pinned schema_version, closed rationale enum, max 8, unique). Revised observation only on DISAGREE, must be QWEN_CHALLENGER, same source_ref, supplied refs only; AGREE/INSUFFICIENT with revision and DISAGREE without codes are rejected (OUTPUT_INCONSISTENT). Granite observation re-validated and neutralised, size capped, sent as untrusted data between separate delimiters.
- No tools: fixed request keys plus FORBIDDEN_REQUEST_KEYS assertion; tool-shaped output rejected.
- Every attempt goes through `dispatch_if_local`; deny raises ROUTE_DENIED, which is not in the retry set, so nothing is sent or retried. Retry bounded by min(config, HARD_MAX_ATTEMPTS=3); hostile/identity/ref/inconsistent output not retried.
- Optional: `transport=None` gives UNAVAILABLE/NOT_DEPLOYED; unknown alias and registry down map to UNAVAILABLE; `challenge_optional` never raises.
- `resolve_cautious`: absent/failed/AGREE returns the floor; floors other than SAFE/LOW unchanged; SAFE/LOW + DISAGREE -> REVIEW, + INSUFFICIENT -> INCOMPLETE; never lowers, never allows, never RESTRICTED; invalid floor -> INCOMPLETE. Matches the requirement that disagreement resolves to the more cautious outcome.
- Logs and exceptions carry enum codes, digest, prompt version, attempts, latency only; `from None` suppresses chained causes; tests cover caplog and result fields.
- Tests cover all required categories (agree/disagree/insufficient, 27 malformed/hostile shapes, retry bound, deny sends nothing, unavailable, full resolution table, no content in logs/exceptions) plus a static no-network / not-imported check. No module under backend imports qwen_challenger. NS-ADR-030 recorded with open risks. Static-test exclusions for the new module are narrow.
- .agents/, .kiro/steering/, the spec zip and build artifacts show as pre-existing untracked/modified state, not attributable to 4.4. The models.py/phase3 diffs belong to 4.3.

Non-blocking notes: `ChallengeResult.revised_observation` holds model-written `gist`, so callers must not log the dataclass repr (module never does; ADR-030 risk 3 covers storage). Real Qwen behavior remains unverified, as stated.
