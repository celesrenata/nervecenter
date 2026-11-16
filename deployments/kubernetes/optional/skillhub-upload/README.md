# NerveCenter skill upload (NS-ADR-112)

Writes one `SKILL.md` per NerveCenter agent to the OmniRoute Skill Hub share so agents
and other clients can find them under domain `nervecenter`.

- Share: `192.168.42.8:/volume1/Kubernetes/omniroute/skills`
- The Job mounts it read-write at subPath `nervecenter` only, as `/skills/nervecenter`.
  Other domain folders are not reachable from the pod.
- Content comes from the generator (`backend/agents/skillpack.py`) through the ConfigMap
  `nervecenter-skillpack`. Files are written to a temp name and renamed, so re-running
  updates them in place. The Job removes only slugs listed in its own previous
  `.pack-manifest`.
- Not in the base kustomization: `deploy.sh` never applies it.

## Run it (owner)

```bash
python -m backend.owner_tools.skillhub_upload plan            # read-only; shows path, files, Job
python -m backend.owner_tools.skillhub_upload apply --approved  # writes to the NAS
python -m backend.owner_tools.skillhub_upload refresh         # POST /api/skills/catalog/refresh
python -m backend.owner_tools.skillhub_upload verify          # GET /api/skills/catalog?domain=nervecenter
```

The Job runs as uid/gid 10001 by default (`--uid`/`--gid` change it). If the NFS export
rejects that user, pick the uid that owns the share. `refresh` and `verify` read
`NERVE_OMNIROUTE_PRIVILEGED_KEY` or the cluster Secret and never print it.
