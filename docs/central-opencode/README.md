# Central opencode (NS-ADR-106)

Live location: `celes@192.168.42.254:~/sources/kube/opencode` (not a git repo).
This directory in the nervcenter repo is the traceability mirror; keep both in
sync when either changes.

What it is: one `opencode serve` (v2.3.0, pinned by digest in the `Dockerfile`)
in namespace `opencode`, with workspaces as git clones on a Longhorn PVC. Used by
NerveCenter agents for coding handoffs on Linux-buildable repos (the api's
central executor) and by the owner's Macs with `opencode --server`.

## Exposure

- ClusterIP `opencode.opencode.svc:4096` for NerveCenter.
- LoadBalancer `opencode-lan` on the node IPs `10.1.1.12-15:4096` (k3s servicelb),
  reached from the Macs over the owner's WireGuard (Mac `192.168.133.2/28`, routes
  to `10.1.1.0/24`). No Ingress, no public hostname.
- NetworkPolicy: default deny; ingress :4096 only from `nervecenter` api pods and
  `192.168.133.0/28`; egress DNS, OmniRoute `:20128`, TCP 443/22 to public addresses.
- HTTP Basic auth on every request (user `opencode`).

## Owner steps

1. Secrets (values never in files or chat):
   ```bash
   kubectl -n opencode create secret generic opencode-server \
     --from-literal=password="$(openssl rand -hex 32)"
   # runmefirst.sh adds omniroute_api_key itself, copied (never printed) from
   # NerveCenter's working OmniRoute client key in nervecenter-env.
   # optional, read-only token for private repos:
   kubectl -n opencode create secret generic opencode-git --from-file=credentials=./git-credentials
   # NerveCenter side, same password:
   kubectl -n nervecenter create secret generic nervecenter-opencode \
     --from-literal=password='<same password>'
   ```
2. `./build-image.sh` (needs Docker and a Harbor login), then `./runmefirst.sh`.
3. NerveCenter deploy (`deploy.sh`) sets `NERVE_OPENCODE_URL` and reads the password.

## Mac client

```bash
export OPENCODE_PASSWORD="$(security find-generic-password -s nervecenter-opencode -a opencode -w)"
opencode --server http://10.1.1.12:4096
```
Store the password once with
`security add-generic-password -s nervecenter-opencode -a opencode -w` (prompts).
Any node IP 10.1.1.12-15 works only on the node running the pod when
`externalTrafficPolicy: Local` (check `kubectl -n opencode get pod -o wide`).

## Verify after deploy (from the Mac over WireGuard)

1. `curl -s -o /dev/null -w '%{http_code}' http://10.1.1.12:4096/api/info` -> 401.
2. `curl -s -u "opencode:$OPENCODE_PASSWORD" http://10.1.1.12:4096/api/info` -> 200.
3. `curl -sI https://nervecenter.celestium.life/api/info` must not reach opencode
   (SSO redirect or 404).
4. Source IP: `kubectl -n opencode logs deploy/opencode` (or temporarily run
   `--print-logs --log-level debug`) for the request from the Mac. It must show
   `192.168.133.x`. If it shows a node or svclb address, the NetworkPolicy blocks the
   Mac: report it and ask the owner. Do not widen to `10.1.1.0/24`.

## Push policy

The config denies `git push` and `git remote` for every session; NerveCenter
sessions also deny them unless the owner approved push for that handoff in the
Handoffs view.

## Workbench Phase 2: NFS `/projects` mount

Workbench Phase 2 adds a second volume to the opencode Deployment. It is optional
and is not part of `deployment.yaml` in this mirror until the owner enables Phase 2.

- Claim: `oc-opencode-projects` in namespace `opencode`. Its PV and PVC are defined in
  `deployments/kubernetes/optional/workbench/pv-pvc-opencode-ns.yaml`. They point at
  the Opencode_Store export `/volume1/Kubernetes/opencode/projects` (NFSv4.1, `hard`,
  `Retain`), the same export the `wb-fs-opencode` file worker mounts in namespace
  `workbench`.
- Mount: add the claim as a volume and mount it at `/projects` in the `opencode`
  container (and in any init container that needs it):
  ```yaml
  volumeMounts:
    - {name: projects, mountPath: /projects}
  volumes:
    - name: projects
      persistentVolumeClaim: {claimName: oc-opencode-projects}
  ```
- Working directory: a Workbench Session for project `<slug>` runs in
  `/projects/<slug>`.
- Unchanged: the Longhorn PVC `opencode-data`, its `/data` mount and the
  `/workspaces` subPath mount stay as they are. Handoff clones keep using them.
- Only the Opencode_Store is mounted here. The Menagerie_Store is never mounted in
  namespace `opencode`.
- Rollback: remove the `projects` volume and mount from the Deployment, then follow
  the Phase 2 rollback in `docs/workbench/README.md`. Files on the NFS export stay.
