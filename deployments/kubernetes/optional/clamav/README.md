# ClamAV sidecar overlay (DRAFT)

**Status: DRAFT. Never applied. Unverified in-cluster.** Nothing here has run
on a cluster. It is not referenced by `deployments/kubernetes/kustomization.yaml`
or `base/`, so a normal `kubectl apply -k deployments/kubernetes` ignores it.
The image is an invalid placeholder, so the overlay cannot be applied by
accident.

## Why a sidecar, not a Service

`backend/mail_security/clamav.py` accepts only a unix socket or a loopback
address. clamd therefore runs as a second container in the `api` pod and the
api container reaches it at `127.0.0.1:3310` through `NERVE_CLAMD_HOST` and
`NERVE_CLAMD_PORT` (names from `backend/mail_security/attachment_wiring.py`).
Do not add a Service for clamd; the client would refuse the address anyway.

## Files

- `kustomization.yaml`: pulls in `../../base` and applies the patch to Deployment `api`.
- `api-clamd-sidecar-patch.yaml`: adds the env vars, the `clamd` container, and emptyDir volumes.

## Before you apply: pin the image

1. Pick a ClamAV image you trust. The orchestrator checked behavior on clamd 1.5.4; use a tag you have tested.
2. Resolve it to a digest, for example `crane digest <repo>/clamav:<tag>` or `docker buildx imagetools inspect <repo>/clamav:<tag>`.
3. Replace `registry.invalid/REPLACE-ME/clamav:PIN-TAG-BEFORE-APPLY` in the patch with `<repo>/clamav:<tag>@sha256:<digest>` (digest pinned). Mirror it into your registry if the cluster cannot reach the public one.
4. Check against that image: the uid/gid (`runAsUser: 100`, `runAsGroup: 101` are assumptions), that `nc` exists for the probes, and the writable paths (`/var/lib/clamav`, `/run/clamav`, `/var/log/clamav`, `/tmp`). Add emptyDir mounts for any other path clamd or freshclam writes.
5. Make clamd listen on loopback only (`TCPAddr 127.0.0.1` in clamd.conf), otherwise it also binds the pod IP. This draft does not ship a clamd.conf; mount one for the image you pin. Set `StreamMaxLength` at or above the client cap (default 16 MiB, `max_stream_bytes`) so the client cap triggers first.

## Sizing (not measured on this cluster)

clamd needs roughly 1-2 GiB for the signature set. The draft requests 1536Mi
with a 3Gi limit because a database reload can briefly hold old and new
signatures together. These numbers are **not measured on this cluster**.
Re-size from `kubectl top pod` after a real run. The pod's total memory is now
the api container plus clamd.

## Tradeoffs

(a) **Signature updates and reload.** freshclam must keep signatures current
and clamd must pick them up. On a real clamd 1.5.4 a startup race left clamd on
stale signatures until it was reloaded. Use the image's documented mechanism
(freshclam `NotifyClamd`, or clamd's periodic `SelfCheck`), or reload by hand:
`kubectl exec <pod> -c clamd -- sh -c "printf 'RELOAD\n' | nc -w 5 127.0.0.1 3310"`.
Confirm the reload happens automatically on the image you pin; this draft does
not add a reload step.

(b) **Stale signatures make every scan non-clean.** The client treats
signatures older than 3 days (`max_signature_age`) as not clean
(`SIGNATURES_STALE`, status `UNAVAILABLE`). If freshclam cannot reach the
mirrors, every attachment scan becomes non-clean. Alert on signature age, for
example by exporting the VERSION date from clamd and alerting well before 3 days.

(c) **Egress cannot be isolated.** A sidecar shares the pod's network
namespace, so a NetworkPolicy cannot allow freshclam egress while denying it to
the api container. The alternative is a separate updater CronJob that runs
freshclam and writes signatures to a shared volume, with clamd and the api pod
having no egress. That needs a ReadWriteMany (or equivalent) volume and a
reload path. It is not implemented here.

(d) **Scanning cost and size cap.** Each attachment is streamed to clamd and
scanned in memory and CPU of the api pod. The client caps a stream at
`max_stream_bytes` (16 MiB default), 4 concurrent scans, and a 30 s total
budget. Larger attachments are not scanned (`TOO_LARGE`).

## Operator commands (NOT run by this change)

Run from the repository root, after pinning the image:

```sh
# 1. Render (local, contacts no cluster)
kubectl kustomize deployments/kubernetes/optional/clamav

# 2. Server-side dry run (contacts the cluster, changes nothing)
kubectl kustomize deployments/kubernetes/optional/clamav | kubectl apply --server-side --dry-run=server -f -

# 3. Apply
kubectl kustomize deployments/kubernetes/optional/clamav | kubectl apply -f -
```

Then verify in-cluster: `kubectl -n nervecenter rollout status deploy/api`,
the clamd container is Ready, and the VERSION date is fresh. Note that the
overlay renders the whole base, so applying it re-applies every base object.

## Open items

- In-cluster verification of clamd (image, uid, probes, reload, memory).
- Signature-age alerting.
- Decision on sidecar vs updater CronJob.
