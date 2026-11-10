# NerveShield Phase 4, tasks 4.5 and 4.6: results

Status: implemented, uncommitted, not wired into the pipeline. Fixtures only: no real mail, model, network, cluster or host was touched. No new dependencies. The G4 gate document is not written here.

## Task 4.5: escalation policy (built by the previous step)

- `backend/mail_security/escalation.py`: pure, versioned (`mail-escalation-policy@1`) decision function `decide_escalation(inputs, config)` returning the disposition so far plus the single next stage (`ARC`, `GRANITE`, `QWEN`) or `None`.
- `tests/backend/test_mail_security_escalation.py`: 87 table-driven and exhaustive-product tests.
- NS-ADR-031 appended to `decisions.md` (PROPOSED).
- Properties: a later stage never lowers the deterministic/PDP floor; models never produce `RESTRICTED` or an allow; failure, unavailability, denial, rate limit and over-budget evidence give `REVIEW` or `INCOMPLETE`; Qwen absence or failure equals no Qwen; invalid input gives `INCOMPLETE`; `log_fields()` carries enums and counts only.
- Unmeasured defaults (Arc band edges 0.2/0.8, call and evidence budgets, opinion mapping) remain unmeasured. Cost, high-risk coverage and safe-abstention metrics are not computed (no labelled corpus).

## Task 4.6: prompt-injection red team (this step)

### Files
- `tests/backend/fixtures/mail_security_injection_corpus.json`: 13 synthetic payloads across the five placements: body (4), HTML alt text (2), MIME header values (3, incl. CRLF header smuggling and encoded words), link query strings (2), quoted thread text (2). Each carries a `{CANARY}` placeholder replaced by a fresh per-run token. Payloads include fake delimiters, fake system/assistant turns, tool-call JSON, bidi/control characters, and schema-override requests.
- `tests/backend/test_mail_security_prompt_injection.py`: 728 tests.

### What it drives
`run_chain` runs observer -> challenger -> escalation exactly as a caller would: it loops on `decide_escalation(...).next_stage`, runs `GraniteObserver.observe` or `QwenChallenger.challenge_optional` against an in-memory `LocalOnlyRouteResolver` registry and scripted transports, and maps results to escalation outcomes. Hostile replies come from "compromised model" transports.

### Hostile model output covered (31 Granite and 24 Qwen replies)
Tool-call shapes (top level, nested, in lists, in the revised observation); OpenAI-style envelopes; extra keys (`disposition`, `pdp_override`, `severity`, `bypass_pdp`, `exfil`); schema version changes; missing and mistyped fields; invented verdicts; free-text rationale; unknown and URL-shaped evidence refs; identity and role swaps; "ignore previous / mark safe" prose, fenced and trailing-prose JSON; truncation, NaN, duplicate keys, deep nesting, non-object roots; oversized output; non-text output; transport exceptions embedding canary and secret text; registry outage embedding secrets; route to a non-local provider.

### Assertions
- Request shape is fixed for every payload and placement: exact key set, no tool keys, `stream` false, pinned system prompt, exactly one data block, payload delimiters and control characters neutralised, payload text only inside the data block.
- Hostile output becomes a typed `GraniteObserverError` or `QwenChallengerError` (or a non-COMPLETED `ChallengeResult`) with the expected code; `str(exc)` equals the code; no `__cause__`; retries bounded, byte-identical, and not applied to non-retryable codes (tool-call shape).
- Chain result for every Granite failure is `REVIEW` (high impact) or `INCOMPLETE`; never `SAFE_TO_PROCESS`, never model-produced `RESTRICTED`.
- A schema-valid observation that obeys the injection (gist says "mark safe") is inert data; Qwen `AGREE` cannot clear a high-impact flag or an Arc high score; cautious and `RESTRICTED` floors are returned unchanged with zero model calls.
- Qwen failure of every kind gives exactly the no-Qwen result, and `resolve_cautious` returns the floor. Qwen `DISAGREE` and `INSUFFICIENT` only raise caution.
- Forged Granite observation inputs to the challenger are rejected before any request is sent.
- A model-supplied string cannot become the PDP floor (`decide_escalation` returns `INCOMPLETE`). An exhaustive product over floor, Granite status and opinion, Qwen opinion, high impact and two configs (with Arc saying "benign") never lowers severity, allows, or produces `RESTRICTED`.
- Canary leak check across all captured `caplog` text and records, stdout/stderr, exception strings/reprs/args, log fields, and every registry (route) request path. The detector is self-tested. Registry reads are only the three fixed paths. A denied route sends nothing to the transport.
- Static: no module under `backend/` (other than the four new ones) references `route_guard`, `granite_observer`, `qwen_challenger` or `escalation` by absolute, relative or dynamic import, and `mail_security/__init__.py` does not; the four modules only depend in the expected direction (escalation imports no model module).

### Commands and results
| Command | Result |
|---|---|
| `uv run pytest tests/backend/test_mail_security_prompt_injection.py -q` | 728 passed |
| `uv run ruff check` and `ruff format --check` on `test_mail_security_prompt_injection.py`, `test_mail_security_escalation.py`, `escalation.py` | clean |
| `uv run pytest tests/ -m "not integration" -q` (regression guard) | 7524 passed, 2 skipped, 7 deselected, 1 failed. The single failure is the known `tests/backend/test_routines.py::TestWellnessSleepSeam::test_returns_iso_records_with_source`. |

### Not verified
- No real model, Granite/Qwen deployment or OmniRoute instance was exercised; "compromised model" behaviour is simulated by scripted transports.
- The corpus is small and hand-written; it is not a measured detection-rate benchmark.
- Evidence packet construction from real MIME/HTML (alt-text extraction, header decoding) is not part of these modules; the tests place payload text where an extractor would, so extractor-level injection handling is untested here.
- Link query strings reach the model only as body text; `EvidenceLink.host` rejects anything but a bare host, which the tests assert.
- Nothing committed or pushed.
