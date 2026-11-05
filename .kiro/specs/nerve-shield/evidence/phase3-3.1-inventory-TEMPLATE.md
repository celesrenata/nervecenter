# Phase 3 — Task 3.1: Arc host inventory results (TEMPLATE)
**Task:** 3.1 Inventory NixOS / Intel GPU driver / OpenVINO / OVMS environment on all four GTi14 hosts, including PF vs VF reliability. **Req:** R-008, R-021.5, R-022.5. **Depends:** G2. **Invariants:** I-04, I-08, I-09. **Threat:** T-27 (artifact supply chain).
**Status: NOT VERIFIED ON HARDWARE.** This file is an empty template. The script was only exercised on a macOS Apple arm64 laptop (script behaviour, not hardware). No install may happen until the owner has filled this in and reviewed it.
**Legend:** VERIFIED = the recorded command ran and exited 0 on that host. UNAVAILABLE = command absent, failed, timed out or skipped. Copy values from the JSON; never fill a cell from memory or a datasheet. Vendor TOPS figures are not evidence (R-021.5).

## How to collect (per host, no sudo, no network, read-only)
```
bash tools/nerve_shield/inventory_arc_host.sh --out-dir ./inventory-<label>
```
- `<label>` is a neutral label such as `gti14-a` to `gti14-d`; do not use the hostname. Redaction is on by default; review both output files before sharing or committing them.
- Optional `--no-runtime-probes` runs presence checks only (skips clinfo, vainfo, ovms, k3s and Python imports). Use it if you do not want vendor libraries loaded; record that choice below.
- Root-only fields (dmesg when restricted, GuC/HuC debugfs, journal, kubelet sockets) show UNAVAILABLE with a hint. Re-running privileged is a separate owner decision. Record it here if you do it.
- Commit only the redacted `inventory.json` and `summary.txt`, under `evidence/` with the label, after review.

## Run log
| Host label | Date (UTC) | Script version | Redaction | Runtime probes | Privileged re-run? | Output files reviewed by | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| gti14-a |  | 1 | ON / OFF | ON / OFF | no / yes |  |  |
| gti14-b |  | 1 | ON / OFF | ON / OFF | no / yes |  |  |
| gti14-c |  | 1 | ON / OFF | ON / OFF | no / yes |  |  |
| gti14-d |  | 1 | ON / OFF | ON / OFF | no / yes |  |  |

## Per-host facts (JSON field id in backticks; status must be VERIFIED or UNAVAILABLE)
Use `V` or `U` before each value.

| Fact (field id) | gti14-a | gti14-b | gti14-c | gti14-d |
| --- | --- | --- | --- | --- |
| CPU model (`hw.cpu_lscpu` / `hw.cpu_proc_cpuinfo`) |  |  |  |  |
| Memory (`hw.mem_meminfo`) |  |  |  |  |
| DMI product / BIOS (`hw.dmi_product_name`, `hw.dmi_bios_version`) |  |  |  |  |
| Kernel (`os.uname`) |  |  |  |  |
| OS release (`os.release_file`) |  |  |  |  |
| NixOS version / generation (`os.nixos_version`, `os.nixos_system_profile`) |  |  |  |  |
| Kernel cmdline GPU params (`os.kernel_cmdline`) |  |  |  |  |
| Display devices, vendor:device (`pci.display_ids`) |  |  |  |  |
| Arc GPU status (`summary.arc_gpu_status`) |  |  |  |  |
| `/dev/dri` nodes (`dri.dev_nodes`) |  |  |  |  |
| Driver in use: i915 or xe (`pci.<bdf>.lspci_k`, `pci.<bdf>.sysfs_driver`, `driver.lsmod`) |  |  |  |  |
| `force_probe` / `enable_guc` (`driver.*`) |  |  |  |  |
| PF or VF (`pci.<bdf>.pf_vf`) |  |  |  |  |
| `sriov_totalvfs` / `sriov_numvfs` (`pci.<bdf>.sriov_*`) |  |  |  |  |
| GuC / HuC load state (`firmware.dmesg`, `firmware.journal_kernel`, `firmware.debugfs_*`) |  |  |  |  |
| level-zero / compute-runtime (`level_zero.*`) |  |  |  |  |
| clinfo (`tool.clinfo`, `version.clinfo_list`) |  |  |  |  |
| vainfo (`tool.vainfo`, `version.vainfo`) |  |  |  |  |
| OpenVINO Python (`version.openvino_python`) |  |  |  |  |
| OpenVINO devices incl. GPU? (`openvino.available_devices`) |  |  |  |  |
| OVMS (`tool.ovms`, `version.ovms`) |  |  |  |  |
| ONNX Runtime + providers (`version.onnxruntime_python`) |  |  |  |  |
| Python versions (`python.python3_version`) |  |  |  |  |
| K3s (`version.k3s`) |  |  |  |  |
| Intel GPU device-plugin socket (`k3s.kubelet_device_plugins`) |  |  |  |  |
| intel_gpu_top present (`tool.intel_gpu_top`) |  |  |  |  |

## PF vs VF reliability (R-008.4; open question 6)
The script reports the current PF or VF state from sysfs only. It cannot show reliability. Fill in from owner observation and later benchmarks; leave blank if unknown.

| Host | Current mode (from `pf_vf`) | SR-IOV in use now? | Past wedging/hangs observed (date, symptom, source) | Evidence link |
| --- | --- | --- | --- | --- |
| gti14-a |  |  |  |  |
| gti14-b |  |  |  |  |
| gti14-c |  |  |  |  |
| gti14-d |  |  |  |  |

## Review gate before any install
- [ ] All four hosts collected, or the missing hosts are listed with a reason.
- [ ] Output reviewed for leaked hostnames, IPs, MACs, usernames, serials.
- [ ] Hosts where the Arc GPU is not visible to OpenVINO (`openvino.available_devices`) are listed.
- [ ] Host differences (kernel, driver, firmware, runtime versions) noted. Open question 6 answered or still open.
- [ ] Anything that must be installed is listed with a pinned version and an artifact hash plan (R-022.5, T-27). Nothing is installed until the owner approves.

## Not established by this task
- Throughput, latency, memory footprint and CPU/GPU comparison (tasks 3.2, 3.3; R-021.5 requires measured numbers).
- GPU inference stability, warm-up and cold-start numbers (open question 6).
- Model choice (open question 7).
- Whether the PF approach is needed (R-008.4) beyond what the owner records above.

## Coverage limits of the script itself
- Redaction is best-effort pattern matching; always review the output.
- Runtime probes may make vendor libraries create their own caches under `$HOME/.cache`.
- `pf_vf` is derived from sysfs (`physfn`, `sriov_totalvfs`); it is not a reliability measure.
- Tested only against fake tool shims and on macOS arm64. Never run on a GTi14, so every cell above stays blank until the owner runs it.
