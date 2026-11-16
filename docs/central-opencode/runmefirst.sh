#!/usr/bin/env bash
# Central opencode (NS-ADR-106). Applies only the files in this directory.
# Prerequisites: image built (./build-image.sh) and the owner-created Secret
# `opencode-server` (keys password, omniroute_api_key) in namespace opencode.
set -euo pipefail
cd "$(dirname "$0")"
kubectl apply -f namespace.yaml
# Harbor pull secret: copy the per-namespace copy from nervecenter (no values printed).
if ! kubectl -n opencode get secret harbor-pull >/dev/null 2>&1; then
  kubectl -n nervecenter get secret harbor-pull -o json \
    | python3 -c 'import json,sys; s=json.load(sys.stdin); m=s["metadata"]; s["metadata"]={"name":m["name"],"namespace":"opencode"}; print(json.dumps(s))' \
    | kubectl apply -f - >/dev/null
fi
kubectl -n opencode get secret opencode-server >/dev/null 2>&1 \
  || echo "NOTE: secret opencode-server is missing; the pod will wait for it"
kubectl apply -f networkpolicy.yaml -f configmap.yaml -f pvc.yaml -f deployment.yaml -f service.yaml
kubectl -n opencode get svc opencode-lan
