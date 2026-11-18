# Workbench (opencode + Menagerie)

Optional add-on. Manifests: `deployments/kubernetes/optional/workbench/`. They are
deliberately not referenced by `deployments/kubernetes/kustomization.yaml` or
`base/`, so nothing here is deployed until the Owner applies them.

This page covers Phase 1 (NFS project storage and the file workers), Phase 2
(opencode `/projects` mount) and Phase 3 (Browser_IDE).

What Phase 1 creates, all in namespace `workbench` (Pod Security `restricted`):

- Two static NFS PVs and PVCs: `wb-opencode-projects` and `wb-menagerie-projects`
  (50Gi, `ReadWriteMany`, `Retain`, `storageClassName: ""`).
- Two file workers, `wb-fs-opencode` and `wb-fs-menagerie` (port 8090). Each mounts
  exactly one store, so no pod has both.
- NetworkPolicy: default deny, file worker ingress only from `app: api` pods in
  `nervecenter`.

| Store | NFS export (server `192.168.42.8`) | PV / PVC |
|-------|------------------------------------|----------|
| opencode | `/volume1/Kubernetes/opencode/projects` | `wb-opencode-projects` |
| menagerie | `/volume1/Kubernetes/menagerie/projects` | `wb-menagerie-projects` |

The existing Longhorn PVC `opencode-data` (namespace `opencode`) is not touched.
Its spec, bound volume and handoff clones are identical before and after enabling.

## NFS mount options

Both PVs set `mountOptions: [nfsvers=4.1, hard]`.

- `nfsvers=4.1`: pins the protocol version. NFSv4 uses one TCP port (2049), which
  keeps firewall rules simple, and has built-in locking, so no separate lockd or
  statd is needed. Pinning stops a node from silently negotiating v3 or v4.0.
- `hard`: if the server stops answering, I/O blocks and retries until it returns.
  With `soft`, a timeout returns an I/O error to the application, and a write that
  a program believed succeeded can be lost or truncated. For a store holding source
  code, a stalled write is safer than a corrupt one. Trade-off: processes touching a
  dead mount can hang in uninterruptible sleep until the server comes back.

`noac` is not set (see the attribute-cache limit below).

## UID/GID requirement

Every process that writes to the exports runs as one UID/GID, default `1000:1000`
(`WORKBENCH_UID`, `WORKBENCH_GID`). The file workers set `runAsUser`, `runAsGroup`
and `fsGroup` to 1000 and the same values in their env. The directories on both
exports must be owned by that UID/GID (and writable by it) on the NFS server.
Squashing rules on the server (for example Synology "map all users to admin")
must produce that ownership for files this user creates. `nfs-check.sh` fails if a
marker file it creates is not owned by `WORKBENCH_UID:WORKBENCH_GID`. If you change
the IDs, change them in `fsworker.yaml` and in the check env together.

## Known limit: NFS attribute cache

The NFS client caches file attributes (including mtime) for a few seconds. The
Workbench uses mtime for edit conflict detection. A write made from another node (an
opencode session or an IDE pod) can therefore look unchanged for a few seconds to a
reader on a different node, and a conflicting save in that window may not be
detected. The file worker reads a fresh `stat` after opening a file, which narrows
this for its own writes only. `noac` would remove the window but slows every
metadata operation, so it is not set. Treat the window as a few seconds.

## Backups are the Owner's responsibility

The Workbench does not back up either export. The Owner must keep NFS snapshots or
backups of both `/volume1/Kubernetes/opencode/projects` and
`/volume1/Kubernetes/menagerie/projects` on the NAS. `Retain` protects against
deleting a PVC, not against disk failure, an accidental delete in a project, or a
bad NAS update.

### Restore check

Run this after setting up backups, and again after any backup change. It uses a
sample project, never the live one.

1. Pick a sample project directory `<slug>` on the export and record its manifest
   from a cluster node with the export mounted at `/mnt/workbench/<store>`:
   `(cd /mnt/workbench/<store>/<slug> && find . -type f -print0 | sort -z | xargs -0 sha256sum) > /tmp/<slug>.src.sha256`.
   Check: the file is non-empty.
2. Restore that directory from the NAS snapshot or backup into a scratch path, for
   example `/mnt/workbench/<store>/.restore-check/<slug>` (create `.restore-check`
   first; do not restore over the live directory). Check: the restored directory
   exists and is non-empty.
3. Build the same manifest on the restored copy and compare:
   `(cd /mnt/workbench/<store>/.restore-check/<slug> && find . -type f -print0 | sort -z | xargs -0 sha256sum) | diff - /tmp/<slug>.src.sha256`.
   Check: `diff` prints nothing and exits 0. Any output means the backup does not
   match the source.
4. Remove only the scratch copy you made, `.restore-check/<slug>`, and `/tmp/<slug>.src.sha256`.
   Check: the live `<slug>` directory is unchanged.

Do this for one project in each store.

## Enable (Owner steps)

Run from the nervcenter repo root with `kubectl` pointed at the cluster.

1. Prepare the exports on the NFS server: both directories exist, are owned by
   `1000:1000`, and are exported to the cluster nodes with NFSv4.1.
   Command (on a node): `sudo mkdir -p /mnt/workbench/opencode /mnt/workbench/menagerie`
   and `ls -ldn` on each mounted export.
   Check: owner and group print as `1000 1000`.
2. Run the NFS check from a cluster node (needs root to mount):
   `sudo WORKBENCH_NFS_MOUNT=1 deployments/kubernetes/optional/workbench/nfs-check.sh`.
   Check: it prints `OK   export=opencode ...`, `OK   export=menagerie ...`, then
   `All Workbench NFS exports passed.` and exits 0. A non-zero exit names the
   failing export; fix that before continuing.
   Override variables if you use other IDs or paths (`WORKBENCH_UID`,
   `WORKBENCH_GID`, `WORKBENCH_OPENCODE_PATH`, `WORKBENCH_MENAGERIE_PATH`).
3. Record the current state of the Longhorn PVC so you can prove it is unchanged:
   `kubectl -n opencode get pvc opencode-data -o yaml > /tmp/opencode-data.before.yaml`.
   Check: the file is non-empty and `status.phase` is `Bound`.
4. Get the file worker image into Harbor. `deployments/harbor/deploy.sh` retags the
   api image the workers use (`registry.celestium.life/nervecenter/api:latest`).
   Check: `kubectl -n nervecenter get deploy api -o jsonpath='{.spec.template.spec.containers[0].image}'`
   shows the same registry path, and the image contains `backend.workbench.fsworker`.
5. Render the manifests before applying them:
   `kubectl kustomize deployments/kubernetes/optional/workbench | kubectl apply --dry-run=server -f -`.
   Check: every object reports `created (server dry run)` and no errors.
6. Apply: `kubectl apply -k deployments/kubernetes/optional/workbench`.
   Check: `kubectl get ns workbench` shows `Active`.
7. Confirm both claims bind:
   `kubectl -n workbench get pvc wb-opencode-projects wb-menagerie-projects`.
   Check: both show `Bound` within 60 seconds.
8. Confirm the file workers start:
   `kubectl -n workbench rollout status deploy/wb-fs-opencode deploy/wb-fs-menagerie`
   (run one command per deployment) and `kubectl -n workbench get pods`.
   Check: both pods are `Running` and `1/1` Ready. If a pod is `Pending`, see
   "NFS server unreachable" below.
9. Confirm mount options took effect:
   `kubectl -n workbench exec deploy/wb-fs-opencode -- cat /proc/mounts` and look for
   the `/projects` line.
   Check: it shows `nfs4`, `vers=4.1` and `hard`. Repeat for `wb-fs-menagerie`.
10. Confirm network isolation: `kubectl -n workbench get networkpolicy`.
    Check: `default-deny`, `fsworker-ingress-from-api` and `fsworker-egress` are listed.
11. Confirm Longhorn is untouched:
    `kubectl -n opencode get pvc opencode-data -o yaml | diff - /tmp/opencode-data.before.yaml`.
    Check: no differences other than `resourceVersion`, and
    `kubectl -n opencode get pods` shows the opencode pod unchanged.

## Rollback

Returns the cluster to its state before enabling. No step deletes anything on the
NFS exports. PVs use `Retain`, so removing the PVs and PVCs from Kubernetes leaves
every file on the NAS in place.

1. Stop the workers so nothing writes: 
   `kubectl -n workbench scale deploy/wb-fs-opencode deploy/wb-fs-menagerie --replicas=0`.
   Check: `kubectl -n workbench get pods` shows no running worker pods.
2. Before removing anything, record what is on the exports so you can compare later:
   `ls -la /mnt/workbench/opencode /mnt/workbench/menagerie > /tmp/exports.before-rollback.txt`
   (mount them on a node first if needed).
   Check: the file lists your project directories.
3. Remove the Workbench objects: `kubectl delete -k deployments/kubernetes/optional/workbench`.
   This deletes the `workbench` namespace, which removes the workers, services,
   PVCs and NetworkPolicies, and the two cluster-scoped PVs.
   Check: `kubectl get ns workbench` returns NotFound.
4. If a PV is stuck `Terminating` or `Released`, remove only the Kubernetes object:
   `kubectl get pv wb-opencode-projects wb-menagerie-projects`. Delete with
   `kubectl delete pv <name>` (it never touches the export).
   Check: `kubectl get pv | grep wb-` prints nothing.
5. Confirm the data is still there:
   `ls -la /mnt/workbench/opencode /mnt/workbench/menagerie | diff - /tmp/exports.before-rollback.txt`.
   Check: no differences.
6. Confirm Longhorn is untouched:
   `kubectl -n opencode get pvc opencode-data` shows `Bound`, and its handoff clones
   are still present.
7. Confirm the base is unaffected: `kubectl kustomize deployments/kubernetes | grep -c workbench`.
   Check: `0`, and `kubectl -n nervecenter get pods` shows the usual pods running.

Re-enabling later is the Enable procedure again; existing project files are reused.

## Phase 2: opencode `/projects` mount

Phase 2 (`WORKBENCH_PHASE=2`) lets Sessions run in the opencode server with working
directory `/projects/<slug>`. It adds the claim `oc-opencode-projects` in namespace
`opencode`, backed by the Opencode_Store export. The Menagerie_Store is never mounted
in `opencode`. The `opencode-data` PVC is not modified. See
`docs/central-opencode/README.md` for the Deployment volume snippet.

### Enable (Phase 2)

Prerequisite: the Phase 1 Enable procedure is complete.

1. Record the Longhorn PVC again:
   `kubectl -n opencode get pvc opencode-data -o yaml > /tmp/opencode-data.before-p2.yaml`.
   Check: the file is non-empty and `status.phase` is `Bound`.
2. Apply the manifests (includes `pv-pvc-opencode-ns.yaml`):
   `kubectl apply -k deployments/kubernetes/optional/workbench`.
   Check: no errors.
3. Confirm the claim binds: `kubectl -n opencode get pvc oc-opencode-projects`.
   Check: `Bound` within 60 seconds.
4. Add the `projects` volume and the `/projects` mount to the opencode Deployment
   (snippet in `docs/central-opencode/README.md`) and apply it. Keep the
   `opencode-data` volume and its mounts unchanged.
   Check: `kubectl -n opencode rollout status deploy/opencode` succeeds.
5. Confirm the mount: `kubectl -n opencode exec deploy/opencode -- cat /proc/mounts`
   and look for the `/projects` line.
   Check: it shows `nfs4`, `vers=4.1` and `hard`.
6. Set `WORKBENCH_PHASE=2` on the api Deployment and roll it out.
   Check: `GET /api/workbench/config` reports phase 2, and creating a Session for a
   test project runs in `/projects/<slug>`.
7. Confirm Longhorn is untouched:
   `kubectl -n opencode get pvc opencode-data -o yaml | diff - /tmp/opencode-data.before-p2.yaml`.
   Check: no differences other than `resourceVersion`.

### Rollback (Phase 2)

No step deletes anything on the NFS export (`Retain`).

1. Set `WORKBENCH_PHASE=1` on the api Deployment and roll it out.
   Check: Phase 2 routes (files, Sessions) are no longer mounted.
2. Remove the `projects` volume and `/projects` mount from the opencode Deployment
   and apply it. Check: `kubectl -n opencode rollout status deploy/opencode` succeeds
   and the pod mounts only the original volumes.
3. Remove the claim: `kubectl -n opencode delete pvc oc-opencode-projects`, then
   `kubectl delete pv oc-opencode-projects` if it is left `Released`.
   Check: neither object is listed. Project files remain on the NAS.
4. Confirm `opencode-data` is `Bound` and its handoff clones are present.

## Phase 3: Browser_IDE
Phase 3 (`WORKBENCH_PHASE=3` and `WORKBENCH_IDE_ENABLED=true`) lets the API start one
code-server pod per project in namespace `workbench`, reached through the API at
`/ide/`. Each IDE pod mounts exactly one store. The pod holds no git credentials and
no provider key other than an OmniRoute client key.
### Build the IDE image
Build context is `deployments/docker/workbench-ide`. Copy the Owner-supplied Menagerie
VSIX into that directory first.

| Build arg | Required | Meaning |
|-----------|----------|---------|
| `MENAGERIE_VSIX` | yes | File name of the VSIX in the build context. The build fails if it is empty or the file is missing. |
| `MENAGERIE_VERSION` | yes | Version string written to `/opt/menagerie/VERSION`. The build fails if empty. |
| `CODE_SERVER_VERSION` | no | Tag of `codercom/code-server` (default `4.96.4`). |

```
cd deployments/docker/workbench-ide
docker build --build-arg MENAGERIE_VSIX=menagerie.vsix \
  --build-arg MENAGERIE_VERSION=<version> \
  --build-arg CODE_SERVER_VERSION=4.96.4 \
  -t registry.celestium.life/nervecenter/workbench-ide:<version> .
```
Push the image to Harbor and note its digest. The IDE manager rejects an image that is
not pinned by digest (`@sha256:...`), so configure the manager with the digest form.
Check: `docker run --rm --entrypoint cat <image> /opt/menagerie/VERSION` prints
`<version>`.
### Enable (Phase 3)
Prerequisite: Phase 1 is enabled (Phase 2 is only needed for opencode Sessions).
1. Record the Longhorn PVC:
   `kubectl -n opencode get pvc opencode-data -o yaml > /tmp/opencode-data.before-p3.yaml`.
   Check: the file is non-empty and `status.phase` is `Bound`.
2. Build and push the IDE image (above).
   Check: the `VERSION` check prints the version and the digest is recorded.
3. Render and dry-run the manifests, which now include `rbac.yaml` and the
   `api-patch.yaml` patch:
   `kubectl kustomize deployments/kubernetes/optional/workbench | kubectl apply --dry-run=server -f -`.
   Check: no errors; the output has no `ClusterRole` or `ClusterRoleBinding`.
4. Apply: `kubectl apply -k deployments/kubernetes/optional/workbench`.
   Check: `kubectl -n nervecenter get sa nervecenter-workbench` and
   `kubectl -n workbench get role,rolebinding nervecenter-workbench` list the objects.
5. Confirm the API's permissions are only those in the Role:
   `kubectl auth can-i create pods -n workbench --as=system:serviceaccount:nervecenter:nervecenter-workbench`
   returns `yes`, and the same command with `-n nervecenter` returns `no`.
6. Confirm the API rolled out with the new ServiceAccount:
   `kubectl -n nervecenter rollout status deploy/api` and
   `kubectl -n nervecenter get deploy api -o jsonpath='{.spec.template.spec.serviceAccountName}'`.
   Check: the rollout succeeds and the name is `nervecenter-workbench`.
7. Run the NetworkPolicy probe (next section).
   Check: it exits 0.
8. Set `WORKBENCH_PHASE=3` and `WORKBENCH_IDE_ENABLED=true` on the api Deployment, and
   roll it out.
   Check: `GET /api/workbench/config` reports phase 3 with the IDE enabled.
9. Open an IDE for a test project from the Workbench UI.
   Check: `kubectl -n workbench get pods -l app=ide` shows one pod `Running` and
   `1/1` Ready, `/ide/<...>` loads code-server, and `/proc/mounts` in the pod shows
   only that project's store.
10. Confirm Longhorn is untouched:
    `kubectl -n opencode get pvc opencode-data -o yaml | diff - /tmp/opencode-data.before-p3.yaml`.
    Check: no differences other than `resourceVersion`.
### Rollback (Phase 3)
No step deletes anything on the NFS exports. IDE pods mount the stores, but deleting a
pod or Secret never removes files from the export (`Retain`).
1. Set `WORKBENCH_IDE_ENABLED=false` (or `WORKBENCH_PHASE=2`) on the api Deployment and
   roll it out.
   Check: IDE routes are no longer mounted; `GET /api/workbench/config` shows the IDE
   disabled.
2. Delete the IDE pods and their key Secrets only:
   `kubectl -n workbench delete pod -l app=ide` and list Secrets with
   `kubectl -n workbench get secret`; delete those the IDE manager created.
   Check: `kubectl -n workbench get pods -l app=ide` is empty. The file workers are
   still `Running`.
3. Remove the ServiceAccount patch. Delete the `rbac.yaml` entry and the `patches:`
   block from `kustomization.yaml` (or revert them), then
   `kubectl apply -k deployments/kubernetes/optional/workbench`. Apply does not prune,
   so also run:
   `kubectl -n workbench delete role,rolebinding nervecenter-workbench` and
   `kubectl -n nervecenter delete sa nervecenter-workbench`.
   Then re-apply the base Deployment (`kubectl apply -k deployments/kubernetes`) so it
   returns to `automountServiceAccountToken: false` and the default ServiceAccount.
   Check: `kubectl -n nervecenter get deploy api -o jsonpath='{.spec.template.spec.automountServiceAccountToken}'`
   prints `false`, and the pods roll out `Running`.
4. Confirm the data is still there:
   `ls -la /mnt/workbench/opencode /mnt/workbench/menagerie`
   shows your project directories, and `kubectl -n workbench get pvc` shows both claims
   `Bound`.
5. Confirm Longhorn is untouched: `kubectl -n opencode get pvc opencode-data` shows
   `Bound`.
### RBAC and api-patch
`rbac.yaml` creates ServiceAccount `nervecenter-workbench` in `nervecenter`, and a
Role and RoleBinding in `workbench`: pods get/list/watch/create/delete, secrets
get/create/delete, events list. There is no ClusterRole.
`api-patch.yaml` sets `serviceAccountName: nervecenter-workbench` and
`automountServiceAccountToken: true` on the `api` Deployment. It is a patch on the
base Deployment, so kustomize applies it only when the base is in the same build. On a
live cluster use
`kubectl -n nervecenter patch deployment api --patch-file deployments/kubernetes/optional/workbench/api-patch.yaml`.
Side effect: `automountServiceAccountToken` is a pod-level setting, so the `inspector`
init container (which the base keeps token-free, NS-ADR-086) now also gets the
ServiceAccount token mounted. The token only carries the Role's permissions, but the
inspector is no longer token-free. Kubernetes has no per-container switch for this;
accept the change, or run the IDE manager in a separate Deployment. Check after
applying: `kubectl -n nervecenter get pod -l app=api -o yaml` and look for
`/var/run/secrets/kubernetes.io/serviceaccount` under the `inspector` volumeMounts.

### NetworkPolicy probe
`deployments/kubernetes/optional/workbench/netpol-probe.sh` starts a throwaway pod
labelled `app=ide` in `workbench`, runs the checks, and deletes the pod.
Run: `deployments/kubernetes/optional/workbench/netpol-probe.sh`.
Expected: the database, API port, Kubernetes API server, `169.254.169.254` and an
RFC 1918 address are blocked; DNS, OmniRoute (TCP 20128) and a public host on 443 work.
It exits 0 when all behave, otherwise 1 and names the failing checks. Override hosts
and ports with `PROBE_*` variables, `WORKBENCH_NAMESPACE`, `PROBE_IMAGE` and `KUBECTL`
(see the script header). Run it after any NetworkPolicy change and before step 8 of
Enable. It needs a CNI that enforces NetworkPolicy.
### Git wrapper limit
The image puts `git-wrapper.sh` at `/usr/local/bin/git` to deny `git push` and edits to
remotes unless `WORKBENCH_PUSH_APPROVED=1`. This is a guard rail against mistakes. It is
not a security boundary: `/usr/bin/git` can still be called directly. The hard limit is
that no git credentials are mounted into IDE pods, so a push cannot authenticate.
Never mount an SSH key, token or credential helper into an IDE pod.
### Menagerie status assumption
The Workbench reports Menagerie task state (Req 3.11) from `menagerie-status.js`, which
listens on port 8081 and serves `/status` from events it reads on the extension IPC
socket (`ROO_CODE_IPC_SOCKET_PATH`). This endpoint has not been tested against the real
Menagerie extension. The event names and payload shape are assumed. Until it is
verified with the actual VSIX, treat the status as unconfirmed: check that `/status`
on a running IDE reflects a task started in the editor, and fix the script if not.
### Known gap: per-IDE OmniRoute keys
Req 7.12 says an IDE's OmniRoute key is not usable by any other IDE. That is not met
today. `IdeManager`'s default key issuer (`default_key_issuer` in
`backend/workbench/ide.py`) returns the shared regular OmniRoute key from
`backend.inference.key_resolver`, and `omniroute_keys.py` has no per-IDE issuance.
Every IDE therefore receives the same key, so one compromised IDE exposes the key for
all. Before relying on 7.12, implement a real per-IDE issuer (create a distinct key
per IDE, revoke it when the IDE is deleted) and pass it as `key_issuer` to
`IdeManager`. Do not enable the Browser_IDE for untrusted projects until then.
## NFS server unreachable

If the NFS server is down or unreachable when a Workbench pod starts, the kubelet
cannot mount the volume and the pod stays `Pending` (shown as `ContainerCreating`
in some versions). It does not fall back to `emptyDir` or any local storage, so no
project data is written somewhere that would later be lost.

Check: `kubectl -n workbench describe pod <pod>`. The Events section shows a
`FailedMount` event with the mount error (for example "mount failed" or "timed out
waiting for the condition") for the PV server `192.168.42.8`. Fix the server or
network, then the kubelet retries and the pod starts on its own. Run `nfs-check.sh`
from a node to confirm the exports respond.

Because the mounts are `hard`, an already running pod whose server goes away blocks
on I/O until the server returns instead of failing the write.
