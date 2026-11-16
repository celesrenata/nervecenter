# NerveCenter Deployment

| Target | Where | How |
|---|---|---|
| K3s via Harbor | [`deployments/kubernetes/`](deployments/kubernetes/) | [`deployments/harbor/deploy.sh`](deployments/harbor/deploy.sh) |
| Local (docker compose) | [`deployments/local/`](deployments/local/) | [`deployments/local/deploy-local.sh`](deployments/local/deploy-local.sh) |

Images: `registry.celestium.life/nervecenter/api:TAG` (from
[`Dockerfile.api`](../Dockerfile.api), `python:3.11-slim`, non-root uvicorn on :8000)
and `registry.celestium.life/nervecenter/web:TAG` (node:20 build →
nginx:1.27-alpine SPA + `/api/` + `/mcp/` reverse proxy, [`web/Dockerfile`](../web/Dockerfile)).

## Prerequisites

- Running docker daemon (macOS: Docker Desktop). If the socket is missing the
  scripts print a clear message and exit — start Docker and re-run.
- `kubectl` configured for the K3s cluster (gremlin-1..4), with write access.
- Harbor credentials: `HARBOR_USER`/`HARBOR_PASS` env vars, **or** an existing
  per-namespace `harbor-pull` secret on the cluster (the script decodes the
  username from it; `docker login` then prompts for the password).
- The image must be built before a real deploy (T201). Do not run `deploy.sh`
  in CI without images.

## First deploy

1. **Create the app env secret** if it has not been created yet:

   ```bash
   cd deployments/kubernetes/base
   cp secrets.yaml.example secrets.yaml   # secrets.yaml is gitignored
   # edit base64 values for your real endpoints (see the header in the file)
   kubectl -n nervecenter apply -f secrets.yaml
   ```

   The secret carries **both** naming schemes: `NERVE_DB_URL`,
   `NERVE_NATS_URL`, `NERVE_OMNIROUTE_URL`, `NERVE_MOCK_INFER` (T104 deploy
   contract) and `NERVECENTER_DATABASE_URL`, `NERVECENTER_NATS_URL`,
   `NERVECENTER_MOCK_INFER` (what the backend reads today —
   [`backend/persistence/connection.py`](../backend/persistence/connection.py),
   [`backend/events/nats_client.py`](../backend/events/nats_client.py)).
   `envFrom` injects every key into the api & migrate containers.

   Optional agent secrets (NS-ADR-111/113): `nervecenter-omniroute-privileged` (key
   `key`, Skill Hub catalog and read-only OmniRoute tools) and `nervecenter-mcp-hub`
   (key `token`, a copy of the mcp-hub token made without printing it; see
   `secrets.yaml.example`). Without them agents run with no skills and no tools.
2. **Run the deploy** (builds, pushes, ensures ns + pulls the harbor-pull
   secret, re-runs the alembic migration Job, applies the kustomize base
   retagged with the build, waits for readiness):

   ```bash
   ./deployments/harbor/deploy.sh
   ```

   Image tag = `git rev-parse --short HEAD` (override with `TAG=...`). The
   kustomize `--image` transformer rewrites the `:latest` placeholder in the
   api/web/migrate objects to the build tag before apply.

3. **Verify** (the script already probes in-pod; manual checks):

   ```bash
   curl -fsS https://nervecenter.celestium.life/api/health
   curl -fsSI https://nervecenter.celestium.life/ | head -1
   kubectl -n nervecenter get pods,svc,ingress
   ```

### Ingress host & TLS

- Host: `nervecenter.celestium.life` — `/` → web (nginx SPA), `/api/` → api,
  `/mcp/` → api ([`ingress.yaml`](kubernetes/base/ingress.yaml)).
- `ingressClassName: traefik`; TLS is issued by cert-manager via
  `cert-manager.io/cluster-issuer: celestium-le-production` into secret
  `nervecenter-celestium-tls` (mirrors the beeflow/comfyui pattern).
- Point DNS at the same Traefik load-balancer IPs the other hosts use; cert
  is provisioned automatically once DNS resolves.

## Upgrades

```bash
./deployments/harbor/deploy.sh        # new commit → new sha tag
```

The script deletes + re-applies the `nervecenter-migrate` Job so migrations
run on the new image before the api/web roll. `alembic upgrade head` is
idempotent (no-op when the schema is at head). PVC data (`postgres-data`,
longhorn) survives rollouts.

## Rollback

Images are SHA-tagged and remain in Harbor, so rollback is re-pointing:

```bash
PREVSHA=<previous short sha>
kubectl -n nervecenter set image deployment/api api=registry.celestium.life/nervecenter/api:${PREVSHA}
kubectl -n nervecenter set image deployment/web web=registry.celestium.life/nervecenter/web:${PREVSHA}
# re-run migrations on the previous image (idempotent)
kubectl -n nervecenter delete job nervecenter-migrate
kubectl -n nervecenter apply -f deployments/kubernetes/base/migrate-job.yaml \
  --image registry.celestium.life/nervecenter/api:latest=${PREVSHA}
kubectl -n nervecenter wait --for=condition=Complete job/nervecenter-migrate --timeout=300s
```

`kubectl -n nervecenter rollout undo deployment/api` also works (single
replica), but the SHA re-point above is the deterministic path (it moves the
migration Job to the matching image too). Schema downgrades are manual
(alembic downgrades are not run by this pipeline) — coordinate with T101
before rolling back across a migration.

## Copying the Harbor pull secret per namespace

Every namespace carries its own `harbor-pull`
(`kubernetes.io/dockerconfigjson`). `deploy.sh` copies it automatically when
`nervecenter` lacks the secret. By hand:

```bash
# source secret from any ns that already has the same registry auth
kubectl -n beeflow-service get secret harbor-pull -o jsonpath='{.data.\.dockerconfigjson}' \
  | base64 -d > /tmp/harbor-pull.json
kubectl -n nervecenter delete secret harbor-pull --ignore-not-found
kubectl -n nervecenter create secret generic harbor-pull \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson=/tmp/harbor-pull.json
```

Verify: `kubectl -n nervecenter get secret harbor-pull -o jsonpath='{.type}'`
→ `kubernetes.io/dockerconfigjson`.
[`harbor-pull.example.yaml`](kubernetes/base/harbor-pull.example.yaml) in
`base/` is a template only (placeholder body — never apply it).

## Local development

```bash
./deployments/local/deploy-local.sh          # build images + up -d --wait
./deployments/local/deploy-local.sh logs     # tail logs
./deployments/local/deploy-local.sh down     # tear down
```

Stack (see [`docker-compose.yaml`](local/docker-compose.yaml)):
`postgres` (pgvector/pgvector:pg16, :5432), `nats` (:4222/:8222 JetStream),
`migrate` (one-shot `python -m backend.persistence.migrate upgrade`),
`api` (uvicorn :8000), `web` (nginx :8080, proxies `/api/` + `/mcp/` to `api`).

## SSO (Keycloak)

The public site (`nervecenter.celestium.life`) is protected by Keycloak SSO at
`sso.celestium.life` (realm `celestium`, issuer
`https://sso.celestium.life/realms/celestium`). The cluster uses an **oauth2-proxy
deployment** as the front-door gate (mirrors the existing `comfyui` SSO).

Files:

- [`oauth-deployment.yaml`](kubernetes/base/oauth-deployment.yaml) — oauth2-proxy
  deployment (fronts `web:80`, redirects unauthenticated users to Keycloak)
- [`oauth-service.yaml`](kubernetes/base/oauth-service.yaml) — oauth2-proxy
  ClusterIP service on port 4180
- [`ingress.yaml`](kubernetes/base/ingress.yaml) — `/` → `nervecenter-oauth:4180`;
  `/mcp/` → `mcp:8000` (direct, open; the SPA never calls it)

`/mcp/` is intentionally open: the web nginx currently proxies `/mcp/` → `api`
(404s); routing it through the gate requires rebuilding the web image to fan
out `/mcp/` → `mcp`. That's a separate task.

### Activation (order matters — do NOT apply the ingress before steps 1+2)

The manifests in this directory are the **intended end state**: with the
Keycloak client existing and the secret present, `kubectl apply -k
deployments/kubernetes` puts the site behind the gate. Until then, the
in-cluster ingress still routes `/` → `web` directly (open), so applying the
full kustomization prematurely would send every visitor to a Keycloak
"unknown client" error page.

> **Admin access:** creating the client requires a user with the `realm-admin`
> role in realm `celestium`. NOTE (2026-10-10): the in-cluster
> `sso/keycloak-bootstrap` secret (user `celes`) is **stale** — `celes` is a
> regular user in realm `celestium` (password login OK) with no admin roles,
> and the token it yields gets `403` on `/admin`. Fix the bootstrap secret
> (or log in to the Keycloak admin console with a real admin) before step 1.
> The realm's only current realm-admin is `celes-admin` in the `master` realm
> (different credentials, not stored in-cluster as far as we could find).

1. **Create the Keycloak client** (from a node that can reach the cluster,
   using the admin API inside the sso namespace — never print the secret):

   ```bash
   kubectl -n sso exec -it deploy/keycloak -- kcadm.sh create client -r celestium \
     -s clientId=nervecenter \
     -s enabled=true \
     -s protocol=openid-connect \
     -s standardFlowEnabled=true \
     -s directAccessGrantsEnabled=false \
     -s publicClient=false \
     -s "redirectUris[0]=https://nervecenter.celestium.life/oauth2/callback" \
     -s "webOrigins[0]=https://nervecenter.celestium.life"
   ```

   Then set its confidential client secret (keep it out of logs):

   ```bash
   UUID=$(kubectl -n sso exec deploy/keycloak -- kcadm.sh get clients -r celestium \
     -o json | python3 -c 'import json,sys; print([c["id"] for c in json.load(sys.stdin) if c["clientId"]=="nervecenter"][0])')
   SECRET=$(openssl rand -hex 24)
   kubectl -n sso exec deploy/keycloak -- kcadm.sh update -r celestium -g "$UUID" -s "secret=$SECRET"
   # (copy $SECRET out-of-band into step 2; never commit it)
   ```

2. **Store the client secret in-cluster** (ns `nervecenter`):

   ```bash
   kubectl -n nervecenter create secret generic nervecenter-oauth \
     --from-literal=client-secret="$SECRET" \
     --from-literal=cookie-secret="$(openssl rand -hex 16)"
   ```

3. **Activate SSO** (re-point the ingress at the gate):

   ```bash
   kubectl apply -k deployments/kubernetes --dry-run=server   # check
   kubectl apply -k deployments/kubernetes                    # switch
   ```

4. **Verify**:

   ```bash
   # unauthenticated: gate 302s to the Keycloak authorization endpoint
   curl -sI https://nervecenter.celestium.life/ | grep -E 'HTTP|location'
   #   location: /oauth2/auth?client_id=nervecenter&response_type=code&redirect_uri=https%3A%2F%2Fnervecenter.celestium.life%2Foauth2%2Fcallback...
   # that authorization URL must serve the Keycloak login page (200)
   curl -fsSI 'https://sso.celestium.life/realms/celestium/protocol/openid-connect/auth?client_id=nervecenter&response_type=code&redirect_uri=https%3A%2F%2Fnervecenter.celestium.life%2Foauth2%2Fcallback&scope=openid' | head -1
   # /mcp/ stays open by design
   curl -s -o /dev/null -w '%{http_code}\n' https://nervecenter.celestium.life/mcp/health
   ```

### Troubleshooting

- `ImagePullBackOff` / `ErrImagePull` — `harbor-pull` secret missing or push
  failed; re-run `deploy.sh` (re-copies the secret) or fix the Harbor push.
- migrate Job failing — `kubectl -n nervecenter logs job/nervecenter-migrate`
  (usually DB URL in `nervecenter-env`, or postgres not up); the Job has
  `backoffLimit: 5`, `activeDeadlineSeconds: 120`.
- api pod not Ready — readiness is `GET /api/health` (:8000); inspect
  `kubectl -n nervecenter logs deployment/api`.
- ingress 404 — confirm the DNS A record points at the Traefik load-balancer
  IPs (compare `kubectl get ingress -n beeflow-service` addresses) and the
  cert is Ready: `kubectl -n nervecenter get certificates

## Skill Hub upload (NS-ADR-112, owner action only)

`optional/skillhub-upload/` holds the Job that writes the NerveCenter agent skills to
the `nervecenter/` folder of the Skill Hub NFS share. It is not in the base kustomization,
so `deploy.sh` never runs it. Use `python -m backend.owner_tools.skillhub_upload`
(`plan`, then `apply --approved`, `refresh`, `verify`); see that folder's README.

## Default-deny API and the mcp credential (NS-ADR-122)

Every api route needs a signed-in person (SSO, NerveCenter session or personal API
token) unless it is on the allowlist in `backend/api/public_routes.py`.

- **mcp pod**: the api accepts the scoped service credential `NERVE_INTERNAL_SERVICE_TOKENS`
  (`mcp:<token>`, built from Secret `nervecenter-internal-mcp` key `api-token`) only on the
  paths in `mcp/tools_module.py`. The mcp pod sends it as `NERVE_MCP_API_TOKEN`, and
  requires `Authorization: Bearer` on `/mcp` and `/mcp/` (`NERVE_MCP_SERVER_TOKENS`, Secret
  key `server-tokens`; `GET /mcp/health` stays open).
  **Deploy both together**: the api change alone breaks the mcp tools (401), and the mcp
  bearer alone has nothing to send. Create the Secret first (`secrets.yaml.example`).
- **Post-deploy check**: `deploy.sh` runs `deployments/harbor/check-route-auth.py` inside the
  api pod. It calls every operation without a credential and fails the deploy when a
  non-allowlisted route answers anything but 401/403. Local run (no Postgres, no 8001
  listener): start the app, then
  `NERVE_CHECK_BASE=http://127.0.0.1:8000 NERVE_CHECK_LOCAL=1 python deployments/harbor/check-route-auth.py`.

### Sessions and personal API tokens (NS-ADR-123, NS-ADR-124)

- **Session settings** (configmap, all optional): `NERVE_ACCESS_TOKEN_TTL_MINUTES` (60),
  `NERVE_SESSION_IDLE_HOURS` (24, sliding), `NERVE_SESSION_MAX_DAYS` (30),
  `NERVE_REFRESH_REUSE_GRACE_SECONDS` (10) and `NERVE_COOKIE_SECURE` (1; set 0 only for
  plain-http local development). The web signs in once through SSO and then refreshes
  silently; closing the browser does not end the session within the idle window.
- **Migration 037_sessions_pat** adds two tables (`auth_refresh_sessions`,
  `personal_api_tokens`) and alters none. Take the usual `pg_dump` before deploying anyway.
- **Personal API tokens**: web app, Settings, Access tab. Create a token, copy it once, then
  `curl -H "Authorization: Bearer nct_..." https://<host>/api/auth/me`. The `nerve` CLI reads
  it from `NERVE_TOKEN`. Revoke it in the same tab. Tokens are hashed at rest and cannot
  create or revoke tokens.

### Owner "how to test" list

1. After deploy, `deploy.sh` runs the route-auth check. To run it by hand:
   `kubectl -n nervecenter exec -i <api pod> -c api -- python - < deployments/harbor/check-route-auth.py`
   prints 0 open routes outside the allowlist.
2. Device paths still work: a relay POST with a bad bearer answers 401 from the app;
   opencode is still connected to `nervecenter-infra`; tunnel-client is Ready.
3. Sign in through SSO, leave the tab for 20 minutes, click around: no login prompt. Close the
   browser and reopen it the next morning (within 24 h): still signed in.
4. Settings, Access: create a token, copy it, run the `curl` above (200); revoke it and run it
   again (401).
5. Settings, Access, "Sign out everywhere": every browser session of your account ends.
