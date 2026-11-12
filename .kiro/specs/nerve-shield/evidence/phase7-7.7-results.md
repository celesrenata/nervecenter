# Task 7.7 — Operations dashboard results

Date: 2026-11-11. No migration, and no flag change except one new ConfigMap key for the read-only ArcForge status target.

## What changed

**`GET /api/mail-security/ops`** is on the read-only router. It is scoped to the caller's accounts; without scopes it returns 404, and without auth 401. It composes these sections:

| Section | Source |
|---|---|
| `health` | Existing allow-listed `observability.health_view`: stages, alerts, queue depth/age, fidelity/completeness, intel feed versions and live counts, egress reasons, latency, sweeper age. |
| `review_queue` | The 7.3 queue engine: pending by tier and delivery, digest, reviewed, quiet hours active. |
| `counts` | `mail_security_ops_repo.load_ops_counts`: <br>• `versions`: analysis profile / policy / model version → count <br>• dispositions <br>• `model_runs`: route / model / runtime / status → count, mean and max latency <br>• `feedback`: by kind, flagged false-positive rate = FALSE_POSITIVE / (FALSE_POSITIVE + CONFIRMED_THREAT), missed-threat reports <br>• `gate_holds` by status <br>• `confirmations` by status and risk |
| `runner` | Whether the in-process runner is running, plus its typed failure counters. |
| `arc` | ArcForge service status; see below. |

**Redaction.** Every label passes a strict safe-label regex, and anything else becomes `OTHER`. No account, assessment, message, person or reviewer id, and no free text, is ever emitted.

**ArcForge status.** `backend/mail_security/classifier/arcforge_status.py` works through the hardened `ArcForgeClient`:
- It accepts up to 4 comma-separated literal private IPs, with no DNS. Any invalid entry makes the whole setting `NOT_CONFIGURED`, and no socket is opened.
- It calls only `/ready`, `/v1/models` and `/metrics`. A test enforces that it never calls `embed` or `classify`.
- Hosts are reported by position label only (`arcforge-1`, …), never by IP. Each shows readiness, the model identity as the service reports it, and an allow-listed activity summary: queue depth, active inferences, request and item totals, inference count and mean seconds, and the wedged-device flag.
- Results are cached for 30 seconds.
- This is service activity, not a GPU-percent measurement; ArcForge exposes no device-utilization series, and the UI says so.

**Classifier guards.** The I-08 guard (classifier output is not an authority) was widened by exactly one import: the dashboard endpoint may import `arcforge_status` and nothing else from the classifier package. The module also avoids `os`; the caller passes in the environment, which keeps the package's stdlib guard intact.

**ConfigMap.** Added `NERVE_MAIL_SECURITY_ARCFORGE_URL: "http://10.1.1.15:8100,http://10.1.1.12:8100"`. Both were verified as ArcForge `qwen3-embedding-0.6b` instances reachable from the api pod.

**UI.** The `/security-ops` view ("Security ops" in the nav) has sections for health, review queue, Arc service, false-positive review, model runs, versions and feeds, gate and confirmations, and runner failures. Everything renders as inert text, with a fail-closed empty state.

## Verification

| Check | Result |
|---|---|
| `tests/backend/test_mail_security_ops_dashboard.py` (new) | 13 passed |
| Classifier package guards (incl. new no-embed test, single allowed import) | pass |
| Full suite `uv run pytest tests/ -m "not integration" -q` | 8440 passed, 1 failed (known date-dependent `test_routines`) before the multi-host change; affected files re-run 42/42 after |
| `ruff` new files | clean |
| `npm run build` | ok |
| Playwright full e2e | 104 passed (new `security-ops.spec.ts` OPS-1..3) |
| Real client vs both live ArcForge hosts (from the dev machine) | READY / READY, `qwen3-embedding-0.6b-int8` on GPU, real activity counters |

**Live-data wiring tests.** These run against real SQLite repos:
- Counts cover only the caller's scope.
- Model-run aggregation is correct (count 2, mean 100 ms, max 120 ms).
- The false-positive rate is 2/3, and pending review is 1.
- Arc shows NOT_CONFIGURED without the env key.
- The health view is composed in.

**Redaction tests:**
- No canary, scope, or `asm:` / `src:` / `rev:` / `fb:` id appears in the response.
- A recursive walk confirms every key and value is a bounded scalar.
- Unsafe model labels are bucketed as `OTHER`.

**Arc tests:**
- The allow-list sums labels and drops unknown series, including a canary series.
- Public, hostname-based, path-bearing and mixed targets never build a client.
- The ready, unreachable and degraded multi-host shapes are covered, as is the 4-host limit.
- Unsafe identity strings are dropped.
- The 30-second cache works.

## Not verified / limits

- **No GPU-percent figure.** "Arc utilization" here is ArcForge service activity; ArcForge publishes no device-utilization series.
- **The model-run figures are mostly empty on today's data.** Live assessments have no `model_runs` (the Granite and Qwen chain isn't wired into the live pipeline), so that section will mostly show "None".
- **Not in-process across replicas.** Runner failure counters reset on pod restart, and the Arc cache is per process.
- **Arc probes are unauthenticated.** ArcForge endpoint authentication is still an open owner item; the dashboard calls only the unauthenticated read-only routes.
