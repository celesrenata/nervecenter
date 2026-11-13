# G8 follow-on gate

Date: 2026-11-13. Scope: every follow-on the owner approved after G8 (NS-ADR-064 to NS-ADR-078). `tasks.md` defines no gate after G8, so this gate reviews the follow-ons against the same bar: live evidence, invariants, real-Postgres migration and race proofs, and owner decisions.

Verdict: **PASS for SHADOW operation.** Nothing here is cleared to influence dispositions beyond what is already live. Owner decisions are listed at the end.

## Per-item status

| Item | ADR | State | Live evidence |
|---|---|---|---|
| Full capture (Gmail raw, encrypted) | 064 | LIVE | 342/342 jobs DONE, all `GMAIL_RAW` |
| ClamAV sidecar + freshclam | 065 | LIVE | EICAR detected; socket `/run/clamav/clamd.sock` |
| Sent mail as OUTGOING, drafts skipped | 066 | LIVE | 308 outgoing / 342 incoming |
| Thread ledger | 067 | SHADOW | IN 2572 seeded, 1 CONTINUOUS (owner test reply), 7 NOT_A_REPLY; OUT 324 |
| Historical sent-mail fix | 068 | DONE | 306 fixed, 20 drafts dropped; pre-fix `pg_dump` kept |
| Campaigns | 069 | SHADOW | 337 members / 229 campaigns |
| Trusted boundary `mx.google.com` | 070 | LIVE | PROVIDER_REPORTED only; INDEPENDENT = 0 |
| Raw backfill as revision 2 | 071 | DONE | all jobs on revision with raw |
| Calibration policy@4 | 072 | LIVE | REVIEW 102 → 18 |
| Semantic observer | 073 | SHADOW | 348 COMPLETED, 21 OUTPUT_NOT_JSON, 11 UNKNOWN_EVIDENCE_REF, 1 SCHEMA_INVALID, 1 ROUTE_DENIED_REGISTRY_TIMEOUT |
| Per-message registry snapshot | 074 | LIVE | registry denials now bounded to 1 timeout |
| Personal expectedness | 075 | SHADOW | 79 counts / 65 senders |
| OmniRoute key (owner option 3) | 076 | ACCEPTED | `OmnirouteMGMT` retained |
| Link calibration policy@5 | 077 | LIVE | REVIEW 18 → 9 |
| "This was expected" feedback | 078 | LIVE (UI) | 0 feedback rows yet |
| Channel grants (G8 item 4) | — | DEFERRED | no non-email channel exists |

Latest dispositions: 332 LOW_PRIORITY, 9 REVIEW, 1 INCOMPLETE. Holds: 5, all RELEASED. Migration head `025_mail_expect_feedback`. All pods healthy.

## Invariants (live, queried)

- `SAFE_TO_PROCESS` dispositions: 0.
- `INDEPENDENT` auth evidence: 0 (boundary results are provider-reported only).
- Model runs referenced from assessments: 0 (observer cannot affect verdicts).
- Observer gist stored: 0 (enum codes only).
- Observer path writes no memory and logs no prompts (proved before enablement, NS-ADR-073).
- `backend/mail_security` stays network-free except `clamav.py` (guard tests).

## Postgres proofs

Scratch container `pgvector/pgvector:pg16` (removed after the run):

- Migration round trip via `backend.persistence.migrate._build_config()`: upgrade to 025 (6 follow-on tables), downgrade to `020_mail_confirmations` (0), upgrade again (6).
- `tests/backend/test_mail_security_pg_followons.py` (integration): concurrent ledger, campaign, observation, expectedness-count and feedback writes. 12 passed together with `test_mail_security_pg_concurrency.py`.
- Note: the `alembic` CLI fails on the placeholder `sqlalchemy.url` in `alembic.ini`; use the Python API.

## Suite

- `uv run pytest tests/ -m "not integration"`: 8633 passed, 1 known unrelated failure (`test_routines.py::TestWellnessSleepSeam::test_returns_iso_records_with_source`).
- `web` Playwright: 105 passed.

## Open risks

1. Observer category is unreliable (38% PHISHING_SUSPECTED on ordinary mail). It is stored but must not drive anything. 21 non-JSON outputs show the model also drifts on format.
2. Registry denials (about 8% before NS-ADR-074) are mitigated by the per-message snapshot plus one retry, not eliminated.
3. `OmnirouteMGMT` key breadth (`manage`) is wider than the observer needs; accepted by the owner.
4. 9 REVIEW items remain for owner triage.
5. Expectedness has no feedback yet, so its UNCERTAIN band is uncalibrated.

## Owner decisions

1. Keep all SHADOW follow-ons in SHADOW until more data accrues (recommended).
2. Whether any SHADOW reason (thread continuity, campaign-of-flagged, unexpected high-impact ask) may feed the `REVIEW_GATED` promotion gate.
3. Observer: keep request types and ignore category (recommended), drop category entirely, or evaluate the Qwen challenger.
4. Scope the separate specs `nerve-shield-smtp-ingress` and `nerve-shield-detonation`; each needs its own G0.
5. Housekeeping: `tasks.md` checkboxes are still unticked although G0–G8 gate docs exist. Tick them?
