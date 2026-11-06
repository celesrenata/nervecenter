# NerveShield — live cluster verification (Stage 0, 1, 2)

**Cluster:** k3s v1.36.5, 4 amd64 nodes, namespace `nervecenter` (owner stated nothing in it matters at this time).
**Images deployed:** `b7e6de7` (Stage 1, flag off, then Stage 2 flag on) and `fcf133e` (fixes). Built `linux/amd64` on an arm64 Mac, pushed to `registry.celestium.life`.
**Legend:** VERIFIED = observed directly on the cluster by the orchestrator. Counts and enum values only were read; no message content, subject, sender or address was printed.

## Stage 0 — baseline (read-only)
Before: `api`/`web` at `45ab762` (the pre-NerveShield baseline), `mcp` at `fc0bb30` (matches the manifest; the docs saying `86a6a93` are stale), Alembic head `015_inference_providers`, 0 `mail_*` tables, 69 communications, 2 enabled Gmail connections, `NERVE_INGEST_ENABLED=1`, `NERVE_MAIL_SECURITY_ENABLED` unset. Object store is a shared MinIO (`minio-crawler-hl.minio-service.svc:9000`) over plain `http`.

## Stage 1 — deploy with the flag off (VERIFIED)
- Image inspected BEFORE push: linux/amd64, 17 modules import (including the full app), migrations 015-017 packaged, `mail_security_enabled()` is False with no env.
- Migration Job applied 016 and 017: head `017_mail_intel`, six `mail_*` tables, `account_scope` indexes present, 0 jobs and 0 assessments with the flag off.
- `mcp` untouched; new routes registered GET-only and return 401 without a token; public ingress redirects to Keycloak SSO; ingest kept running; no errors in logs.

## Stage 2 — flag on, SHADOW (VERIFIED, with two defects found and fixed)
Set via `nervcenter-config` (also in `deployments/kubernetes/base/configmap.yaml`, UNCOMMITTED), rolled `api` only.
- The runner starts silently (no start log by design); proven alive by scheduling 5 real jobs through the system's own seam: all `DONE` within ~15 s, content resolved from MinIO, `LOW_PRIORITY`/`ASSESSED`/`SHADOW`.
- **Defect 1 (found on the cluster):** assessments said `evidence_fidelity=ORIGINAL_RFC822`, but the 5 stored objects inspected (booleans only) were plain UTF-8 text with zero RFC822 headers, no Authentication-Results and no DKIM signature, i.e. decoded body text. Synthetic fixtures had hidden this. Fixed in `ddf2b00` (NS-ADR-024): fidelity comes only from the acquisition mode recorded by trusted ingest code, fails safe to body-only, and body content can never promote it.
- **Defect 2 (found by reading the deployed wiring):** the runner had no sources provider, so the commit-to-schedule crash window was never recovered and the 66 older messages were never assessed. `communications` has no connection-id column; the connection id and message id exist only inside `content_ref` (`content://mail/<uuid>/<message-id>`). Fixed in `fcf133e`: strict-grammar bounded idempotent backfill; its job key is identical to the live seam's (shared builder, proven by test).
- Redeployed `fcf133e`. I deleted the 5 mislabeled rows (guarded transaction that aborts unless exactly 5 assessments and 5 jobs match; they were my own test artifacts from the buggy build). The backfill then rebuilt them.

### Result after the fix
- 71 communications, 71 jobs `DONE`, 71 assessments: all `BODY_ONLY`, `SHADOW`, `mail-security-policy@3`; 70 `LOW_PRIORITY` and 1 `REVIEW`.
- Evidence rows: all `LEGACY_BODY_ONLY` with missing parts `HEADERS_NOT_CAPTURED, MIME_STRUCTURE_NOT_CAPTURED, ATTACHMENTS_NOT_CAPTURED, AUTHENTICATION_EVIDENCE_NOT_CAPTURED, ENVELOPE_NOT_CAPTURED`.
- Privacy scan over all 222 security rows: 0 email addresses, 0 URLs, 0 real subject lines, 0 sender handles, 0 display names. API logs: 0 errors, 0 email addresses.
- `attention_items` 13 before and after, `communications` 71, `commitments` 0: the security pipeline created nothing in Today.
- `api` pod 0 restarts, about 9m CPU and 107Mi memory.

### The one REVIEW
All 9 findings in the data are `DOMAIN_TYPOSQUAT_ASCII` (`MEDIUM`, `URL_DECEPTION`, `SUSPECTED`) and belong to ONE message, which is the one `REVIEW`. Findings store only opaque evidence refs, so whether this is a real lookalike or a false positive was not determined (it would require reading that message). One false-positive-prone sample of 71 is not a detection-quality measurement.

## Not established by this run
- No API/UI visibility: the assessments API still needs an authorization decision (open); the UI shows "Not assessed".
- No adversarial content has been processed on the cluster; only ordinary real mail.
- Auth axes are all UNKNOWN (no headers are captured). `SAFE_TO_PROCESS` remains unreachable by design (NS-ADR-013).
- No real `clamd`, no feed, no egress transport, no alert delivery exist on the cluster.
- Postgres concurrency tests passed on PostgreSQL 16.15 in a local throwaway container, not on the cluster's Postgres.
- Ingest content-type mislabel (`message/rfc822` on decoded text) is unchanged and still open.

## Operational notes
- `deployments/harbor/deploy.sh` runs an interactive `docker login`, which fails in a non-interactive shell. A temporary copy without that block was used and then deleted. Consider an `HARBOR_USER`/`HARBOR_PASS` path or a skip option in the real script.
- `.roo` deploy docs say `mcp` is pinned to `86a6a93`; live and manifest are `fc0bb30`.
- `configmap.yaml` carries `NERVE_MAIL_SECURITY_ENABLED: "1"` and is not committed. Rolling back is: set it to `"0"`, apply, restart `api`. Schema rollback (downgrade 017 then 016) drops only derived data.
