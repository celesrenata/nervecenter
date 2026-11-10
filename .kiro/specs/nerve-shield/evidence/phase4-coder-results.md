# Phase 4 coder results (tasks 4.1, 4.2) - uncommitted, awaiting orchestrator verification

## Changed files
- backend/mail_security/models.py (4.1: strict SemanticObservation tightening)
- tests/backend/test_mail_security_models.py (pass schema_version explicitly; fields are now required)
- tests/backend/test_mail_security_observation_contract.py (new, 4.1)
- backend/mail_security/route_guard.py (new, 4.2)
- tests/backend/test_mail_security_route_guard.py (new, 4.2)
- .kiro/specs/nerve-shield/decisions.md (NS-ADR-028)
- this file

## 4.1 gaps found and closed
- Schema `required` lists all 14 top-level fields (and nested ones) but the model defaulted lists, `schema_version`, `model_confidence`: omission was accepted. Now required, matching the schema.
- Confidence was lax-coerced ("0.5", True accepted). Now `Confidence` = strict float, 0..1, NaN/inf rejected. `high_impact`/`requires_independent_verification` are strict bool.
- Added sync test comparing model JSON schema with the schema file (properties, required, additionalProperties, enums, lengths, patterns, bounds, maxItems); uniqueItems and the version const (not expressible by pydantic) covered by dedicated tests.
- Extra-key rejection at every nested level, overlong strings/collections, bad refs/codes, invalid confidence, enums, required fields.

## 4.2
Module design and risks: see NS-ADR-028. Not imported by any other module (tested).

## Commands run
- `uv run pytest tests/backend/test_mail_security_route_guard.py tests/backend/test_mail_security_observation_contract.py -q` -> 224 passed
- `uv run pytest tests/ -m "not integration" -q` -> 1 failed (known tests/backend/test_routines.py::TestWellnessSleepSeam::test_returns_iso_records_with_source), 6572 passed, 2 skipped, 7 deselected
- `uv run ruff check` on backend/mail_security and the three test files -> passed; `ruff format --check` passes on all changed files except pre-existing unformatted lines in test_mail_security_models.py (not mine, left alone).

## Remaining risks
- Live /api/combos and /api/providers JSON shapes are assumed (tolerant parser, unknown shape -> deny); unverified against the cluster (no contact allowed).
- Host-name trust without DNS; check/use TOCTOU; alias naming mismatch (hybrid/code vs hybrid/reader/coder) is an owner decision.
- Making observation fields required could break a future producer that omitted empty lists; intended per schema.
