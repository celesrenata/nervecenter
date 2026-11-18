#!/usr/bin/env bash
# Probe the IDE NetworkPolicy from a throwaway pod labelled app=ide in the
# workbench namespace. Blocked: database, API port, Kubernetes API server,
# 169.254.169.254, an RFC 1918 address. Allowed: DNS, OmniRoute, public 443.
# Exit 0 when all checks behave; otherwise exit 1 naming the failing check(s).
#
# Env (defaults match the NerveCenter cluster layout; override as needed):
#   KUBECTL                 kubectl binary/command (default kubectl)
#   WORKBENCH_NAMESPACE     default workbench
#   PROBE_IMAGE             image with nc and nslookup (default busybox:1.36)
#   PROBE_DB_HOST/PORT      default postgres.nervecenter.svc.cluster.local / 5432
#   PROBE_API_HOST/PORT     default api.nervecenter.svc.cluster.local / 8000
#   PROBE_K8S_API_HOST/PORT default kubernetes.default.svc.cluster.local / 443
#   PROBE_RFC1918_HOST      default 192.168.42.8 (port 443 is allowed only to public)
#   PROBE_OMNIROUTE_HOST    default omniroute.omniroute.svc.cluster.local (port 20128)
#   PROBE_PUBLIC_HOST       default 1.1.1.1 (port 443)
#   PROBE_DNS_NAME          default kubernetes.default.svc.cluster.local
set -u

kc="${KUBECTL:-kubectl}"
ns="${WORKBENCH_NAMESPACE:-workbench}"
image="${PROBE_IMAGE:-busybox:1.36}"
pod="netpol-probe-$$"

db_host="${PROBE_DB_HOST:-postgres.nervecenter.svc.cluster.local}"
db_port="${PROBE_DB_PORT:-5432}"
api_host="${PROBE_API_HOST:-api.nervecenter.svc.cluster.local}"
api_port="${PROBE_API_PORT:-8000}"
k8s_host="${PROBE_K8S_API_HOST:-kubernetes.default.svc.cluster.local}"
k8s_port="${PROBE_K8S_API_PORT:-443}"
rfc_host="${PROBE_RFC1918_HOST:-192.168.42.8}"
omni_host="${PROBE_OMNIROUTE_HOST:-omniroute.omniroute.svc.cluster.local}"
pub_host="${PROBE_PUBLIC_HOST:-1.1.1.1}"
dns_name="${PROBE_DNS_NAME:-kubernetes.default.svc.cluster.local}"

cleanup() { $kc -n "$ns" delete pod "$pod" --ignore-not-found --wait=false >/dev/null 2>&1; }
trap cleanup EXIT

$kc -n "$ns" run "$pod" --image="$image" --restart=Never --labels=app=ide \
  --command -- sleep 600 >/dev/null || { echo "FAIL setup: cannot create pod" >&2; exit 1; }
$kc -n "$ns" wait --for=condition=Ready "pod/$pod" --timeout=120s >/dev/null \
  || { echo "FAIL setup: pod not ready" >&2; exit 1; }

failed=()

tcp() { $kc -n "$ns" exec "$pod" -- nc -z -w 5 "$1" "$2" >/dev/null 2>&1; }

expect_blocked() {
  if tcp "$2" "$3"; then failed+=("$1 (reachable, should be blocked)"); else echo "OK   $1 blocked"; fi
}
expect_allowed() {
  if tcp "$2" "$3"; then echo "OK   $1 allowed"; else failed+=("$1 (unreachable, should be allowed)"); fi
}

expect_blocked "database"            "$db_host"  "$db_port"
expect_blocked "api-port"            "$api_host" "$api_port"
expect_blocked "kubernetes-api"      "$k8s_host" "$k8s_port"
expect_blocked "metadata-169.254.169.254" 169.254.169.254 80
expect_blocked "rfc1918"             "$rfc_host" 443

if $kc -n "$ns" exec "$pod" -- nslookup "$dns_name" >/dev/null 2>&1; then
  echo "OK   dns allowed"
else
  failed+=("dns (lookup failed, should be allowed)")
fi
expect_allowed "omniroute"           "$omni_host" 20128
expect_allowed "public-443"          "$pub_host"  443

if [ "${#failed[@]}" -gt 0 ]; then
  for f in "${failed[@]}"; do echo "FAIL $f" >&2; done
  exit 1
fi
echo "All IDE NetworkPolicy checks passed."
exit 0
