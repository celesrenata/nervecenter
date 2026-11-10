# Phase 4 - Granite observer prompt v2 results

ADR: NS-ADR-032 (`.kiro/specs/nerve-shield/decisions.md`). Prompt version `granite-observer-prompt@2`.

## Change
- `backend/mail_security/granite_observer.py`: `SYSTEM_PROMPT` now states every nested object shape (ClaimedIdentity, ObservationRequest, Component, RiskIndicator, Contradiction), all enum values (generated from the model enums), required keys, `[]`/`null` rules, ref and code rules (code regex `^[A-Z][A-Z0-9_.-]*$`, refs only from supplied source/link/attachment refs), max list sizes, and one benign synthetic example (`PROMPT_EXAMPLE`). No tools, data-in-delimiters and no-secrets rules kept.
- `backend/mail_security/qwen_challenger.py`: Granite prompt-line replacement derives from `granite_observer.PROMPT_VERSION` instead of a pinned `@1` literal (required, otherwise the Qwen request would carry the old line).
- Contract models, JSON schema, validation, retry, route guard and escalation are unchanged (schema file needed no change).
- Tests added to `tests/backend/test_mail_security_granite_observer.py`: version pin; prompt mentions every field name and enum value reachable from `SemanticObservation` (observer_role limited to GRANITE_EXTRACTOR on purpose); example validates via strict JSON validation and appears verbatim in the prompt; ref/code/no-tools statements and length cap.

## Prompt size
3653 characters, about 330 words. Token estimate roughly 900-1100 tokens (chars/4 lower bound, enums and JSON tokenize denser). Not measured with the Granite tokenizer.

## Results
- `uv run pytest tests/ -m "not integration" -q`: 7528 passed, 2 skipped, 7 deselected, 1 failed. The only failure is the known `tests/backend/test_routines.py::TestWellnessSleepSeam::test_returns_iso_records_with_source`.
- `uv run ruff check` and `ruff format` on the three changed Python files: clean.

## Not verified
- v2 was not re-run against the live Granite endpoint (fixtures only, no network). Whether it fixes the observed failures is unproven; a non-conforming reply still fails closed as `OUTPUT_SCHEMA_INVALID`.
