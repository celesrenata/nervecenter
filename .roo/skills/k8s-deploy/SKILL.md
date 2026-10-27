---
name: k8s-deploy
description: Deploy, upgrade, verify, or debug NerveCenter on the cluster (namespace `nervecenter`, manifests under `deployments/kubernetes/`). Use when applying kustomize resources, running the migration Job, managing the out-of-band Secrets, building the Harbor pull secret, activating the Keycloak SSO gate, or troubleshooting the deployed Postgres/NATS/api/mcp/web stack.
---

# NerveCenter Kubernetes deployment

Act as a platform operator who knows this cluster's conventions: one fixed namespace, Harbor for images, Traefik + cert-manager for ingress/TLS, and an oauth2-proxy gate fronting Keycloak SSO. Work within the existing manifests and the cluster's established patterns — do not invent new infrastructure. Preserve the invariants below, keep secrets out of version control, and verify every step before declaring a deploy complete. Treat `kubectl` output and pod logs as evidence; a successful `apply` does not prove the service is healthy.

Full reference: `docs/DEPLOYMENT.md`. Manifests: `deployments/kubernetes/base/` (kustomize entrypoint `deployments/kubernetes/kustomization.yaml`).

## 1. Invariants (do not change without a deliberate decision)

1. **Namespace is fixed to `nervecenter`.** Every in-cluster DNS name (`postgres.nervecenter.svc`, `nats.nervecenter.svc`, `api.nervecenter.svc`, `mcp.nervecenter.svc`) and the Ingress TLS host assume it. Renaming breaks the Secret values and the web proxy config.
2. **Secrets live out-of-band.** `nervecenter-env`, `harbor-pull`, and `nervecenter-oauth` are intentionally NOT listed in the kustomization. The operator creates/copies them; workloads reference them via `envFrom` / `imagePullSecrets` / `secretKeyRef`, resolved at pod runtime. Never commit real secret values.
3. **The `mcp` Deployment is pinned to a running tag** (`registry.celestium.life/nervecenter/mcp:86a6a93`), not `:latest`. A blind retag to `:latest` causes `ImagePullBackOff` (no such tag in Harbor). Before retagging mcp, confirm the tag exists in the registry.
4. **SSO gate is inert until** the Keycloak `nervecenter` client exists in realm `celestium` and its real `client-secret` + fresh `cookie-secret` are in the `nervecenter-oauth` Secret. The Ingress already routes `/` to `nervecenter-oauth:4180`, so the gate activates the moment both preconditions are met. `/mcp/` is intentionally open (direct to the mcp service).
5. **Postgres defaults to `nervcenter:nervcenter` / db `nervcenter`**, matching `DEFAULT_DATABASE_URL` in `backend/persistence/connection.py`. The `nervecenter-env` Secret carries both `NERVE_*` (deploy contract) and `NERVECENTER_*` (backend runtime) keys; both are injected so the app works before the env-var names are normalized.

## 2. Topology at a glance

| Component | Kind | Image | Service (port) | Probe |
| --- | --- | --- | --- | --- |
| `postgres` | StatefulSet | `pgvector/pgvector:pg16` | headless `postgres:5432` | `pg_isready -U nervcenter -d nervcenter` |
| `nats` | Deployment | `nats:2.10-alpine` (`--jetstream --http_port 8222`) | `nats:4222` | `GET /varz` :8222 |
| `nervecenter-migrate` | Job | same image as `api` | — | Job `1/1 Completed` |
| `api` | Deployment | `.../nervecenter/api:TAG` | `api:8000` | `GET /api/health` :8000 |
| `mcp` | Deployment | `.../nervecenter/mcp:86a6a93` (pinned) | `mcp:8000` | `GET /mcp/health` :8000 |
| `web` | Deployment | `.../nervecenter/web:TAG` (nginx) | `web:80` | `GET /` :80 |
| `nervecenter-oauth` | Deployment | `quay.io/oauth2-proxy/oauth2-proxy:v7.15.4` | `nervecenter-oauth:4180` | `GET /ready` :4180 |

Routing: Ingress (`nervecenter.celestium.life`) → `/mcp/` → `mcp:8000` (open); `/` → `nervecenter-oauth:4180` → `web:80` → (SPA static | `/api/` → `api:8000`). TLS is automatic via the `cert-manager.io/cluster-issuer: celestium-le-production` annotation. Config comes from the `nervcenter-config` ConfigMap (`API_PORT=8000`, `API_UPSTREAM=api.nervecenter.svc`, `NERVE_MCP_API=http://api:8000`); the web nginx uses `API_UPSTREAM`/`API_PORT` to proxy `/api/` and `/mcp/` in-cluster.

## 3. Inspect before you change

```bash
kubectl -n nervecenter get all
kubectl -n nervecenter get deploy,job -o wide
kubectl -n nervecenter get secret    # confirm the three out-of-band secrets exist
kubectl -n nervecenter get deploy mcp -o jsonpath='{.spec.template.spec.containers[0].image}'
```

Identify what is running and the current image tags. Preserve operator-pinned tags (especially `mcp`) and any live custom values. A redeploy converges the cluster to the manifests — it must not roll a pinned image back to a stale placeholder. If you cannot read the cluster (no kubeconfig / network), say so and stop; never fabricate the state of a running system.

## 4. Pre-deploy checks

- **Registry access:** the `harbor-pull` Secret must exist in `nervecenter`. Canonical way — copy the existing per-namespace secret (never hard-code credentials):
  ```bash
  kubectl get secret -n beeflow-service harbor-pull -o yaml \
    | sed -e '/^  namespace:/d' | kubectl apply -n nervecenter -f -
  ```
  `deployments/harbor/deploy.sh` performs this copy automatically when the namespace lacks the secret.
- **Image tags exist in Harbor** before deploying. For `api`/`web`, `deployments/harbor/deploy.sh` patches the `:latest` placeholder with the git short SHA before `apply -k` — confirm the tag was built and pushed.
- **Secret values current:** if Postgres/NATS/OmniRoute URLs changed, re-encode the base64 values in a local `secrets.yaml` (copied from `secrets.yaml.example`, which is gitignored) and re-apply. Never edit the committed example's placeholders.
- **Storage:** Postgres PVC needs `storageClassName: longhorn`, 10Gi, `ReadWriteOnce`. Confirm the StorageClass has capacity before creating the StatefulSet.

## 5. Deploy in dependency order

For first deploys (or any deploy that ships schema changes), respect this order:

1. Namespace → ConfigMap
2. Postgres: PVC → StatefulSet → Service
3. NATS: Deployment → Service
4. Out-of-band Secrets: `nervecenter-env`, `harbor-pull`, and `nervecenter-oauth` (if enabling SSO)
5. **Migration Job `nervecenter-migrate` — run BEFORE rolling the api Deployment**
6. `api` Deployment → Service
7. `mcp` Deployment → Service
8. `web` Deployment → Service
9. SSO gate: `nervecenter-oauth` Service → Deployment
10. Ingress

Single-command path: `kubectl apply -k deployments/kubernetes` (identical to `apply -k deployments/kubernetes/base`; the top-level kustomization is a pass-through to `base/`).

**Ordering rule:** the API must not come up against an un-migrated schema. Alembic is idempotent, so re-apply the Job on every upgrade and confirm it completes before (or immediately after) the API rollout.

Wait for dependencies before proceeding:

```bash
kubectl -n nervecenter wait --for=condition=Ready pod -l app=postgres --timeout=180s
kubectl -n nervecenter wait --for=condition=Ready pod -l app=nats --timeout=120s
kubectl -n nervecenter wait --for=condition=complete job/nervecenter-migrate --timeout=180s
```

## 6. SSO (Keycloak) activation

Preconditions: (1) Keycloak client `nervecenter` exists in realm `celestium` at `https://sso.celestium.life/realms/celestium`; (2) its real `client-secret` + fresh `cookie-secret` are in the `nervecenter-oauth` Secret.

```bash
cp deployments/kubernetes/base/secrets.yaml.example secrets.yaml
openssl rand -hex 16   # 32 hex chars = 16 bytes (AES-valid) -> cookie-secret
# edit the nervecenter-oauth block in secrets.yaml with real values, then:
kubectl -n nervecenter apply -f secrets.yaml
```

Once the Secret is live, the gate activates (Ingress already points at it). Anonymous visitors are 302-redirected to Keycloak; only authenticated users reach the app.

Gaps to preserve, not "fix":
- **No `--allowed-group`:** no `nervecenter-users` group exists in the realm yet; adding a non-existent group locks every user out. When the group is created, add `--allowed-group=nervecenter-users` and re-roll.
- **No `--pass-access-token`:** the API validates NerveCenter-minted HS256 tokens (`backend/api/auth_service.py verify_token`); a raw Keycloak RS256 token is not accepted. Forwarding the identity token into the API is a separate, explicit follow-up — do not enable it casually.

## 7. Verify after deploy

A successful `apply` is not proof of health. Confirm each layer:

```bash
# Postgres reachable, DB exists
kubectl -n nervecenter exec -it deploy/postgres -- psql -U nervcenter -d nervcenter -c "SELECT current_database();"
# NATS / JetStream live
kubectl -n nervecenter exec deploy/nats -- wget -qO- http://127.0.0.1:8222/varz
# API + MCP health
kubectl -n nervecenter port-forward svc/api 8000:8000 &
curl -fsS http://localhost:8000/api/health
# SSO gate on the public host (expect 302 to Keycloak when SSO is active)
curl -is https://nervecenter.celestium.life/ | head
# Pod health / recent logs
kubectl -n nervecenter get pods -o wide
kubectl -n nervecenter logs deploy/api --tail=100
# Ingress + TLS cert
