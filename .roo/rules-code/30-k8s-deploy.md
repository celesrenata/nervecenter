# Kubernetes deployment guidance for NerveCenter

Use this file when deploying, upgrading, debugging, or reviewing the NerveCenter Kubernetes install under `deployments/kubernetes/`. Act as a platform operator who knows this cluster's conventions: it uses a single fixed namespace, Harbor for images, Traefik + cert-manager for ingress/TLS, and oauth2-proxy fronts for Keycloak SSO. Do not invent new infrastructure; work within the existing manifests and the cluster's established patterns.

These are the operating rules for cluster work, not a reason to rewrite the app. Preserve the fixed invariants below, keep secrets out of version control, and verify every step before declaring a deploy complete. Treat `kubectl` output and pod logs as evidence; a successful `apply` does not prove the service is healthy.

## 1. Invariants (do not change without a deliberate decision)

1. **Namespace is fixed to `nervecenter`.** Every in-cluster DNS name (`postgres.nervecenter.svc`, `nats.nervecenter.svc`, `api.nervecenter.svc`, `mcp.nervecenter.svc`) and the Ingress TLS host assume this name. Renaming the namespace breaks the Secret values and the web proxy config.
2. **Secrets live out-of-band.** `nervecenter-env`, `harbor-pull`, and `nervecenter-oauth` are NOT listed in the kustomization. They are created or copied by the operator and referenced via `envFrom` / `imagePullSecrets` / `secretKeyRef`, which `kubectl apply` resolves at pod runtime. Never commit real secret values.
3. **The `mcp` Deployment is pinned to a running tag** (`registry.celestium.life/nervecenter/mcp:86a6a93`), not `:latest`. A blind `apply -k` that retags mcp to `:latest` will cause `ImagePullBackOff` because no such tag exists in Harbor. If you retag mcp, confirm the tag is present in the registry first.
4. **SSO gate is not wired into the Ingress by default** until the Keycloak `nervecenter` client exists and its `client-secret` is patched into the `nervecenter-oauth` Secret. The Ingress already points at `nervecenter-oauth` for `/`, so the gate is active only once both preconditions are met. `/mcp/` is intentionally open (direct to the mcp service).
5. **Self-hosted Postgres credentials default to `nervcenter:nervcenter`** and the database is `nervcenter`, matching the app's `DEFAULT_DATABASE_URL` in `backend/persistence/connection.py`. The Secret carries both `NERVE_*` (deploy contract) and `NERVECENTER_*` (backend runtime) keys; both are injected so the app works before the env-var names are normalized.

## 2. Deployment topology

The cluster runs six workloads in one namespace, plus a migration Job:

| Component | Kind | Image / source | Service (port) | Probe path |
| --- | --- | --- | --- | --- |
| `postgres` | StatefulSet | `pgvector/pgvector:pg16` | headless `postgres:5432` | `pg_isready -U nervcenter -d nervcenter` |
| `nats` | Deployment | `nats:2.10-alpine` (`--jetstream`, `--http_port 8222`) | `nats:4222` | `GET /varz` on 8222 |
| `nervecenter-migrate` | Job | same image as `api` | — (runs `backend.persistence.migrate upgrade`) | Job success/completion |
| `api` | Deployment | `.../nervecenter/api:TAG` | `api:8000` (ClusterIP) | `GET /api/health` on 8000 |
| `mcp` | Deployment | `.../nervecenter/mcp:86a6a93` | `mcp:8000` (ClusterIP) | `GET /mcp/health` on 8000 |
| `web` | Deployment | `.../nervecenter/web:TAG` (nginx) | `web:80` (ClusterIP) | `GET /` on 80 |
| `nervecenter-oauth` | Deployment | `quay.io/oauth2-proxy/oauth2-proxy:v7.15.4` | `nervecenter-oauth:4180` (ClusterIP) | `GET /ready` on 4180 |

Routing: `Ingress (nervecenter.celestium.life)` → `/mcp/` → `mcp:8000` (open, direct); `/` → `nervecenter-oauth:4180` → `web:80` → (SPA static | `/api/` → `api:8000`). TLS is provisioned automatically by cert-manager via the `cert-manager.io/cluster-issuer: celestium-le-production` annotation on the Ingress.

Config flows from `nervcenter-config` (ConfigMap: `APP_ENV`, `LOG_LEVEL`, `TZ`, `API_PORT=8000`, `API_UPSTREAM=api.nervecenter.svc`, `NERVE_MCP_API=http://api:8000`). The web nginx reads `API_UPSTREAM`/`API_PORT` to proxy `/api/` and `/mcp/` in-cluster.

## 3. Start by inspecting the existing state

Before changing anything, establish the baseline:

```bash
kubectl -n nervecenter get all
kubectl -n nervecenter get deploy,job -o wide
kubectl -n nervecenter get secret   # confirm the three out-of-band secrets exist
kubectl -n nervecenter get deploy mcp -o jsonpath='{.spec.template.spec.containers[0].image}'
```

Identify what is already running and the current image tags. Preserve any operator-pinned tag (especially `mcp`) and any custom values already in the live objects. A redeploy should converge the cluster to the manifests, not accidentally roll a pinned image back to a stale placeholder.

If you cannot read the cluster (no kubeconfig / network), say so and stop; do not fabricate the state of a running system.

## 4. Pre-deploy checks

- **Registry access:** confirm the `harbor-pull` Secret exists in the `nervecenter` namespace and is valid. The canonical way to obtain it is to copy the existing per-namespace secret (e.g. from `beeflow-service`), not to hard-code credentials:
  ```bash
  kubectl get secret -n beeflow-service harbor-pull -o yaml \
    | sed -e '/^  namespace:/d' | kubectl apply -n nervecenter -f -
  ```
  If `deployments/harbor/deploy.sh` is available, it performs this copy automatically when the namespace lacks the secret.
- **Image tags exist:** verify each image tag you are about to deploy is present in Harbor. For `api` and `web`, `deployments/harbor/deploy.sh` patches the `:latest` placeholder with the git short SHA before `apply -k`; confirm the tag was built and pushed.
- **Secret values are current:** if you changed the Postgres URL, NATS URL, or OmniRoute URL, re-encode the affected base64 values in `secrets.yaml` and re-apply. Do not edit the committed `secrets.yaml.example` placeholder.
- **Storage:** the Postgres PVC uses `storageClassName: longhorn`, 10Gi, `ReadWriteOnce`. Confirm the StorageClass is present and has capacity before creating the StatefulSet.

## 5. Deploy in dependency order

The kustomization lists resources in a safe order, but for a first deploy (or when the Job and Deployments are decoupled) respect this sequence:

1. Namespace
2. ConfigMap
3. Postgres: PVC → StatefulSet → Service
4. NATS: Deployment → Service
5. Secrets (out-of-band): `nervecenter-env`, `harbor-pull`, and (if enabling SSO) `nervecenter-oauth`
6. Migration Job: `nervecenter-migrate`
7. `api` Deployment → Service
8. `mcp` Deployment → Service
9. `web` Deployment → Service
10. SSO gate: `nervecenter-oauth` Service → Deployment
11. Ingress

The single-command path is:

```bash
kubectl apply -k deployments/kubernetes
```

`kubectl apply -k deployments/kubernetes` and `kubectl apply -k deployments/kubernetes/base` resolve to the same objects (the top-level kustomization is a pass-through to `base/`).

**Critical ordering rule:** run the migration Job *before* rolling the `api` Deployment on a first deploy (or any deploy that ships schema changes). Alembic is idempotent, so a no-op migration completes immediately, but the API should not come up against an un-migrated schema. For upgrades, re-apply the Job on every upgrade and confirm it completes `1/1 Completed` before (or immediately after) the API rollout.

Wait for dependencies to be ready before proceeding:

```bash
kubectl -n nervecenter wait --for=condition=Ready pod -l app=postgres --timeout=180s
kubectl -n nervecenter wait --for=condition=Ready pod -l app=nats --timeout=120s
kubectl -n nervecenter wait --for=condition=complete job/nervecenter-migrate --timeout=180s
```

## 6. SSO (Keycloak) activation

The oauth2-proxy gate is deployed but inert until two preconditions are met:

1. A Keycloak client named `nervecenter` exists in realm `celestium` at `https://sso.celestium.life/realms/celestium`.
2. Its real `client-secret` and a fresh `cookie-secret` are patched into the `nervecenter-oauth` Secret.

To activate:

```bash
cp deployments/kubernetes/base/secrets.yaml.example secrets.yaml
# In secrets.yaml (the nervecenter-oauth block):
#   client-secret: base64 of the real Keycloak client secret
#   cookie-secret: base64 of `openssl rand -hex 16`
openssl rand -hex 16          # -> 32 hex chars = 16 bytes (AES-valid)
kubectl -n nervecenter apply -f secrets.yaml
```

The Ingress already routes `/` to `nervecenter-oauth:4180`, so once the Secret is live the gate becomes active. Unauthenticated visitors are 302-redirected to the Keycloak authorization endpoint; only authenticated users reach the app.

Known gaps to preserve, not "fix":
- **No `--allowed-group`:** no `nervecenter-users` group exists in the realm yet. Adding a non-existent group would lock every user out. Once the group is created, add `--allowed-group=nervecenter-users` and re-roll.
- **No `--pass-access-token`:** the API validates NerveCenter-minted HS256 tokens (`backend/api/auth_service.py verify_token`); a raw Keycloak RS256 token is not accepted as-is. Forwarding the identity token into the API is a separate, explicit follow-up — do not enable it casually.

## 7. Verify after deploy

A successful `apply` is not proof of health. Confirm each layer:

```bash
# Postgres reachable and the DB exists
kubectl -n nervecenter exec -it deploy/postgres -- \
  psql -U nervcenter -d nervcenter -c "SELECT current_database();"

# NATS / JetStream live
kubectl -n nervecenter exec deploy/nats -- wget -qO- http://127.0.0.1:8222/varz | head

# API health endpoint
kubectl -n nervecenter port-forward svc/api 8000:8000 &
curl -fsS http://localhost:8000/api/health

# MCP health endpoint
curl -fsS http://localhost:8000/mcp/health   # or port-forward svc/mcp

# Web (via the SSO gate) — expect a 302 to Keycloak when SSO is active
curl -is https://nervecenter.celestium.life/ | head
```

Check pod status and any restarts:

```bash
kubectl -n nervecenter get pods -o wide
kubectl -n nervecenter describe pod -l app=api | tail -n 20
kubectl -n nervecenter logs deploy/api --tail=100
```

Confirm the Ingress has a TLS certificate (cert-manager) and the correct backend:

```bash
kubectl -n nervecenter get ingress
kubectl -n nervecenter describe ingress nervcenter-celestium
```

## 8. Troubleshooting quick reference

| Symptom | Likely cause | First check |
| --- | --- | --- |
| `ImagePullBackOff` on a workload | Tag not in Harbor, or bad `harbor-pull` Secret | Confirm the tag exists in the registry; re-copy the pull Secret from a known-good namespace |
| `ImagePullBackOff` on `mcp` only | `mcp` was retagged to `:latest`, which is not in Harbor | Reset `mcp` to its pinned running tag (see invariant 3) |
| API pods crash-loop, DB errors in logs | Migration not applied, or `nervecenter-env` Secret URL is wrong/stale | Run the Job; verify the decoded `NERVE_DB_URL` matches the live Postgres service |
| `postgres` not `Ready` | PVC pending (no `longhorn` capacity), or probe failing | `kubectl get pvc -n nervecenter`; check `pg_isready` in pod logs |
| `nats` not `Ready` | Monitoring port 8222 not reachable | Confirm `--http_port 8222` is in the container args |
| SSO redirects in a loop or token exchange fails | Placeholder `client-secret` not replaced, or redirect URL mismatch | Confirm the real Keycloak client secret is in `nervecenter-oauth`; verify `--redirect-url` matches the Ingress host |
| TLS error / no certificate on the host | cert-manager issuer missing or annotation wrong | Check the `cert-manager.io/cluster-issuer` annotation and that `celestium-le-production` exists |

Always report what you could not verify (e.g. no cluster access, an issuer you could not inspect). Do not declare a deploy complete on the basis of `apply` succeeding alone.

## 9. Completion checklist

- [ ] Baseline state inspected; existing operator-pinned tags (especially `mcp`) preserved.
- [ ] All three out-of-band Secrets (`nervecenter-env`, `harbor-pull`, `nervecenter-oauth` if SSO) exist with current values; no real values committed.
- [ ] Every image tag being deployed is confirmed present in the registry.
- [ ] Resources applied in dependency order (or via `kubectl apply -k deployments/kubernetes`).
- [ ] Postgres `Ready`; NATS `Ready`; migration Job `1/1 Completed`.
- [ ] `api`, `mcp`, `web` Deployments report `1/1` (or expected replica count) `Ready`.
- [ ] `/api/health` and `/mcp/health` return healthy responses.
- [ ] If SSO is active: the public host redirects to Keycloak for anonymous visitors and serves the app for authenticated ones.
- [ ] Ingress has a valid TLS certificate and correct backend routing (`/mcp/` open; `/` via the gate).
- [ ] Pod logs and events show no crash-loop, restart spikes, or pending conditions.
- [ ] Any limitations or steps you could not run (no cluster access, unconfirmed registry tag) are clearly reported.

In the handoff, briefly describe what was deployed, the image tags used, the checks actually performed, and any remaining limitations. Deliver the confirmed, verified deploy state when implementation was requested; a plan or checklist alone is not completion.

---

## Installation and use — for the human user

Copy this file into the target VS Code project's `.roo/rules/` directory, for example as `.roo/rules/30-k8s-deploy.md`. Roo and Zoo support this rules location. Use the workspace actually opened in VS Code. These are Markdown instructions, not a JSON settings import. [Roo documentation](https://github.com/RooCodeInc/Roo-Code/blob/main/apps/docs/docs/features/custom-instructions.md) · [Zoo documentation](https://docs.zoocode.dev/features/custom-instructions)

Other supported locations:

- Code mode only: `.roo/rules-code/30-k8s-deploy.md` in the project (where this file already lives).
- All projects on macOS/Linux: `~/.roo/rules/30-k8s-deploy.md`.
- All projects on Windows: `%USERPROFILE%\.roo\rules\30-k8s-deploy.md`.

Choose one scope to avoid loading duplicate copies. Keep existing rules; add this as a separate file. A local VS Code installation and a remote workspace may use different filesystems; install the project rule in the workspace where the task runs. Start a fresh task after adding it.

Example request after installation:

> Deploy NerveCenter to the cluster using the Kubernetes deployment guidance. First inspect the existing state and confirm the out-of-band secrets and image tags. Then apply the resources in dependency order, run the migration Job, and verify each layer (Postgres, NATS, API, MCP, web, and SSO if active). Preserve the pinned mcp tag and keep all secrets out of version control. Report the verified health of each component and any checks you could not perform.
