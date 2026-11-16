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
# OmniRoute client key (owner: "kubectl for omniroute has the api key in secrets"):
# reuse NerveCenter's working OmniRoute client key, never printed. The
# password key stays owner-created; this only adds/refreshes omniroute_api_key.
OMNI_KEY_B64="$(kubectl -n nervecenter get secret nervecenter-env -o jsonpath='{.data.NERVE_OMNIROUTE_API_KEY}')"
if kubectl -n opencode get secret opencode-server >/dev/null 2>&1; then
  kubectl -n opencode patch secret opencode-server --type merge \
    -p "{\"data\":{\"omniroute_api_key\":\"${OMNI_KEY_B64}\"}}" >/dev/null
else
  echo "NOTE: secret opencode-server (key: password) is missing; create it, then re-run"
fi
unset OMNI_KEY_B64
kubectl apply -f networkpolicy.yaml -f configmap.yaml -f pvc.yaml -f deployment.yaml -f service.yaml
kubectl -n opencode get svc opencode-lan
