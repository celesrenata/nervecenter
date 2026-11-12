# Dedicated attachment detonation microVMs — scoping spec (DRAFT, not approved)

Origin: NerveShield task 8.5 (design §15, "Self-hosted detonation research"). This is a **new security boundary**: a separate product spec with heavy security review. Nothing here is built, and no sample has been executed.

## 1. Problem

Today attachments get static checks and an optional local ClamAV scan (NS-ADR-014). Encrypted archives and password-protected documents are `INCOMPLETE`, never clean. Detonation would observe behavior (process tree, file and registry writes, network attempts) for documents, scripts and executables that static analysis can't judge.

## 2. What the cluster has today (checked 2026-11-12, read-only)

| Item | State | Implication |
|---|---|---|
| KubeVirt | deployed (`kubevirt/kubevirt`, phase Deployed); 3 VMIs running | microVM-per-sample is technically available |
| Kata / Firecracker runtime class | **absent** (only crun, nvidia, wasm runtimes) | no lightweight sandboxed container path; KubeVirt VMs are the only isolation option |
| Node KVM label (`devices.kubevirt.io/kvm`) | not advertised on any of the 4 nodes in the label query | verify device plugin capacity before sizing |
| NetworkPolicy in `nervecenter` | **none** | prerequisite: a CNI that enforces NetworkPolicy, verified with a deny test, before any sample runs |
| Node sharing | every node also runs other workloads (GPU, Plex worker, VMs, NerveCenter) | a hypervisor escape lands on a shared node; needs a dedicated node or taint |

## 3. Threat model deltas (what's new)

**T-D1: sandbox or hypervisor escape.** Malware is deliberately executed.
- KubeVirt runs QEMU/KVM inside a `virt-launcher` pod, so an escape reaches the launcher pod and then the node.
- Mitigations:
  - a dedicated, tainted node with no secrets, no GPU and no NerveCenter workloads;
  - prompt node-kernel and QEMU patch cadence;
  - no host devices passed through.

**T-D2: egress abuse.** A sample uses the sandbox to attack others, or to call home and confirm the target. The default is **no egress**. Optional sinkholed DNS and HTTP with a fake internet happen inside the VM network only, are logged, and never reach the real internet.

**T-D3: data exfiltration from the sandbox.**
- Only the sample enters; no mail body, address, credential or token.
- The VM has no service account token, no cloud metadata and no cluster DNS.
- Results come out as a bounded, schema-validated JSON report (codes and counts). No dropped files leave.

**T-D4: input smuggling into the analyzer.** The report is attacker-influenced, so it's treated as untrusted data through the same strict schemas as model output (R-022.1). A report can only add findings.

**T-D5: legal and licensing.** Handling live malware locally needs owner acceptance. A Windows guest image needs a valid licence.

## 4. Requirements, if approved later

1. **DR-1:** One ephemeral VM per sample from a read-only golden image. The disk is discarded after each run, the run has a hard wall-clock limit (e.g. 120 s), CPU and memory are capped, and the VM is destroyed even if the run fails.
2. **DR-2:** It runs only on a dedicated, tainted node that holds no other workload's secrets. Enforced by taints, tolerations and node affinity; a test checks that no other pod can schedule there.
3. **DR-3:** Network policy denies all egress from the VM namespace by default; verified by a probe that must fail. Optional sinkhole services sit inside the namespace only.
4. **DR-4:** The sample goes in as a read-only volume, and only the structured report comes out. There is no shared filesystem, clipboard or guest agent with host access.
5. **DR-5:** Samples go to detonation only on an explicit policy decision: static INCOMPLETE plus a high-impact context, and an owner opt-in per account. Detonation is never triggered by message text.
6. **DR-6:** The sample's hash and the report are stored encrypted (NS-ADR-062). Samples are deleted after analysis, unless the owner opts in to retention for intel.
7. **DR-7:** Every run is audited (who, what policy, hash, duration, outcome code). Kill-switch flag: OFF by default.

## 5. Measurable tradeoffs (to measure before any approval)

| Dimension | Metric | How |
|---|---|---|
| Detection uplift | share of `INCOMPLETE` attachments that detonation resolves to a verdict; recall on a licensed or public malicious-document set vs. ClamAV-only | offline lab with public sample sets (e.g. MalwareBazaar under its terms), never owner mail |
| False positives | benign-document flag rate | public benign office/PDF corpus |
| Latency | p50/p95 seconds per sample, including VM boot | KubeVirt on the dedicated node |
| Cost | node-hours per 1,000 samples; disk churn | measured |
| Isolation | egress probe denied; SA token absent; metadata unreachable; escape-surface checklist | automated checks per run |
| Operational | golden-image patch cadence; QEMU/kernel CVE response time | runbook |

## 6. Recommendation for the gate

**Do not approve detonation as a product yet.** Prerequisites first:
- an enforced NetworkPolicy (none exists in the namespace today);
- a dedicated node;
- an owner decision on handling live malware.

A useful intermediate step is **static deep inspection** in a no-network container: document macro and OLE extraction, archive listing, PDF object analysis. It resolves part of `INCOMPLETE` without executing anything. That would be its own small spec.

## 7. Owner decisions

- **D-1:** Accept local handling of live malware samples at all?
- **D-2:** Dedicate and taint a node for detonation (which one, given the GPU, VF and Plex allocations)?
- **D-3:** Which guest OS images (Linux only, or Windows with licence)?
- **D-4:** Approve the static-deep-inspection intermediate spec instead?
