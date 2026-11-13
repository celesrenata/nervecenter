# Namespace NetworkPolicy, observer re-run, DNS draft, static-inspection spec

Date: 2026-11-13. Owner: "go ahead" (to the three decisions after the observer compact@2 report).

## 1. Default-deny NetworkPolicy for `nervecenter` (NS-ADR-083)

File: `deployments/kubernetes/base/networkpolicy.yaml` (in the kustomization). Rollback: `kubectl -n nervecenter delete networkpolicy -l nervecenter.policy=hardening`.

Flows were derived from the config, not guessed: the Ingress (`/` → oauth:4180, `/mcp/` → mcp:8000), the oauth2-proxy `--upstream=http://web:80`, the web `nginx.conf` (`/api/`, `/mcp/` → `api.nervecenter.svc:8000`), the `mcp` env (`NERVE_MCP_API=http://api:8000`), and `NERVE_NATS_URL` (api only). Traefik runs as a normal pod in `kube-system`.

- **Ingress:**
  - default deny for every pod;
  - allowed: traefik → oauth and mcp; oauth → web; web and mcp → api; api, migration and `db-snapshot` → postgres; api → nats.
- **Egress:**
  - postgres and nats: DNS only;
  - web: DNS plus api;
  - api, oauth and mcp keep egress (Google APIs, SSO, OmniRoute, ArcForge);
  - freshclam keeps its own policy.

Probes, before and after (live):

| Flow | Before | After |
|---|---|---|
| public `/` (oauth redirect) / `/mcp/health` | 302 / 200 | 302 / 200 |
| web → api, mcp → api | ok | ok |
| api → postgres, nats, Gmail :443, OmniRoute | open | open |
| **mcp → postgres** | open | **blocked** |
| **web → internet** | open | **blocked** |
| freshclam → postgres | blocked | blocked |

After 2 minutes: 0 restarts, 0 warning events, 0 api errors. A full redeploy under the policy succeeded: migrate job Complete, new api and web pods Ready.

**Policy-controller lag.** k3s's controller registers a new pod's IP a moment after the pod starts. A one-off pod that connects immediately is refused: the first snapshot attempt failed, and the second, with a `pg_isready` loop, succeeded on try 2. The migrate job has `backoffLimit: 5`, so a lost race costs at most a retried pod. One-off pods should wait for connectivity.

## 2. Observer re-run with compact@2 (NS-ADR-080 follow-up)

- **Snapshot first:** an in-cluster `pg_dump -Fc` (pod label `db-snapshot`, NFS to the NAS, `umask 077`) wrote `/volume1/Backups/nervecenter/nervecenter-db-pre-reobserve-20261113T152059Z.dump`, 27 MB, 67 tables.
- **Deleted** all 388 observation rows (382 compact@1, 6 compact@2). No foreign keys reference the table.
- **Re-observed** with `observe_existing --limit 1000 --no-expectedness`. The new flag stops counting during the run, so counts aren't doubled. Result: 348 model calls, 346 COMPLETED, 2 OUTPUT_NOT_JSON.
- **Rebuilt expectedness** in received order with `seed_expectedness --forget`: 80 old counts forgotten, 147 counts across 75 keyed senders, 0 unexpected high-impact.

| After | Value |
|---|---|
| PHISHING_SUSPECTED | **14 / 346 (4.0%)** (was 129 / 348) |
| gist stored | 0 |
| Dispositions | 338 LOW_PRIORITY, 9 REVIEW, 1 INCOMPLETE (unchanged; 6 new messages since the gate) |
| SAFE_TO_PROCESS | 0 |

## 3. DNS draft (NS-ADR-084)

`nerve-shield-smtp-ingress/dns-records.md` contains ready-to-apply Route 53 change batches and a phased rollout. Nothing was applied.
- `frameshift.net`: SPF, DKIM (generated in the Workspace admin console), then DMARC `p=none` → `quarantine` → `reject`.
- `celestium.life`: remove the SPF self-include; replace the template DMARC `rua`; add a cross-domain report authorization.

## 4. Static deep inspection spec (NS-ADR-085)

`.kiro/specs/nerve-shield-static-inspection/` holds the requirements and tasks, stopped at G0.
- **Measured need:** 13 of 348 messages carry attachments: 45 PDF, 11 ZIP, 3 OOXML, 0 legacy OLE.
- **Design:** a no-network, no-secret sidecar over a UNIX socket that never executes anything. It returns codes and counts only, can only add findings, and feeds policy@6 in SHADOW.
- **Open decisions:** PDF parser library, sidecar vs pod, whether to return link hosts.
