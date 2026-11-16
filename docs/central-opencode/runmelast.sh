#!/usr/bin/env bash
# Remove central opencode. Keeps the PVC (workspaces) unless DELETE_DATA=1.
set -euo pipefail
cd "$(dirname "$0")"
kubectl delete -f service.yaml -f deployment.yaml -f configmap.yaml -f networkpolicy.yaml --ignore-not-found
if [ "${DELETE_DATA:-0}" = "1" ]; then kubectl delete -f pvc.yaml --ignore-not-found; fi
