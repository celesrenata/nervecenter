# Phase 0.1 — Baseline evidence (NerveShield)

**Task:** 0.1 Establish current HEAD and baseline. **Req:** R-001, R-025.
**Mode:** READ-ONLY audit. No production code changed.
**Legend:** `[VERIFIED]` = observed by running a command / reading code at HEAD; `[INFERRED]` = reasoned from evidence but not directly executed.

## HEAD and branch state `[VERIFIED]`

| Fact | Value | Source |
| --- | --- | --- |
| HEAD SHA | `45ab7626325ee40be7c59d33ba0a871bfb81110f` | `git -C … rev-parse HEAD` |
| Branch | `main` | `git -C … branch --show-current` |
| Working tree | clean except noise + untracked spec artifacts | `git status --porcelain` |
| SHA vs design.md §1 | **matches** recorded snapshot `45ab762…` | design.md §1 |

Untracked/modified entries are all non-source: `backend/persistence/__pycache__/*.pyc`, `web/node_modules/.package-lock.json`, and untracked `.agents/`, `.kiro/`, `nerve-shield-kiro-spec.zip`, `nerve-shield-kiro-spec/`. No tracked source file is dirty.

## Toolchain discovered `[VERIFIED]`

| Tool | Version (local) | Where defined |
| --- | --- | --- |
| uv | 0.13.5 | repo uses `uv sync --locked` (ci.yml) |
| python | 3.13.16 (local); CI pins 3.12; `requires-python>=3.11` | pyproject.toml |
| node | 24.21.0 (local); CI pins 22 | ci.yml web job |
| npm | 11.19.0 | ci.yml web job |

## Canonical build/lint/test commands (discovered, not assumed) `[VERIFIED]`

From `pyproject.toml`, `.github/workflows/ci.yml`, `web/package.json`:

- **Python tests (non-integration):** `uv run pytest tests/ -m "not integration" -q` — CI floor **≥ 601 passed, 0 failed/error**.
- **Python lint:** `uv run ruff check …` (config in `[tool.ruff]`, line-length 100, select E/W/F/I/B/UP; `extend-exclude = deployments, migrations, synthetic-lab`).
- **Python type-check:** `uv run pyright` (config `[tool.pyright]`, `include = backend, sdk/python, schemas`, standard mode).
- **Web build:** `npm ci && npm run build` (`tsc && vite build`) — CI floor ≥ 60 Playwright e2e passed.
- **Web lint:** `npm run lint` (eslint, `--max-warnings 0`).
- **Apple:** `bash apple/scripts/test-apple.sh` — out of scope for NerveShield (Python/web only).

## REAL runs executed in this audit

### Python non-integration suite `[VERIFIED]`

```
uv run pytest tests/ -m "not integration" -q
=> 1850 passed, 3 warnings in 42.33s   (PYTEST_EXIT=0)
```

- Pass count **1850** far exceeds the CI floor of 601. **0 failed, 0 error.**
- 3 warnings only (non-fatal). No import blocker, no collection error.
- The 75 test modules under `tests/backend/` include the suites that cover every module NerveShield touches (see mapping below), all green.

### Python lint (ruff) `[VERIFIED]`

```
uv run ruff check backend schemas
=> Found 416 errors.  (RUFF_EXIT=1)
```

Top categories: `E501` 209 (line length), `B904` 39, `F401` 33 (unused import), `B008` 29, `I001` 27 (import order), `E402` 24, plus small counts of F841/F821/B027/B010/F541/F403/E741/B905/B033.

**Assessment — PRE-EXISTING, UNRELATED TO NERVESHIELD `[VERIFIED]/[INFERRED]`:** these are style/lint findings across the existing backend (e.g. `backend/agents/registry.py`, `schemas/planned_changes.py`); none are in files NerveShield will create. CI's Python job gates on the **pytest** count floor, **not** on `ruff check` (ci.yml has no ruff step), so this is not a merge blocker today. `[INFERRED]` It is a standing repo hygiene gap, not a feature blocker. NerveShield code must itself be ruff-clean.

### Type check / web build — NOT RUN in this audit `[VERIFIED: not run]`

`pyright` and the web `npm run build` were **not executed** here (read-only audit scope; pytest + ruff were sufficient to establish the baseline and no NerveShield code exists yet to type-check). Recorded as `NOT RUN` rather than claimed. Phase 1 must run `uv run pyright` and the web build for any code it adds.

## Import/build blockers unrelated to this feature

- **None blocking tests.** The suite imports and runs with `uv run` against the locked environment; no network fetch on import was triggered (tests use fixtures/fakes — e.g. `StubMailTransport`, `httpx.MockTransport`, `NERVE_OMNIROUTE_FIXTURE`).
- Ruff reports 416 style errors (above) — a hygiene debt, not a build blocker.

## Baseline test modules covering NerveShield-adjacent code `[VERIFIED]`

`tests/backend/` (75 modules) includes, among others:
`test_mail_transport.py`, `test_mail_ingest.py`, `test_mail_connector.py`, `test_mail_oauth_refresh.py`, `test_mail_persistence.py`, `test_detection.py`, `test_detection_wiring.py`, `test_communications.py`, `test_communications_normalize.py`, `test_attention*.py` (6), `test_object_storage.py`, `test_omniroute_transport.py`, `test_p0_pipeline.py`, `test_ingest_gate_health.py`, `test_app_wiring.py`, `test_ask_pipeline.py`. All pass in the 1850-green run. These are the regression guards Phase 1 must keep green.

## Fixtures / no-network discipline `[VERIFIED]`

- Mail transport tests inject `StubMailTransport` (no real IMAP/Gmail).
- OmniRoute tests use `NERVE_OMNIROUTE_FIXTURE=1` fixture replay (no network).
- Object-store tests use `InMemoryObjectStore` / `httpx.MockTransport` (no real S3).
- No real accounts, inbox samples or production data are required or used.

## Done criteria (0.1) — met

Commands + **real** pass/fail counts documented (1850 passed / 0 failed; ruff 416 pre-existing style errors; pyright/web build marked NOT RUN). Blockers unrelated to the feature identified (ruff debt only). ✅
