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

---

# Addendum — gap-closure round (deployed image `625d7d0`)

**Deployed with the real `deployments/harbor/deploy.sh`** (first run of the non-interactive login fix; it ran start to finish with no prompt). Code on the cluster is `625d7d0`; the later commit `a87a72e` is documentation and ConfigMap only.

## What changed on the cluster
- ConfigMap `nervcenter-config` now has `NERVE_MAIL_SECURITY_ENABLED=1` and `NERVE_MAIL_SECURITY_API_SCOPE_MODE=single_tenant` (NS-ADR-025). Code default is `claim_only`; reverting is removing the key and rolling `api`.
- Detector `offline-inspections@2` (NS-ADR-026): first-party registrable domains and brand-owned TLDs.
- Alert logging, content-type label `text/plain; charset=utf-8` on new stored bodies, `.roo` doc fixes, draft (never applied) ClamAV overlay under `deployments/kubernetes/optional/clamav/`.

## Verified directly
- **The one REVIEW was a false positive.** An Apple Card message linking to `applecard.cdn-apple.com` (Apple's CDN) and `email.post.applecard.apple` was flagged by the "hyphenated brand suffix" rule. Only the flagged domain names were read; no subject, sender or body was printed.
- **Detector comparison, old vs new, 50 hosts:** 21 legitimate hosts, 0 false positives remaining (2 fixed); 29 attack lookalikes, 0 regressions, 5 new catches (for example `cdn-apple.com.evil.net`, `icloud.com.evil.net`). Anecdotal sample, not a measured rate.
- **Scope resolver on real Postgres:** returned exactly the 2 enabled mail connections (matches an independent computation), valid scope grammar. `single_tenant` human token: 2 scopes. Device token, empty `sub`, a claim naming an unresolved scope, and an empty claim: 0 each. `claim_only` human token with no claim: 0. All 72 assessments visible through the resolved scopes; a foreign scope saw 0.
- **Live path works unprompted:** a new real email arrived (71 to 72 communications) and was assessed automatically by seam, runner and worker.
- **Rebuild under detector `@2`:** I purged the derived `mail_*` rows (guarded transaction: aborts unless every job is DONE and counts agree; intel tables untouched) so no verdict mixes detector versions (I-10). The backfill rebuilt all 72: all `BODY_ONLY` / `LOW_PRIORITY` / `ASSESSED` / `SHADOW` / policy `@3`, **0 findings, 0 REVIEW**.
- **Safety after rebuild:** `attention_items` 13 and `commitments` 0 (unchanged), pod 0 restarts, 0 errors and 0 email addresses in API logs, privacy scan over 216 rows found 0 email addresses, URLs, real subjects and sender handles.
- **ClamAV client verified against a real clamd 1.5.4** (see `phase2-clamav-real-daemon-verification.md`); not deployed.

## Still open (unchanged by this round)
- Alert DELIVERY to a person (only log lines exist); the channel is an owner decision.
- Re-assessment policy after detector changes: today a detector change needs a derived-data purge or an `analysis_profile_version` bump; no policy decides which.
- The first-party domain allowlist is curated data needing periodic owner review.
- Raw RFC822 capture is blocked on the object-store encryption decision (MinIO is shared and plain `http`).
- No egress transport; no `clamd` in the cluster; the unix-socket path is unverified on a real daemon.
- Arc GPU work (Phase 3 G3) needs a GTi14 host.
- `single_tenant` means every authenticated human can read metadata-only assessments for every enabled mail connection; there is no per-user ownership model and `src:` scopes are not covered.
- Two commits during this work staged `decisions.md` before the owner asked for commits; all commits are local and nothing was pushed.
