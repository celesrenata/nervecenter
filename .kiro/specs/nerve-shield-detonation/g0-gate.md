# G0 — Attachment detonation (scoping gate)

Date: 2026-11-13. Owner: "yes" to scoping this spec (G8 follow-on gate, decision 4). Read-only checks only: `kubectl get` on the cluster and queries on the live NerveShield database. Nothing was built, and no sample was executed.

**Verdict: STOP. One of the three prerequisites is now met and the KVM capacity question is answered. The measured need is small: 14 unresolved attachments in 342 messages. Recommendation: build the static deep-inspection step first, not detonation.**

## Cluster facts, re-checked

| Item | Draft (2026-11-12) | Now (2026-11-13) |
|---|---|---|
| Kubernetes | — | k3s v1.36.5 on NixOS 26.11, 4 nodes (`gremlin-1..4`), flannel |
| NetworkPolicy enforcement | "none in namespace; must verify" | **Met.** k3s's embedded policy controller is not disabled (node args), and enforcement was proven live for `clamav-freshclam` (api port blocked, update mirror reachable; NS-ADR-065). Other namespaces (`argocd`, `comfyui-service`) also rely on policies |
| NetworkPolicy in `nervecenter` | none | only `clamav-freshclam`; api, web, postgres and NATS are unrestricted |
| KVM capacity | "not advertised in label query" | **`devices.kubevirt.io/kvm: 1k` allocatable on all 4 nodes.** The draft's label query was the wrong check |
| Sandboxed runtimes | no Kata/Firecracker | unchanged: crun, nvidia(-experimental), wasm runtimes only. KubeVirt VMs remain the only isolation option |
| Dedicated node | none | **still none.** No node is tainted, and all four run shared workloads (GPU, Plex, VMs, NerveCenter) |

## Measured need (latest assessment per message, live)

| Attachment outcome | Count |
|---|---|
| Scanned by ClamAV (`ENGINE_CLEAN_VERDICT_NOT_SAFETY`) | 58 |
| `SCAN_NOT_RUN` | 13 |
| `DECODED_BYTES_EXCEEDED` | 1 |
| Messages total | 342 |

Detonation would only help the unresolved 14 (about 4% of messages), and some of those are just unscanned rather than unanalyzable. Next step: break the 13 `SCAN_NOT_RUN` down by cause (pre-ClamAV history, type or size) before sizing anything.

## Prerequisites status

1. Enforced NetworkPolicy: **met** (cluster level; a deny-all for the detonation namespace still has to be written and probed).
2. Dedicated, tainted node: **open**. This is the biggest cost: one of four nodes, which all carry GPU, VF or Plex roles.
3. Owner decision on handling live malware: **open**.

## Recommendation

1. **Do not approve detonation yet.** The need is small, and its main cost (a dedicated node) is high.
2. **Approve the intermediate spec instead: static deep inspection** in a no-network, no-secret container. It covers OLE/macro extraction, archive listing (encrypted entries reported, never cracked), PDF object and JavaScript analysis, and resolves part of `INCOMPLETE` without executing anything. It fits the existing pod pattern (like clamd) and needs neither KVM nor a dedicated node.
3. Separately, it's worth adding a default-deny NetworkPolicy for the `nervecenter` namespace (api, web, postgres, NATS). That's a hardening change on live workloads, so it needs your go-ahead.

## Owner decisions

- **D-1:** Accept local handling of live malware at all? (Only needed if detonation is ever approved.)
- **D-2:** Dedicate and taint a node? (Same condition.)
- **D-3:** Guest images (Linux only, or Windows with a licence)? (Same condition.)
- **D-4:** Approve the static deep-inspection spec as the next step? (Recommended.)
- **D-5:** Add a default-deny NetworkPolicy for the `nervecenter` namespace?
