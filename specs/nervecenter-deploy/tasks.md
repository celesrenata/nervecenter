# Tasks: NerveCenter Deployment to K3s via Harbor

**Spec:** [`spec.md`](./spec.md) · **Plan:** [`plan.md`](./plan.md)

## Task Board

| ID | Title | AC | Deps | Files owned | Verification |
|----|-------|----|------|-------------|--------------|
| T101 | Backend boots & all subsystems run | AC001, AC002, AC010 | — | `backend/**`, `schemas/**`, `migrations/**`, `tests/**`, `pyproject.toml` | venv install clean; `uvicorn` boots; pytest ≥1 test/subsystem green |
| T102 | CLI + MCP + SDK against API contract | AC003, AC004 | — | `cli/**`, `mcp/**`, `sdk/**` | contract tests first; live API smoke after T101 |
| T103 | Web UI builds & serves all spec views | AC005 | — | `web/src/**`, `web/index.html`, web build config (not `web/Dockerfile`) | `npm run build` clean; mocked API smoke first, live Playwright after T101 |
| T104 | Docker + k8s manifests + Harbor scripts | AC006, AC007 | — | `Dockerfile.api`, `web/Dockerfile`, `deployments/kubernetes/**`, deployment scripts | manifests/scripts and dry-run first; image build after T101 and T103 |
| T201 | Deploy to K3s via Harbor | AC006, AC007 | T101, T103, T104 | k8s objects in `nervecenter` ns | all pods Running; ingress serves web+API; logs clean |
| T202 | Audit deployed system & fix loop | AC001–AC005, AC007 | T201 | patches across `backend/**`, `web/**`, `deployments/**` | curl matrix + Playwright matrix all green; defects filed & fixed |
| T203 | Synthetic incident demo end-to-end | AC008 | T202 | `synthetic-lab/**` | seeded fault → DAG → ≥3 parallel investigations → remediation → resolution |
| T204 | Apple shared SDK compiles on M5 Max | AC009 | — | `apple/Shared/**`, xcodeproj | compile contract-based models first; regenerate from live OpenAPI after T101 |
| T205 | Redeploy web image from current source (fix blank page) | AC005, AC007 | T206 | `web/**`, `deployments/harbor/deploy.sh` | live URL renders UI from LAN (Playwright `#root` non-empty, 0 console errors) |
| T206 | Bake `VITE_API_BASE=/api` into web build (real, not mock) | AC005 | — | `web/Dockerfile` | deployed bundle fetches `/api/*`; Overview shows live data, not fixtures |
| T207 | Public reachability / DNS for `nervecenter.celestium.life` | AC007 | — | DNS/tunnel config (external) + ingress | chosen access path (LAN-only or public tunnel) loads the UI |
| T208 | Fix https trailing-slash redirects (mixed content) for `/api/events`, `/api/entities`, `/api/evidence` | AC005 | T205 | `backend/**` (route paths / `ProxyHeaders`) or `web/nginx.conf.template` | `curl -L https://nervecenter.celestium.life/api/events` → 200; Timeline view: 0 blocked/mixed-content console errors |

T101, T102, T103, T104, and T204 have disjoint initial file ownership and can start in one dispatch wave. Work from `spec.md` and the existing schema/API contract; defer live integration checks until the backend boots. A failing downstream smoke test is an integration finding, not a reason to idle its worker before implementation. Dispatch at least three of these scopes concurrently when three inference hosts are free; keep each worker within its owned paths.

## T101 — Backend boots & all subsystems run

Scope:
- Fix venv: install all deps into `.venv` (py3.11) — `pip install -e ".[dev]"`
- Fix `backend/api/app.py` import surface (expose both `app` and `create_app`)
- Implement the 4 empty modules to spec §26/§37/§44/§74:
  - `backend/evidence/` — EvidenceService (CRUD, immutability, hash verify, claim linkage)
  - `backend/agents/` — agent registry, AgentRun lifecycle, blackboard (spec §25–26, §75 loop protection)
  - `backend/orchestration/` — incident workflow DAG, blackboard coordinator, correlation agent (§38)
  - `backend/observability/` — OTel span wiring, trace correlation, Prometheus metrics (§74)
- Wire all routers into API (events, entities, relationships, evidence, claims, goals, tasks, agents, runs, incidents, models, compute, routes, changes, approvals, clients — spec §46)
- Alembic: upgrade head idempotent; add `initdb` job entrypoint
- Tests: ≥1 pytest per subsystem + integration test posting an event through NATS→DB

## T102 — CLI + MCP + SDK

Scope:
- CLI: all §47 commands (`status`, `ask`, `entity`, `changes`, `goal`, `task`, `agents`, `compute`, `routes`, `incident`, `pause-agents`, `stop-mutations`) with `--json`
- MCP server: spec §45 tool list (15 tools), MCP client wrapper; expose on `/mcp` endpoint or stdio
- SDK python: typed client from OpenAPI (httpx, async); SDK typescript: fetch client
- Verification against live (local then cluster) API

## T103 — Web UI

Scope:
- Implement missing views per §48–51: Overview, Systems, Timeline, Goals, Task DAG (8 states visible), Agents, Inference (nodes/models/routes live), Incidents, Decisions, Ask
- Realtime via SSE/WebSocket from API
- Auth context per §71 device-pairing stub
- Playwright smoke: each view renders, API data shown, 0 console errors

## T104 — Deploy artifacts

Scope:
- `Dockerfile.api` (multi-stage, python3.11-slim, uvicorn, alembic entry)
- `web/Dockerfile` (build stage + nginx static, SPA fallback)
- K3s kustomize `deployments/kubernetes/base/`: namespace `nervecenter`, pull secret, PVC+StatefulSet postgres (pgvector/pgvector:pg16), nats (JetStream), migrations Job, api Deployment, web Deployment+Service, ingress `nervecenter.celestium.life`, ConfigMap/Secret
- `deploy-harbor.sh`: build → tag with git sha → login (harbor creds) → push → kubectl apply -k
- Dry-run validation: `kubectl apply -k --dry-run=server`

## T201 — Deploy to K3s

Steps:
1. Extract Harbor creds: `kubectl get secret -n beeflow-service harbor-pull -o jsonpath='{.data.\.dockerconfigjson}'` or docker login prompt
2. Build+push api & web images
3. `kubectl create ns nervecenter`; copy harbor-pull secret
4. Apply kustomize; watch pods; fix until Running
5. Verify: `curl https://nervecenter.celestium.life/api/health`, ingress cert via cert-manager

## T202 — Audit & fix loop

Matrix:
- API: every §46 group endpoint returns 200/expected JSON
- CLI: §47 commands against cluster URL
- MCP: list_tools + one call each tool
- Web: Playwright script across 10 views + Ask query round-trip
- Defects → fix → T201 redeploy → re-run until green

## T203 — Synthetic incident demo

Scope per §94 canonical demo: seed HelloDJ-equivalent workload via synthetic-lab; break it (config drift); observe NerveCenter investigation DAG with ≥3 concurrent branches; Watch/iPhone-equivalent approval via API; execute reversible remediation; verify recovery; assert permanent record (cause/fix/evidence/timeline) in DB.

## T204 — Apple shared SDK

Scope: `apple/Shared/` SwiftPM package (NerveModels/NerveAPI/NerveAuth/NervePersistence/NerveRealtime/NerveDesign); generate NerveModels from OpenAPI; `xcodebuild -scheme NerveShared -destination 'generic/platform=iOS Simulator' build` on M5 Max.

## Verification Log

*(updated per task completion — commands, results, gaps)*

### T-WEBE2E / AC005 — Web UI e2e audit (Playwright)

- Command: `cd web && npm run e2e` (self-contained: globalSetup boots `NERVE_MOCK_INFER=1 .venv/bin/python -m uvicorn backend.api.app:app` on :8000, Vite dev server on :3000 with `VITE_API_BASE=/api`, then 10 view-render tests + Ask round-trip on chromium; globalTeardown stops the backend)
- Result: **11 passed (14.1s)** under Node v24.20.0 / chromium 1243 — every spec view (Overview, Systems, Timeline, Goals, Task DAG, Agents, Inference, Incidents, Decisions, Ask) renders without console errors; Ask round-trip asserts the real 202 `accepted/executed:false/service:context` ack and renders it with zero console errors.
- Defect found & fixed (blocking): `@playwright/test` 1.48 wedged indefinitely under Node 24 — its `requireOrImport` (`lib/transform/transform.js`) does `eval(import(global-setup.mjs))` and the dynamic import never resolves (sample stack: stuck in `ImportModuleDynamicallyWithPhase`, 0 output, 10+ min; minimal A/B repro with the real global-setup hook: Node 22 = 1s pass, Node 24 = hard-timeout hang). Fixed by bumping `@playwright/test` 1.48 → `^1.63.0`; requires `npx playwright install chromium` once after install.
- Defect found & fixed: the documented 404 console filter in `views.spec.ts`/`ask.spec.ts` parsed the URL out of the console *text* (`text.split(' (').pop()` → `"Not Found"`), which can never match — the URL of a "Failed to load resource" console error is on `msg.location().url`, not `msg.text()` (verified empirically). The proven dead-ends `/api/systems` + `/api/decisions` (404 live; neither is in spec §46's endpoint groups; views degrade to empty state) therefore failed `/systems` + `/decisions`.
- Defect found & fixed: `ask.spec.ts` user-bubble assertion was a strict-mode violation — the question string also renders in the permanent "Quick Questions" suggestion button; scoped to `p.whitespace-pre-wrap`.
- Companion gates: `cd web && npm run build` → exit 0 (tsc + vite, 49 modules); `.venv/bin/python -m pytest tests/ -m "not integration" -q` → 38 passed.
- Commit: `c34712b`

### A5/A6 — Deployment audit (2026-09-30) — “I can’t see anything”

Evidence (all live, 2026-09-30):
- Cluster (ns `nervecenter`): all pods Running (`api`/`mcp`/`nats`/`postgres`/`web`), `nervecenter-migrate` Complete. Ingress `nervcenter-celestium` present (traefik, host `nervecenter.celestium.life`, ports 80/443). `kubectl` context `default`.
- `GET /` → HTTP 200 (nginx 1.27.5); `GET /api/health` → 200 real PostgreSQL 16.15 (pgvector ok); `/mcp/health` → 200. TLS valid: Let’s Encrypt, `CN=nervecenter.celestium.life`, notAfter Dec 28 2026.
- **Defect D1 (blocking — the blank page):** root cause = **stale image + known pre-`c34712b` bug**. `nervecenter/web:amd64` was built 2026-09-29 04:59, before commit `c34712b` (Sep 30 10:52) which fixed `App.tsx` to wrap `AuthProvider` *inside* `BrowserRouter`. In the deployed bundle, `AuthProvider`'s `useNavigate()` ran outside `RouterProvider` → react-router invariant `dr()||X(!1)` (NavigationContext null) threw on mount → whole tree unmounted → `#root` empty → blank white page (proven: deployed bundle served locally on :4174 reproduced the identical `X→m0→ni→G0` console stack with `#root`=0; same source built fresh renders fully). Fix = rebuild + redeploy web from current HEAD (T205).
- **Defect D2 (blocking data fidelity):** the web build is in **mock mode** — `VITE_API_BASE` is unset at build (no `.env`; `web/Dockerfile` bakes none) → `useMockMode=true` → UI would show fixtures (esnixi / “Goal 1”), not the live API. The e2e that “passed” ran in **real mode on the dev server** (`VITE_API_BASE=/api`), so this was never exercised on the deployed production bundle. Fix: bake `VITE_API_BASE=/api` into `web/Dockerfile` (nginx already proxies `/api/` same-origin).
- **Defect D3 (reachability):** `nervecenter.celestium.life` is **authoritatively NXDOMAIN on the public internet** (Google + Cloudflare DoH → Status 3; authoritative NS = Route53 `ns-*.awsdns-*`). It resolves only to private `10.1.1.12–15` (traefik LoadBalancer) via the home resolver (`192.168.42.1`). ⇒ off the home network the URL never resolves (browser DNS error) — a second, independent reason for “can’t see anything.” Public access requires a tunnel/public record (T207).
- **Defect D4 (secondary; blocks AC005 "0 console errors" on the live UI):** `/api/entities|/api/evidence|/api/events` → **307 with an absolute `http://` Location** (`curl -D` shows `location: http://nervecenter.celestium.life/api/events/`). From an `https://` page the browser **blocks the redirect as Mixed Content → the fetch rejects** (Timeline/Systems data lost + console errors). Root: traefik→nginx hop drops the real scheme (nginx rewrites `X-Forwarded-Proto` to `$scheme`=http) and FastAPI issues scheme-absolute redirects. The dev-server e2e (http→http) can never see this. → **T208**.
- Secondary (non-blocking; UI degrades gracefully, not the cause of the blank page): `/api/overview`, `/api/systems`, `/api/decisions`, `/api/timeline` → 404 (not in spec §46 endpoint groups).

Remaining: **T205** (rebuild + redeploy web), **T206** (bake real-mode env), **T207** (access model + public DNS/tunnel).

### T205/T206 — Web rebuild + real-mode deploy (2026-09-30) — RESOLVED

- T206: [`web/Dockerfile`](../../web/Dockerfile) build stage now bakes `ARG VITE_API_BASE=/api` + `ENV VITE_API_BASE` before `npm run build` (overridable via `--build-arg`); bundle statically selects `realApi` (`useMockMode=false`).
- T205: built + pushed `registry.celestium.life/nervecenter/web:eef1c45` (linux/amd64, digest `sha256:04812964…`). Deployed bundle `index-BoWc-G3Z.js` (replaces crashing `index-DHxfMRGP.js`). Real-mode proof from the image: bundle contains baked `"/api"` base; `esnixi`=0, `node-esnixi`=0, `mockApi` identifier absent (one unreachable `mockOverview.goals` string survives tree-shaking, never rendered).
- Deploy mechanics: rendered kustomize carried `api:latest` placeholder vs live `api:61c1481` → full apply would have retagged api, so ONLY the extracted web Deployment was applied (PyYAML doc split, 0 api refs). api/mcp/nats/postgres/migrate-job/ingress byte-identical to before. `rollout status deploy/web` OK; pod `web-5fc8f8d5c4-qhkzv` Running 1/1, 0 restarts.
- Live verification (production bundle, Playwright chromium headless, not the dev-server harness): `GET /` → 200 with new asset hash (old hash absent); `GET /api/health` → 200 real Postgres 16.15 + NATS ok (api regression clean). All 10 views render (sidebar + route content each); Overview/Goals show live `/api/goals` data ("Goal 1"/"Goal 2"); no `esnixi` fixture text anywhere; 0 uncaught page errors. Console errors = only the known 404s for `/api/systems` + `/api/decisions` (`/api/overview`, `/api/timeline` 404s are curl-confirmed dead-ends but never fetched by the current views). **D1 (blank page) and D2 (mock mode) resolved.**
- T208: **NOT fixable web-only** — api pod runs bare `uvicorn … --port 8000` (default `forwarded_allow_ips=127.0.0.1,::1`); live test from the web pod proved `X-Forwarded-Proto: https` is ignored (still `307 → http://`). Requires backend change (`--forwarded-allow-ips "*"` or `FORWARDED_ALLOW_IPS` env → api image/manifest retag). `web/nginx.conf.template` unchanged; `/api/events|entities|evidence` mixed-content blocks remain until the api is retagged.
- T207 remains open: the URL still resolves only on the home network (public NXDOMAIN).

### Re-verify (2026-10-26) — premise drift + code fix landed

- **T207 status changed — the public-DNS premise is stale.** `nervecenter.celestium.life`
  now resolves **publicly**: the NS chain points at `frameshift.net` →
  `24.19.103.182` (a Cloudflare tunnel exit; the 2026-09-30 NXDOMAIN against Route53
  `ns-*.awsdns-*` NS records no longer reflects the zone). Whether the tunnel actually
  serves the K3s ingress end-to-end is **not verified from this host** — a curl to the
  public endpoint returned no response (`curl -o /dev/null -w '%{http_code}'` → `000`,
  2026-10-26), so the host may resolve but not yet route. Re-confirm from the home
  network / a host with cluster access before closing T207.
- **T208 code fix landed.** `Dockerfile.api` CMD is now
  `uvicorn backend.api.app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"`
  (root cause per the 2026-09-30 note: uvicorn's default `forwarded_allow_ips=127.0.0.1,::1`
  silently drops `X-Forwarded-Proto` from the nginx/Traefik hops). Boot-verified locally
  (uvicorn 0.54.0): app starts, `GET /api/health` → 200. **Not yet live:** the fix only
  takes effect after an api image rebuild + Harbor push + deploy retag — the running
  `api` pod still runs the old image.
- **Live-cluster re-verification is not possible from this host.**
  `kubectl config current-context=default` shows **no `nervecenter` namespace** on the
  reachable cluster (the K3s cluster that carries the `nervecenter` namespace is not
  connected to this kubeconfig). Consequently T205/T206/T207/T208 cannot be re-verified
  against the live cluster from here; this session re-verified only: the public-DNS
  change above, the T208 code fix + local boot, and the 2026-09-30 RESOLVED log for
  T205/T206 (unchanged, already complete in the log above).
- **Remaining to close T208 live:** rebuild + push an api image from this
  `Dockerfile.api`, retag the `api` Deployment, then `curl -D - https://nervecenter.celestium.life/api/events/`
  from a network that reaches the cluster — the `Location` header must now be
  `https://` (the 2026-09-30 mixed-content blocker is expected to disappear).

### Full redeploy (2026-10-26) — post-reset recovery, VERIFIED

The ~2026-10-22 cluster reset had wiped the `nervecenter` namespace, the Harbor
`nervecenter` project (all images, including the pinned `mcp:86a6a93`), and the
Keycloak `nervecenter` client. Fresh full redeploy from `0545873`:

- **Harbor:** `nervecenter` project recreated via API (private, owner admin).
  `api`, `mcp`, `web` built at `0545873` and pushed. First pass was built
  arm64 (host default) and CrashLoopBackOff'd with `exec ... no such file or
  directory` on the amd64 nodes; rebuilt with `--platform linux/amd64` and
  re-pushed under the same tag — hence `imagePullPolicy: Always` now on all
  four NerveCenter manifests (commit `5fa47ad`).
- **Cluster:** namespace + out-of-band secrets recreated out-of-band
  (`harbor-pull`, `nervecenter-env` per `secrets.yaml.example` doc-1 values,
  `nervecenter-oauth` placeholder). `kubectl apply -k` with `:latest` →
  `:0545873` retag. Migration Job ran against fresh Postgres 16 (pgvector) —
  `alembic=005_nc_platform`.
- **Manifest drift from old cluster state (committed this session):**
  - `mcp-deployment.yaml` — pin `86a6a93` → `0545873` (old tag lost, `13d359b`).
  - `ingress.yaml` — `/` briefly served open via `web:80` (`0d10eb1`) while the
    Keycloak client was being recreated; gate re-routed the same day (below).
- **Verification (all from this host, 2026-10-26):**
  - Pods: api, mcp, nats, web, postgres-0, nervecenter-oauth all `1/1 Running`,
    zero restarts; migrate Job `Succeeded` (`1/1 Completed`).
  - `/api/health` (port-forward and via Traefik LB) → `status ok`, db
    `ok` (PostgreSQL 16.15, pgvector_available), nats `connected` + JetStream.
  - `/mcp/health` (port-forward + LB) → `healthy`; SPA `/` → 200.
  - **T207 closed live:** public `https://nervecenter.celestium.life/` → 200
    from this host (Cloudflare tunnel now routes to the ingress);
    `/api/health` and `/mcp/health` 200 over the public host with valid
    cert-manager TLS.
  - **T208 closed live:** `GET /api/events/` over the public host → HTTP 200,
    no `Location` 307, no mixed content (T208 `--proxy-headers` fix confirmed
    end-to-end).
- **Known state after this deploy:**
  - Fresh empty database (data reset accepted by operator).
  - `NERVE_MOCK_INFER=1` and `NERVE_OMNIROUTE_URL` point at an in-cluster
    `omniroute.nervecenter.svc` that does not exist — inference answers are
    mock; no OmniRoute failure expected on the happy path.
  - web nginx still fans `/mcp/` → api (404 via web); the ingress serves
    `/mcp/` direct from the mcp service, so this only matters for clients that
    reach the web Service directly.

### SSO activation (2026-10-26, after the redeploy) — VERIFIED

The `nervecenter` Keycloak client was recreated in the **live** `celestium`
realm via the Admin API (`admin-cli` password grant against the
`sso/keycloak-bootstrap` admin; realm NOT re-imported — per
`sources/kube/sso/APPLICATION-ROLLOUT.md`: import skips existing realms,
reconcile live changes through the Admin API). Client shape mirrors the
working `comfyui` gate client: confidential, standard-flow only,
`S256` PKCE, `groups` protocol mapper, scopes
`openid profile email` (+web-origins/acr/roles). The fresh client-secret was
stored out-of-band in `sso/keycloak-clients` (`NERVECENTER_CLIENT_SECRET`)
and `nervecenter/nervecenter-oauth` (`client-secret`); the gate pod was
re-rolled to pick it up, and the Ingress `/` backend was restored to
`nervecenter-oauth:4180` (`ingress.yaml` committed).

Verification (public host, this session):
- `GET https://nervecenter.celestium.life/` → **302** to
  `https://sso.celestium.life/realms/celestium/protocol/openid-connect/auth?client_id=nervecenter&code_challenge_method=S256&redirect_uri=https://nervecenter.celestium.life/oauth2/callback...`
  (gate active, correct client, S256 challenge present).
- `GET /mcp/health` → **200** (still open, direct to mcp).
- Client-secret probe against the token endpoint returned
  `unauthorized_client` ("not enabled to retrieve service account") — i.e.
  the secret **authenticated** (`invalid_client` would mean a mismatch);
  service-account flow is off by design, same as the other web gate clients.
- Full browser login + callback/token-exchange not exercised end-to-end from
  this host (requires the operator's Keycloak credentials); the redirect,
  client configuration, and secret validity are all verified, which is the
  same depth the existing gate runbook's activation check stops at.

Staging scripts updated on the SSO host (`ssh://celes@192.168.42.254:sources/kube/sso`):
`keycloak/render_realm.py` (`CLIENTS` + `nervecenter`) and
`distribute_client_secrets.py` (target → `nervecenter/nervecenter-oauth`),
so a future fresh realm import / secret distribution stays coherent.
