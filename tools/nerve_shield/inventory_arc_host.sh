#!/usr/bin/env bash
# NerveShield task 3.1 - READ-ONLY host inventory for Intel Arc iGPU hosts (BeeLink GTi14).
#
# Purpose: let the owner collect hardware/driver/runtime FACTS on each GTi14 host
# before any installs (requirements R-008, R-021.5; design section 6). Nothing here
# is a benchmark and nothing here claims throughput.
#
# Guarantees
#   * Changes nothing, installs nothing, needs no sudo, makes no network connection,
#     sends nothing anywhere. It only runs local read-only commands and reads files.
#   * Writes NOTHING unless --out-dir is given; then it creates that directory and
#     exactly two files (inventory.json, summary.txt) and refuses to overwrite.
#   * Every field is VERIFIED (the recorded command ran and exited 0) or UNAVAILABLE
#     (command absent, failed, timed out, or deliberately skipped). The exact command
#     is recorded. Values are never guessed or defaulted.
#   * Redaction is ON by default: hostnames, user names, home path, IPv4/IPv6/MAC
#     addresses, UUIDs and serial-number lines are replaced/removed. --no-redact turns
#     it off. Redaction is best-effort pattern matching: REVIEW THE OUTPUT before
#     sharing it. Redaction can also over-redact (e.g. a hostname equal to a common
#     word is replaced everywhere).
#
# Needs root (degrades to UNAVAILABLE with a hint when run unprivileged):
#   * dmesg when kernel.dmesg_restrict=1; GuC/HuC debugfs under /sys/kernel/debug;
#     the journal kernel log unless the user is in the systemd-journal group;
#     /var/lib/kubelet/device-plugins on some setups.
#
# Caveat (honest): runtime probes (python imports of openvino/onnxruntime, clinfo,
# vainfo, ovms --version, k3s --version) load vendor libraries, which may create
# their own cache files under $HOME/.cache. PYTHONDONTWRITEBYTECODE is set so Python
# does not write .pyc files. Use --no-runtime-probes to run presence checks only.
#
# Usage: inventory_arc_host.sh [--redact|--no-redact] [--no-runtime-probes]
#                              [--out-dir DIR] [-h|--help]
#   stdout: machine-readable JSON      stderr: short human summary
#   with --out-dir: JSON -> DIR/inventory.json, summary -> DIR/summary.txt (and stderr)
#
# Test-only overrides (default to the real paths): NS_INV_SYSROOT=/sys NS_INV_DEVROOT=/dev
#
# Compatible with bash 3.2+ (no associative arrays).

# shellcheck disable=SC2329  # f_* filters are invoked by name from rec()
SCRIPT_VERSION=1
SYSROOT=${NS_INV_SYSROOT:-/sys}
DEVROOT=${NS_INV_DEVROOT:-/dev}
REDACT=1
PROBES=1
OUT_DIR=""

export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case $1 in
    --redact) REDACT=1 ;;
    --no-redact) REDACT=0 ;;
    --no-runtime-probes) PROBES=0 ;;
    --out-dir)
      shift
      if [ $# -eq 0 ] || [ -z "$1" ]; then
        echo "error: --out-dir needs a directory" >&2
        exit 2
      fi
      OUT_DIR=$1
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [ -n "$OUT_DIR" ] && { [ -e "$OUT_DIR/inventory.json" ] || [ -e "$OUT_DIR/summary.txt" ]; }; then
  echo "error: refusing to overwrite existing output in $OUT_DIR" >&2
  exit 2
fi

TIMEOUT_BIN=$(command -v timeout 2>/dev/null || command -v gtimeout 2>/dev/null || true)

# ---------------------------------------------------------------- redaction --
# Literal tokens (hostname / user / home) are collected first and replaced verbatim.
HOME_TOKEN=""
USER_TOKENS=()
HOST_TOKENS=()

add_token() { # add_token user|host VALUE
  local kind=$1 val=$2 short
  [ -n "$val" ] || return 0
  if [ "$kind" = user ]; then
    [ "${#val}" -ge 2 ] && [ "$val" != root ] && USER_TOKENS+=("$val")
  else
    [ "${#val}" -ge 3 ] && HOST_TOKENS+=("$val")
    short=${val%%.*}
    [ "$short" != "$val" ] && [ "${#short}" -ge 3 ] && HOST_TOKENS+=("$short")
  fi
  return 0
}

if [ "$REDACT" = 1 ]; then
  case ${HOME:-} in "" | / ) ;; *) HOME_TOKEN=$HOME ;; esac
  add_token user "${USER:-}"
  add_token user "${LOGNAME:-}"
  add_token user "$(id -un 2>/dev/null || true)"
  add_token host "${HOSTNAME:-}"
  add_token host "$(hostname 2>/dev/null || true)"
  add_token host "$(uname -n 2>/dev/null || true)"
fi

IPV4_OCTET='(25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})'
IPV4_RE="${IPV4_OCTET}(\\.${IPV4_OCTET}){3}"
H4='[0-9A-Fa-f]{1,4}'

redact_pipe() { # stdin -> stdout: pattern-based redaction
  awk '{
    l = tolower($0)
    if (l ~ /serial[ _-]?(number|no|num)|serial[ ]*[:=]|s\/n/) { print "<REDACTED:serial-line>"; next }
    print
  }' |
    sed -E \
      -e "s/(^|[^0-9.])${IPV4_RE}([^0-9.]|\$)/\\1<IPV4>\\5/g" \
      -e "s/(^|[^0-9.])${IPV4_RE}([^0-9.]|\$)/\\1<IPV4>\\5/g" \
      -e "s/(${H4}:){7}${H4}/<IPV6>/g" \
      -e "s/(${H4}:){1,6}:(${H4}(:${H4}){0,5})?/<IPV6>/g" \
      -e "s/::${H4}(:${H4}){0,6}/<IPV6>/g" \
      -e 's/[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}/<MAC>/g' \
      -e 's/[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}/<UUID>/g'
}

redact() { # redact STRING -> stdout
  local v=$1 t
  if [ "$REDACT" != 1 ]; then
    printf '%s' "$v"
    return 0
  fi
  [ -n "$HOME_TOKEN" ] && v=${v//"$HOME_TOKEN"/<HOME>}
  for t in "${USER_TOKENS[@]}"; do v=${v//"$t"/<USER>}; done
  for t in "${HOST_TOKENS[@]}"; do v=${v//"$t"/<HOST>}; done
  printf '%s' "$v" | redact_pipe
}

# --------------------------------------------------------------- JSON output --
json_esc() { # json_esc STRING -> JSON string body (ASCII only, no surrounding quotes)
  printf '%s' "$1" |
    tr '\t' ' ' |
    tr -d '\000-\011\013-\037\200-\377' |
    sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' |
    awk 'BEGIN { sep = "" } { printf "%s%s", sep, $0; sep = "\\n" }'
}

san() { printf '%s' "$1" | tr -c 'A-Za-z0-9\n' '_'; }

FIELDS=""
N_VERIFIED=0
N_UNAVAILABLE=0
LAST_STATUS=""
LAST_RAW=""

emit() { # emit ID STATUS COMMAND VALUE REASON NOTE
  local id=$1 status=$2 cmd=$3 value=$4 reason=$5 note=$6 rv rr s
  LAST_STATUS=$status
  LAST_RAW=$value
  s=$(san "$id")
  printf -v "ST_$s" '%s' "$status"
  printf -v "RV_$s" '%s' "$value"
  if [ "${#value}" -gt 6000 ]; then value="${value:0:6000}...[truncated]"; fi
  rv=$(redact "$value")
  rr=$(redact "$reason")
  if [ "$status" = VERIFIED ]; then
    N_VERIFIED=$((N_VERIFIED + 1))
  else
    N_UNAVAILABLE=$((N_UNAVAILABLE + 1))
    rv=""
  fi
  [ -n "$FIELDS" ] && FIELDS="$FIELDS,"$'\n'
  FIELDS="$FIELDS    {\"id\":\"$(json_esc "$id")\",\"status\":\"$status\",\"command\":\"$(json_esc "$cmd")\",\"value\":\"$(json_esc "$rv")\",\"reason\":\"$(json_esc "$rr")\",\"note\":\"$(json_esc "$note")\"}"
}

getv() { # getv ST|RV ID
  local n
  n="$1_$(san "$2")"
  printf '%s' "${!n-}"
}

# ------------------------------------------------------------ run + record ---
f_display() { grep -E 'VGA compatible|Display controller|3D controller' || true; }
f_gpufw() { grep -E 'GuC|HuC|i915|drm.*firmware|xe [0-9a-fA-F]{4}:' | tail -n 60 || true; }
f_modules() { grep -E '^(i915|xe) ' || true; }
f_virtfn() { grep -E '^virtfn' || true; }
f_nixgens() { grep -E 'system-[0-9]+-link' | tail -n 5 || true; }
f_gpulibs() { grep -E 'libze_loader|libze_intel_gpu|libigdgmm|libigc|libOpenCL|libva|libvpl|libmfx' || true; }
f_cpu() { grep -E 'Model name|Architecture|^CPU\(s\)|Thread|Core\(s\)|Socket|Flags' | cut -c1-200 || true; }

fmt_cmd() { printf '%q ' "$@" | sed 's/ $//'; }

# rec ID FILTER NOTE CMD [ARGS...]   (FILTER is a bash function name or "-")
rec() {
  local id=$1 filter=$2 note=$3 cmdstr out rc
  shift 3
  cmdstr=$(fmt_cmd "$@")
  [ "$filter" != "-" ] && cmdstr="$cmdstr | $filter"
  if ! command -v "$1" >/dev/null 2>&1; then
    emit "$id" UNAVAILABLE "$cmdstr" "" "command not found: $1" "$note"
    return 0
  fi
  if [ -n "$TIMEOUT_BIN" ]; then
    out=$("$TIMEOUT_BIN" 20 "$@" 2>&1)
    rc=$?
  else
    out=$("$@" 2>&1)
    rc=$?
  fi
  if [ "$rc" -eq 124 ] && [ -n "$TIMEOUT_BIN" ]; then
    emit "$id" UNAVAILABLE "$cmdstr" "" "timed out after 20s" "$note"
  elif [ "$rc" -ne 0 ]; then
    local why="exit code $rc: ${out:0:200}"
    case $out in
      *ermission* | *"not permitted"* | *"must be root"* | *nsufficient* | *"Operation not"*)
        why="$why (likely requires root; re-run privileged only if you choose to)" ;;
    esac
    emit "$id" UNAVAILABLE "$cmdstr" "" "$why" "$note"
  else
    [ "$filter" != "-" ] && out=$(printf '%s\n' "$out" | "$filter")
    emit "$id" VERIFIED "$cmdstr" "$out" "" "$note"
  fi
  return 0
}

skipped() { # skipped ID CMDTEXT  (runtime probes disabled)
  emit "$1" UNAVAILABLE "$2" "" "skipped: --no-runtime-probes" "presence check only"
}

# rec_probe: like rec but obeys --no-runtime-probes
rec_probe() {
  local id=$1
  if [ "$PROBES" = 1 ]; then
    rec "$@"
  else
    shift 3
    skipped "$id" "$(fmt_cmd "$@")"
  fi
}

presence() { # presence TOOL
  local p
  if p=$(command -v "$1" 2>/dev/null) && [ -n "$p" ]; then
    emit "tool.$1" VERIFIED "command -v $1" "$p" "" "presence on PATH only"
  else
    emit "tool.$1" UNAVAILABLE "command -v $1" "" "not found on PATH" "presence on PATH only"
  fi
}

# ------------------------------------------------------------- collection ----
GEN_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)

# System identity (no hostname, no serials, no uuid).
rec os.uname - "kernel and arch; hostname deliberately not requested" uname -s -r -v -m
rec os.release_file - "" cat /etc/os-release
rec os.sw_vers - "macOS only; expected UNAVAILABLE on Linux" sw_vers
rec os.nixos_version - "" nixos-version
rec os.nixos_system_profile - "current system generation symlink" readlink /nix/var/nix/profiles/system
rec os.nixos_generations f_nixgens "last 5 system-N-link entries" ls -l /nix/var/nix/profiles
rec os.kernel_cmdline - "i915/xe parameters; UUIDs redacted" cat /proc/cmdline
rec hw.cpu_lscpu f_cpu "" lscpu
rec hw.cpu_proc_cpuinfo - "first lines only" head -n 5 /proc/cpuinfo
rec hw.cpu_sysctl_mac - "macOS only" sysctl -n machdep.cpu.brand_string
rec hw.mem_meminfo - "" head -n 3 /proc/meminfo
rec hw.dmi_product_name - "no serial/uuid read" cat "$SYSROOT/class/dmi/id/product_name"
rec hw.dmi_sys_vendor - "" cat "$SYSROOT/class/dmi/id/sys_vendor"
rec hw.dmi_bios_version - "" cat "$SYSROOT/class/dmi/id/bios_version"
rec hw.dmi_bios_date - "" cat "$SYSROOT/class/dmi/id/bios_date"

# PCI display devices.
rec pci.display_lspci f_display "ids in [vendor:device] form" lspci -D -nn
DISPLAY_RAW=$LAST_RAW
DISPLAY_STATUS=$LAST_STATUS

IDS_OUT=""
BDFS=()
if [ "$DISPLAY_STATUS" = VERIFIED ]; then
  OLDIFS=$IFS
  IFS=$'\n'
  set -f
  for line in $DISPLAY_RAW; do
    bdf=${line%% *}
    case $bdf in
      [0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F].[0-7]) ;;
      *) continue ;;
    esac
    ids=$(printf '%s\n' "$line" | sed -n 's/.*\[\([0-9a-fA-F]\{4\}:[0-9a-fA-F]\{4\}\)\].*/\1/p')
    IDS_OUT="${IDS_OUT}${bdf} ${ids:-unparsed}"$'\n'
    if [ "${#BDFS[@]}" -lt 8 ]; then BDFS+=("$bdf"); fi
  done
  set +f
  IFS=$OLDIFS
  emit pci.display_ids VERIFIED "derived from pci.display_lspci (parsed bdf and [vendor:device])" "${IDS_OUT%$'\n'}" "" "8086 = Intel"
else
  emit pci.display_ids UNAVAILABLE "derived from pci.display_lspci" "" "source field pci.display_lspci unavailable" ""
fi

rec dri.dev_nodes - "render/card nodes" ls -l "$DEVROOT/dri"
rec dri.sys_class_drm - "" ls "$SYSROOT/class/drm"
rec driver.lsmod f_modules "i915 vs xe module loaded" lsmod
rec driver.i915_enable_guc - "" cat "$SYSROOT/module/i915/parameters/enable_guc"
rec driver.i915_force_probe - "" cat "$SYSROOT/module/i915/parameters/force_probe"
rec driver.xe_force_probe - "" cat "$SYSROOT/module/xe/parameters/force_probe"

for bdf in "${BDFS[@]}"; do
  d="$SYSROOT/bus/pci/devices/$bdf"
  p="pci.$(san "$bdf")"
  rec "$p.lspci_k" - "Kernel driver in use / modules (i915 vs xe)" lspci -k -s "$bdf"
  rec "$p.sysfs_driver" - "driver symlink target" readlink "$d/driver"
  rec "$p.sriov_totalvfs" - "absent file => SR-IOV not exposed" cat "$d/sriov_totalvfs"
  rec "$p.sriov_numvfs" - "" cat "$d/sriov_numvfs"
  rec "$p.virtfn" f_virtfn "VF children present on a PF" ls "$d"
  phys=unknown
  if [ -e "$d/physfn" ] || [ -L "$d/physfn" ]; then
    rec "$p.physfn" - "physfn exists only on an SR-IOV virtual function" readlink "$d/physfn"
    [ "$LAST_STATUS" = VERIFIED ] && phys=present
  elif [ -d "$d" ]; then
    emit "$p.physfn" VERIFIED "test -e $d/physfn" "absent" "" "no physfn => not a virtual function"
    phys=absent
  else
    emit "$p.physfn" UNAVAILABLE "test -e $d/physfn" "" "device sysfs directory not found: $d" ""
  fi
  tv=$(getv RV "$p.sriov_totalvfs")
  nv=$(getv RV "$p.sriov_numvfs")
  tvs=$(getv ST "$p.sriov_totalvfs")
  case $tv in '' | *[!0-9]*) tv_num=0 ;; *) tv_num=$tv ;; esac
  src="derived from $p.physfn, $p.sriov_totalvfs, $p.sriov_numvfs"
  if [ "$phys" = present ]; then
    emit "$p.pf_vf" VERIFIED "$src" "VF (virtual function; physfn -> $(getv RV "$p.physfn"))" "" "sysfs physfn present"
  elif [ "$phys" = absent ] && [ "$tvs" = VERIFIED ] && [ "$tv_num" -gt 0 ]; then
    emit "$p.pf_vf" VERIFIED "$src" "PF (physical function; SR-IOV capable: totalvfs=$tv numvfs=${nv:-unknown})" "" "no physfn, sriov_totalvfs>0"
  elif [ "$phys" = absent ]; then
    emit "$p.pf_vf" VERIFIED "$src" "PF (physical function; sysfs exposes no SR-IOV capability: totalvfs=${tv:-not readable})" "" "no physfn"
  else
    emit "$p.pf_vf" UNAVAILABLE "$src" "" "physfn could not be determined from sysfs" ""
  fi
done

# Firmware / GuC / HuC.
rec firmware.dmesg f_gpufw "may require root when kernel.dmesg_restrict=1" dmesg
rec firmware.journal_kernel f_gpufw "needs systemd-journal group or root" journalctl -k -b -o cat --no-pager
rec firmware.debugfs_guc_info - "needs root and debugfs; i915 path" cat "$SYSROOT/kernel/debug/dri/0/gt0/uc/guc_info"
rec firmware.debugfs_huc_info - "needs root and debugfs; i915 path" cat "$SYSROOT/kernel/debug/dri/0/gt0/uc/huc_info"

# Runtimes: presence first, then versions (versions obey --no-runtime-probes).
for t in lspci clinfo vainfo intel_gpu_top sycl-ls ovms benchmark_app python3 python k3s; do
  presence "$t"
done
rec_probe version.ovms - "" ovms --version
rec_probe version.clinfo_list - "OpenCL platforms/devices" clinfo -l
rec_probe version.vainfo - "VA-API driver; also prints to stderr" vainfo
rec python.python3_version - "" python3 --version
rec python.python_version - "" python --version
PY_OV='import openvino as ov; print(ov.__version__)'
PY_OV_DEV='import openvino as ov; print(ov.Core().available_devices)'
PY_ORT='import onnxruntime as o; print(o.__version__, o.get_available_providers())'
rec_probe version.openvino_python - "import only" python3 -c "$PY_OV"
rec_probe openvino.available_devices - "Core().available_devices: shows whether GPU is visible to OpenVINO" python3 -c "$PY_OV_DEV"
rec_probe version.onnxruntime_python - "import only; providers list" python3 -c "$PY_ORT"
rec level_zero.ldconfig f_gpulibs "level-zero / compute-runtime / media libs" ldconfig -p
rec level_zero.opengl_driver_libs f_gpulibs "NixOS hardware.graphics path" ls /run/opengl-driver/lib
rec level_zero.resolved_ze_intel_gpu - "version is in the resolved file name" readlink -f /run/opengl-driver/lib/libze_intel_gpu.so.1
rec level_zero.resolved_ze_loader - "version is in the resolved file name" readlink -f /run/opengl-driver/lib/libze_loader.so.1

# K3s / GPU device plugin (local files only; the cluster API is never contacted).
rec_probe version.k3s - "" k3s --version
rec k3s.kubelet_device_plugins - "gpu.intel.com-*.sock indicates the Intel GPU plugin; may need root" ls /var/lib/kubelet/device-plugins

# ----------------------------------------------------------------- summary ---
if [ "$DISPLAY_STATUS" = VERIFIED ]; then
  n_intel=$(printf '%s\n' "$DISPLAY_RAW" | grep -c '\[8086:' || true)
  if [ "${n_intel:-0}" -gt 0 ]; then
    ARC_STATUS="DETECTED ($n_intel Intel display device(s) per lspci; see pci.display_ids)"
  else
    ARC_STATUS="NOT DETECTED (lspci ran; no Intel [8086:] VGA/Display/3D device listed)"
  fi
else
  ARC_STATUS="UNDETERMINED (lspci unavailable; cannot confirm or rule out an Intel GPU)"
fi

SCOPE="Output describes only the host where it was run. Task 3.1 is NOT VERIFIED ON HARDWARE until the owner runs this on each of the four GTi14 hosts and records results in the evidence template."
PLATFORM=$(getv RV os.uname)

TOOLS_PRESENT=""
for t in clinfo vainfo intel_gpu_top sycl-ls ovms benchmark_app k3s; do
  [ "$(getv ST "tool.$t")" = VERIFIED ] && TOOLS_PRESENT="$TOOLS_PRESENT $t"
done

SUMMARY="NerveShield 3.1 host inventory (script v$SCRIPT_VERSION, $GEN_UTC)
Platform:        ${PLATFORM:-UNAVAILABLE}
Redaction:       $([ "$REDACT" = 1 ] && echo ON || echo OFF)   Runtime probes: $([ "$PROBES" = 1 ] && echo ON || echo OFF)
Intel Arc GPU:   $ARC_STATUS"
for bdf in "${BDFS[@]}"; do
  p="pci.$(san "$bdf")"
  SUMMARY="$SUMMARY
  $bdf: $(getv RV "$p.pf_vf" | head -n 1)"
  [ "$(getv ST "$p.pf_vf")" = UNAVAILABLE ] && SUMMARY="$SUMMARY UNAVAILABLE"
done
SUMMARY="$SUMMARY
/dev/dri:        $([ "$(getv ST dri.dev_nodes)" = VERIFIED ] && echo "present (see dri.dev_nodes)" || echo "UNAVAILABLE")
i915/xe module:  $([ "$(getv ST driver.lsmod)" = VERIFIED ] && echo "lsmod ran (see driver.lsmod)" || echo "UNAVAILABLE")
GuC/HuC (dmesg): $(getv ST firmware.dmesg)
Tools on PATH:  ${TOOLS_PRESENT:- none of clinfo vainfo intel_gpu_top sycl-ls ovms benchmark_app k3s}
Fields:          $N_VERIFIED VERIFIED, $N_UNAVAILABLE UNAVAILABLE
$SCOPE"

SUMMARY=$(redact "$SUMMARY")

JSON="{
  \"schema\": \"nervshield-inventory/1\",
  \"script_version\": $SCRIPT_VERSION,
  \"generated_utc\": \"$(json_esc "$GEN_UTC")\",
  \"redaction\": $([ "$REDACT" = 1 ] && echo true || echo false),
  \"runtime_probes\": $([ "$PROBES" = 1 ] && echo true || echo false),
  \"scope_statement\": \"$(json_esc "$SCOPE")\",
  \"summary\": {
    \"arc_gpu_status\": \"$(json_esc "$ARC_STATUS")\",
    \"verified_fields\": $N_VERIFIED,
    \"unavailable_fields\": $N_UNAVAILABLE
  },
  \"fields\": [
$FIELDS
  ]
}"

if [ -n "$OUT_DIR" ]; then
  mkdir -p "$OUT_DIR" || exit 1
  printf '%s\n' "$JSON" >"$OUT_DIR/inventory.json" || exit 1
  printf '%s\n' "$SUMMARY" >"$OUT_DIR/summary.txt" || exit 1
fi
printf '%s\n' "$JSON"
printf '%s\n' "$SUMMARY" >&2
exit 0
