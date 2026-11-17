# NS-ADR-127: a cmap scan never leaves the approved ranges
Status: accepted. Builds on NS-ADR-125 and NS-ADR-126.
## Context
On 2026-11-17 scan e4080e4f targeted 10.1.1.0/24. cmap classified 10.1.1.253 as a router and started its
feedback loop (`_trigger_feedback_loop`). For routers, switches and firewalls cmap reads the ARP table, interface
IPs, NAT rules, port forwards and routes over SNMP, SSH (the owner's key) and the pfSense REST API, then queues
every address it finds for a full scan, tagging unknown ones "external". That follows the firewall to hosts the
owner never approved. cmap has no stop endpoint, so the scan was stopped by deleting the pod. Nothing outside
10.1.1.x had been queued. A second fault: after the restart `ScanService` marked the scan `completed`, because
cmap's `/api/services/{id}` answers `complete` for any unknown session id.
## Decision: three layers
1. **cmap code.** `deployments/pentest/cmap/patches/0001-strict-scope.patch` is applied to the pinned cmap commit
   with `git apply` at image build (the build fails if it no longer applies). With `CMAP_STRICT_SCOPE=1` (set in the
   manifest and the image) a `ScopeGuard` (`scope_guard.py`, standard library only) built from the subnets of THIS
   crawl request is consulted at every place that queues or stores a discovered host: initial discovery, the masscan
   fallback, the rescan loop's switch ARP, `_enqueue_arp_results`, `_expand_firewall`, `_harvest_switch_via_ssh`, and
   the worker (so any other `queue.put` is caught). Out-of-scope addresses are dropped and logged at INFO as
   `scope-guard: dropped <ip> (outside requested subnets)` with a running counter. `_trigger_feedback_loop` and every
   network helper behind it (`_harvest_arp_table`, `_fw_get_interface_ips_snmp`, `_fw_expand_*`, `_ssh_extract_ips`,
   `_harvest_switch_via_ssh`) return without any SNMP, SSH or API call when the device itself is outside the
   subnets. A router inside the subnets is still read, but nothing it reveals outside them is followed. Tests:
   `deployments/pentest/cmap/tests/test_scope_guard.py` (no cmap dependencies) and a build-time smoke check
   `scope_smoke.py` that drives the patched `CrawlPipeline`. The patch could be upstreamed to
   github.com/celesrenata/cmap; nothing is pushed there.
2. **Network.** NetworkPolicy `cmap-egress-private-only` limits the cmap pod's egress to 10.0.0.0/8, 172.16.0.0/12,
   192.168.0.0/16 and kube-dns (UDP/TCP 53). `cmap-api-only` (ingress) is unchanged.
3. **NerveCenter.** `ScanService.scoped_devices` keeps only hosts inside the scan's own targets AND the ticked,
   approved allowlist (`scope.ip_in_scope`). The count is stored in `pentest_scans.out_of_scope_discarded`
   (migration 046), shown in the scan ledger, and a single info finding of kind `scope` ("cmap reported N hosts outside
   the approved scope (discarded)", one per scan, updated in place) lists them. The daily sweep reads cmap results only
   through this filter, and a scan is audited when it completes.
## Status fix
`CmapClient.status` returns `missing` when the session is not in cmap's memory and cmap holds no hosts for it. The
scan becomes `aborted`. `aborted` joins `UNCOUNTED`: it counts toward neither the min gap, the daily limit nor
"covered this week", exactly like `refused`. A scan cmap really completed still counts. A completed scan with zero
hosts also reads as `missing`, which is harmless (it found nothing and may be repeated). An unreachable cmap leaves the
row unchanged. `status` is a plain string column, so only the new column needs a migration.
