#!/usr/bin/env bash
# T104 — NerveCenter Harbor build + push + deploy (AC006/AC007).
#
#   1. tag      — git short SHA (fallback: latest)
#   2. login    — HARBOR_USER/HARBOR_PASS, or derive username from an existing
#                cluster harbor-pull secret (beeflow-service / harbor-service)
#   3. build    — Dockerfile.api (repo root) + web/Dockerfile -> local images
#   4. push     — registry.celestium.life/nervecenter/{api,web}:TAG
#   5. secrets  — ensure ns nervecenter exists; copy harbor-pull into the ns
#   6. apply    — render kustomize to yaml, sed-retag :latest -> TAG, apply (amd64)
#   7. wait     — migration Job Complete, then workloads Available/Ready
#   8. probe    — in-pod /api/health (python stdlib) + web index (wget)
#   9. print    — ingress URL https://nervecenter.celestium.life
#
# Usage:
#   ./deployments/harbor/deploy.sh            # tag = git short SHA (fallback: latest)
#   TAG=1.2.3 ./deployments/harbor/deploy.sh  # explicit tag
#
# Requires: running docker daemon, kubectl with cluster access, git, python3.
set -euo pipefail

REGISTRY="registry.celestium.life"
API_IMAGE="${REGISTRY}/nervecenter/api"
WEB_IMAGE="${REGISTRY}/nervecenter/web"
NS="nervecenter"
KUSTOMIZE_DIR="deployments/kubernetes"
INGRESS_HOST="nervecenter.celestium.life"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

log()  { printf '\033[1;34m[nervcenter-deploy]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[nervcenter-deploy] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. tooling -------------------------------------------------------------
command -v docker  >/dev/null 2>&1 || fail "docker not found on PATH"
command -v kubectl >/dev/null 2>&1 || fail "kubectl not found on PATH"
command -v python3 >/dev/null 2>&1 || fail "python3 not found on PATH"
if ! docker info >/dev/null 2>&1; then
  fail "docker daemon is not reachable.
  - macOS: open 'Docker Desktop' and wait for the whale to turn green.
  - Linux: 'sudo systemctl start docker'
  Then re-run this script."
fi

# --- 2. tag -------------------------------------------------------------------
TAG="${TAG:-}"
if [[ -z "${TAG}" ]]; then
  if git rev-parse --short HEAD >/dev/null 2>&1; then
    TAG="$(git rev-parse --short HEAD)"
  else
    TAG="latest"
    log "git short SHA unavailable — falling back to tag 'latest'"
  fi
fi
API_TAG="${API_IMAGE}:${TAG}"
WEB_TAG="${WEB_IMAGE}:${TAG}"
log "tag: ${TAG} (api=${API_TAG}, web=${WEB_TAG})"

# --- 3. harbor login ----------------------------------------------------------
if [[ -n "${HARBOR_USER:-}" && -n "${HARBOR_PASS:-}" ]]; then
  log "docker login ${REGISTRY} via HARBOR_USER/HARBOR_PASS"
  printf '%s' "${HARBOR_PASS}" | docker login "${REGISTRY}" -u "${HARBOR_USER}" --password-stdin
else
  log "HARBOR_USER/HARBOR_PASS not set — deriving username from an existing cluster harbor-pull secret"
  SRC_NS=""
  for ns in beeflow-service harbor-service; do
    if kubectl -n "${ns}" get secret harbor-pull >/dev/null 2>&1; then
      SRC_NS="${ns}"
      break
    fi
  done
  [[ -n "${SRC_NS}" ]] || fail "no harbor-pull secret in beeflow-service or harbor-service; export HARBOR_USER and HARBOR_PASS instead"
  HARBOR_USER="$(kubectl -n "${SRC_NS}" get secret harbor-pull -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["auths"]["'"${REGISTRY}"'"]["username"])' 2>/dev/null)" \
    || fail "could not decode username from ${SRC_NS}/harbor-pull"
  log "using harbor user '${HARBOR_USER}' from ${SRC_NS}/harbor-pull (password prompted interactively)"
  docker login "${REGISTRY}" -u "${HARBOR_USER}"
fi

# --- 4. build + push -----------------------------------------------------------
log "building ${API_TAG} (linux/amd64)"
docker build --platform linux/amd64 -f Dockerfile.api -t "${API_TAG}" .
log "building ${WEB_TAG} (linux/amd64)"
docker build --platform linux/amd64 -f web/Dockerfile -t "${WEB_TAG}" web/
# Optional self-host app artifacts (web/dist/downloads) are baked in when
# staged; without them /downloads/ simply 404s (mount a volume at
# /usr/share/nginx/downloads to override). See web/README.md + web/verify-downloads-packaging.sh.
if [[ -d web/dist/downloads ]]; then
  log "web/dist/downloads staged — /downloads/ artifacts baked into ${WEB_TAG}"
else
  log "web/dist/downloads absent — /downloads/ will 404"
fi
log "pushing images"
docker push "${API_TAG}"
docker push "${WEB_TAG}"

# --- 5. namespace + per-namespace pull secret ------------------------------------
log "ensuring namespace ${NS}"
kubectl get namespace "${NS}" >/dev/null 2>&1 || kubectl create namespace "${NS}"

if ! kubectl -n "${NS}" get secret harbor-pull >/dev/null 2>&1; then
  log "copying harbor-pull from ${SRC_NS:-beeflow-service} into ${NS}"
  tmp="$(mktemp)"
  trap 'rm -f "${tmp}"' EXIT
  kubectl -n "${SRC_NS:-beeflow-service}" get secret harbor-pull -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d > "${tmp}"
  kubectl -n "${NS}" create secret generic harbor-pull \
    --type=kubernetes.io/dockerconfigjson \
    --from-file=".dockerconfigjson=${tmp}"
  log "harbor-pull created in ${NS}"
fi

# --- 6. apply manifests (image retag to build tag) ---------------------------------
# Base manifests carry the :latest placeholder. `kubectl apply` has no --image
# flag (that invocation fails with "unknown flag: --image"). To retag, we render
# the full kustomize tree to yaml, rewrite the api/web :latest tags to the build
# tag with sed (portable to BSD/macOS sed — no in-place -i), and apply the
# rendered file. This rewrites every occurrence (api Deployment, migrate Job,
# web Deployment).
#
# IMPORTANT — the render MUST use `kubectl kustomize`, NOT `kubectl apply -k -o yaml`.
# `kubectl apply` is a *live* apply even with `-o yaml`: it writes to the cluster
# first (retagging the RUNNING api/web to the placeholder :latest -> ImagePullBackOff
# and re-rolling the Deployments) and only then prints the YAML to stdout.
# `kubectl kustomize` is the pure, no-side-effect render that produces the identical
# manifest set, so the single `kubectl apply -f` below is the only cluster write.
# Delete the old Job first so the migration re-runs on the new image
# (alembic upgrade head is idempotent — no-op when the schema is at head).
log "re-running migration job + applying kustomize base (image retag -> ${TAG})"
kubectl -n "${NS}" delete job nervecenter-migrate --ignore-not-found=true
kubectl kustomize "${KUSTOMIZE_DIR}" \
  | sed "s|${API_IMAGE}:latest|${API_IMAGE}:${TAG}|g; s|${WEB_IMAGE}:latest|${WEB_IMAGE}:${TAG}|g" \
  > /tmp/rendered.yaml
kubectl apply -f /tmp/rendered.yaml

# --- 7. wait for workloads ------------------------------------------------------
# Migration Job completes first (schema at head before the api is useful).
log "waiting for job/nervecenter-migrate Complete (timeout 300s)"
kubectl -n "${NS}" wait --for=condition=Complete job/nervecenter-migrate --timeout=300s
# NOTE: the postgres StatefulSet is intentionally NOT waited on with
# --for=condition=Available — some K3s versions (observed: v1.36.4+k3s1) never
# populate a StatefulSet's status.conditions, so that wait times out on a
# fully-healthy pod. Deployment Available works fine; the pod-level Ready
# waits below already cover postgres.
for kindname in deployment/api deployment/web deployment/nats; do
  log "waiting for ${kindname} Available (timeout 300s)"
  kubectl -n "${NS}" wait --for=condition=Available "${kindname}" --timeout=300s
done
log "waiting for pods Ready"
kubectl -n "${NS}" wait --for=condition=Ready pod --selector=app=api      --timeout=300s
kubectl -n "${NS}" wait --for=condition=Ready pod --selector=app=web      --timeout=300s
kubectl -n "${NS}" wait --for=condition=Ready pod --selector=app=postgres --timeout=300s
kubectl -n "${NS}" wait --for=condition=Ready pod --selector=app=nats     --timeout=300s

# --- 8. in-pod health probes -----------------------------------------------------
# python:slim has no curl — use the python stdlib. nginx:alpine has busybox wget.
wait_pod() { # $1=label selector  $2=max seconds
  local pod="" i
  for i in $(seq 1 $(( $2 / 5 ))); do
    pod="$(kubectl -n "${NS}" get pod -l "$1" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    [[ -n "${pod}" ]] && break
    sleep 5
  done
  [[ -n "${pod}" ]] || fail "no pod found for selector $1"
  echo "${pod}"
}
api_pod="$(wait_pod "app=api" 60)"
web_pod="$(wait_pod "app=web" 60)"
kubectl -n "${NS}" exec "${api_pod}" -- \
  python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=5)' >/dev/null \
  || fail "api pod ${api_pod} failed /api/health probe"
log "api in-pod probe OK: ${api_pod}"
kubectl -n "${NS}" exec "${web_pod}" -- wget -q -O - http://127.0.0.1/ >/dev/null \
  || fail "web pod ${web_pod} failed index probe"
log "web in-pod probe OK: ${web_pod}"

# --- 9. done ----------------------------------------------------------------------
log "deploy ${TAG} complete"
kubectl -n "${NS}" get pods svc ingress
log "ingress URL: https://${INGRESS_HOST}  (TLS via cert-manager, issuer celestium-le-production)"
