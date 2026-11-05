"""NerveShield task 3.1 — read-only Arc host inventory script (tools/nerve_shield).

These tests prove SCRIPT BEHAVIOUR only (read-only, honest UNAVAILABLE marking,
redaction, PF/VF classification) against fake tool shims in a sandboxed PATH.
They do NOT prove anything about real Intel Arc hardware: task 3.1 stays
NOT VERIFIED ON HARDWARE until the owner runs the script on the four GTi14 hosts.

Subprocess use is allowed in this test module only (the script is a shell tool).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "nerve_shield" / "inventory_arc_host.sh"

BASH = shutil.which("bash")
# Real tools the script is allowed to see in the sandboxed PATH.
REAL_TOOLS = (
    "awk",
    "sed",
    "grep",
    "tr",
    "cat",
    "ls",
    "readlink",
    "uname",
    "date",
    "head",
    "tail",
    "cut",
    "sort",
    "wc",
    "basename",
    "dirname",
    "mkdir",
)

HOST_CANARY = "canaryhost-7731"
USER_CANARY = "canaryuser7731"
IPV4_CANARY = "192.168.77.31"
IPV6_CANARY = "fe80::1234:5678"
MAC_CANARY = "aa:bb:cc:dd:ee:f7"
SERIAL_CANARY = "SN-CANARY-9911"
CANARIES = (HOST_CANARY, USER_CANARY, IPV4_CANARY, IPV6_CANARY, MAC_CANARY, SERIAL_CANARY)

LSPCI_ONE_VF = (
    "0000:00:02.0 VGA compatible controller [0300]: Intel Corporation "
    "Meteor Lake-P [Intel Arc Graphics] [8086:7d55] (rev 08)"
)
LSPCI_TWO = LSPCI_ONE_VF + (
    "\n0000:03:00.0 Display controller [0380]: Intel Corporation Fake PF [8086:7d56] (rev 01)"
)

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _write_exec(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.home = root / "home" / USER_CANARY
        self.cwd = root / "cwd"
        self.tmp = root / "tmp"
        self.sys = root / "sys"
        self.dev = root / "dev"
        self.out = root / "out"  # NOT created: the script must create it itself
        for d in (self.bin, self.home, self.cwd, self.tmp, self.sys, self.dev):
            d.mkdir(parents=True)
        for tool in REAL_TOOLS:
            real = shutil.which(tool)
            if real:
                (self.bin / tool).symlink_to(real)

    def shim(self, name: str, body: str) -> None:
        _write_exec(self.bin / name, body)

    def snapshot(self) -> dict[str, str]:
        """Digest of every file/dir/symlink outside the (allowed) output dir."""
        snap: dict[str, str] = {}
        for p in sorted(self.root.rglob("*")):
            if p == self.out or self.out in p.parents:
                continue
            rel = str(p.relative_to(self.root))
            if p.is_symlink():
                snap[rel] = "L:" + os.readlink(p)
            elif p.is_dir():
                snap[rel] = "D"
            else:
                snap[rel] = "F:" + hashlib.sha256(p.read_bytes()).hexdigest()
        return snap

    def run(self, *args: str, extra_env: dict[str, str] | None = None):
        env = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "USER": USER_CANARY,
            "LOGNAME": USER_CANARY,
            "TMPDIR": str(self.tmp),
            "NS_INV_SYSROOT": str(self.sys),
            "NS_INV_DEVROOT": str(self.dev),
        }
        env.update(extra_env or {})
        return subprocess.run(
            [BASH, str(SCRIPT), *args],
            env=env,
            cwd=self.cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )


@pytest.fixture()
def sb(tmp_path: Path) -> Sandbox:
    box = Sandbox(tmp_path)
    box.shim("hostname", f"echo {HOST_CANARY}\n")
    box.shim("id", f"echo {USER_CANARY}\n")
    return box


def _fields(stdout: str) -> dict[str, dict]:
    doc = json.loads(stdout)
    return {f["id"]: f for f in doc["fields"]}


def _install_leaky_tools(sb: Sandbox) -> None:
    leak = f"{HOST_CANARY} {IPV4_CANARY} {IPV6_CANARY} {MAC_CANARY} user {USER_CANARY}"
    sb.shim(
        "lspci",
        'case "$*" in\n'
        f'  "-D -nn") printf "%s\\n" "{LSPCI_ONE_VF}" "bridge {leak}" '
        f'"Serial Number: {SERIAL_CANARY}";;\n'
        f'  *) printf "Kernel driver in use: xe\\n{leak}\\n";;\n'
        "esac\n",
    )
    sb.shim(
        "dmesg",
        f'echo "xe 0000:00:02.0: [drm] GuC firmware loaded {leak}"\n'
        f'echo "xe 0000:00:02.0: [drm] HuC firmware serial: {SERIAL_CANARY}"\n'
        'echo "unrelated line"\n',
    )


# --------------------------------------------------------------------------- #
# Static / syntax
# --------------------------------------------------------------------------- #


def test_script_exists_and_parses() -> None:
    assert SCRIPT.is_file()
    r = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_shellcheck_clean_if_available() -> None:
    sc = shutil.which("shellcheck")
    if not sc:
        pytest.skip("shellcheck not installed")
    r = subprocess.run([sc, str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


FORBIDDEN = (
    "sudo",
    "su",
    "curl",
    "wget",
    "ssh",
    "scp",
    "nc",
    "ncat",
    "ping",
    "telnet",
    "rm",
    "mv",
    "cp",
    "tee",
    "chmod",
    "chown",
    "dd",
    "ln",
    "kubectl",
    "nix-env",
    "nixos-rebuild",
    "apt",
    "apt-get",
    "pip",
    "pip3",
    "systemctl",
    "modprobe",
    "rmmod",
    "insmod",
    "mount",
    "umount",
)


def test_script_source_has_no_mutating_or_network_commands() -> None:
    code_lines = [ln for ln in SCRIPT.read_text().splitlines() if not ln.lstrip().startswith("#")]
    # Drop quoted text (reasons/notes), keep command words.
    stripped = [re.sub(r"'[^']*'|\"[^\"]*\"", " ", ln) for ln in code_lines]
    pattern = re.compile(r"(?<![\w./$-])(" + "|".join(map(re.escape, FORBIDDEN)) + r")(?![\w-])")
    hits = [(i, ln) for i, ln in enumerate(stripped) if pattern.search(ln)]
    assert not hits, hits


# --------------------------------------------------------------------------- #
# Read-only
# --------------------------------------------------------------------------- #


def test_read_only_no_writes_outside_output_dir(sb: Sandbox) -> None:
    _install_leaky_tools(sb)
    dev = sb.sys / "bus" / "pci" / "devices" / "0000:00:02.0"
    dev.mkdir(parents=True)
    (dev / "sriov_totalvfs").write_text("7\n")
    (dev / "sriov_numvfs").write_text("0\n")
    (sb.dev / "dri").mkdir()
    (sb.dev / "dri" / "card0").write_text("")
    before = sb.snapshot()

    r = sb.run("--out-dir", str(sb.out))

    assert r.returncode == 0, r.stderr
    assert sb.snapshot() == before, "script modified files outside the output dir"
    assert sorted(p.name for p in sb.out.iterdir()) == ["inventory.json", "summary.txt"]
    json.loads((sb.out / "inventory.json").read_text())


def test_default_mode_writes_nothing_at_all(sb: Sandbox) -> None:
    before = sb.snapshot()
    r = sb.run()
    assert r.returncode == 0, r.stderr
    assert sb.snapshot() == before
    assert not sb.out.exists()
    json.loads(r.stdout)  # JSON on stdout
    assert "NOT VERIFIED ON HARDWARE" in r.stderr  # human summary on stderr


def test_refuses_to_overwrite_existing_output(sb: Sandbox) -> None:
    sb.out.mkdir()
    (sb.out / "inventory.json").write_text("keep me")
    r = sb.run("--out-dir", str(sb.out))
    assert r.returncode != 0
    assert (sb.out / "inventory.json").read_text() == "keep me"


# --------------------------------------------------------------------------- #
# Honest UNAVAILABLE marking
# --------------------------------------------------------------------------- #


def test_missing_tools_marked_unavailable_never_invented(sb: Sandbox) -> None:
    r = sb.run()
    assert r.returncode == 0, r.stderr
    f = _fields(r.stdout)

    for tool in ("ovms", "clinfo", "vainfo", "intel_gpu_top", "k3s", "lspci"):
        rec = f[f"tool.{tool}"]
        assert rec["status"] == "UNAVAILABLE", tool
        assert rec["value"] == ""
        assert tool in rec["command"]
    for fid in ("version.ovms", "version.clinfo_list", "pci.display_lspci", "firmware.dmesg"):
        assert f[fid]["status"] == "UNAVAILABLE", fid
        assert f[fid]["value"] == ""

    for rec in f.values():
        assert rec["status"] in {"VERIFIED", "UNAVAILABLE"}
        assert rec["command"].strip(), rec["id"]
        if rec["status"] == "UNAVAILABLE":
            assert rec["value"] == ""
            assert rec["reason"].strip(), rec["id"]


def test_no_lspci_is_undetermined_not_a_claim_of_no_gpu(sb: Sandbox) -> None:
    r = sb.run()
    doc = json.loads(r.stdout)
    assert doc["summary"]["arc_gpu_status"].startswith("UNDETERMINED")
    assert "NOT VERIFIED ON HARDWARE" in doc["scope_statement"]
    assert "UNDETERMINED" in r.stderr


def test_lspci_ran_but_no_display_device_is_not_detected(sb: Sandbox) -> None:
    sb.shim("lspci", 'echo "0000:00:1f.0 ISA bridge [0601]: Fake [8086:1111]"\n')
    doc = json.loads(sb.run().stdout)
    assert doc["summary"]["arc_gpu_status"].startswith("NOT DETECTED")
    f = {x["id"]: x for x in doc["fields"]}
    assert f["pci.display_lspci"]["status"] == "VERIFIED"
    assert f["pci.display_lspci"]["value"] == ""


def test_root_required_field_degrades_with_hint(sb: Sandbox) -> None:
    sb.shim(
        "dmesg", 'echo "dmesg: read kernel buffer failed: Operation not permitted" >&2\nexit 1\n'
    )
    r = sb.run()
    assert r.returncode == 0
    rec = _fields(r.stdout)["firmware.dmesg"]
    assert rec["status"] == "UNAVAILABLE"
    assert "root" in rec["reason"].lower()
    assert "dmesg" in rec["command"]


def test_dmesg_firmware_lines_verified_when_readable(sb: Sandbox) -> None:
    _install_leaky_tools(sb)
    rec = _fields(sb.run().stdout)["firmware.dmesg"]
    assert rec["status"] == "VERIFIED"
    assert "GuC firmware loaded" in rec["value"]
    assert "unrelated line" not in rec["value"]


# --------------------------------------------------------------------------- #
# PF vs VF
# --------------------------------------------------------------------------- #


def _mk_dev(sb: Sandbox, bdf: str) -> Path:
    d = sb.sys / "bus" / "pci" / "devices" / bdf
    d.mkdir(parents=True)
    return d


def test_pf_vs_vf_classification_from_sysfs(sb: Sandbox) -> None:
    sb.shim(
        "lspci",
        f'case "$*" in\n  "-D -nn") cat <<\'EOF\'\n{LSPCI_TWO}\nEOF\n;;\n  *) echo ok;;\nesac\n',
    )
    vf = _mk_dev(sb, "0000:00:02.0")
    (sb.sys / "bus" / "pci" / "devices" / "0000:00:02.1").mkdir()
    (vf / "physfn").symlink_to("../0000:00:02.1")
    pf = _mk_dev(sb, "0000:03:00.0")
    (pf / "sriov_totalvfs").write_text("7\n")
    (pf / "sriov_numvfs").write_text("0\n")

    f = _fields(sb.run().stdout)

    assert f["pci.0000_00_02_0.pf_vf"]["value"].startswith("VF")
    assert f["pci.0000_03_00_0.pf_vf"]["value"].startswith("PF")
    assert "totalvfs=7" in f["pci.0000_03_00_0.pf_vf"]["value"]
    assert f["pci.0000_03_00_0.sriov_numvfs"]["value"] == "0"
    ids = f["pci.display_ids"]["value"]
    assert "0000:00:02.0 8086:7d55" in ids and "0000:03:00.0 8086:7d56" in ids


def test_pf_vf_unavailable_when_sysfs_missing(sb: Sandbox) -> None:
    sb.shim("lspci", f'echo "{LSPCI_ONE_VF}"\n')
    rec = _fields(sb.run().stdout)["pci.0000_00_02_0.pf_vf"]
    assert rec["status"] == "UNAVAILABLE"
    assert rec["value"] == ""


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_default_redaction_removes_all_canaries(sb: Sandbox) -> None:
    _install_leaky_tools(sb)
    r = sb.run("--out-dir", str(sb.out))
    assert r.returncode == 0, r.stderr
    blobs = (
        r.stdout
        + r.stderr
        + (sb.out / "inventory.json").read_text()
        + (sb.out / "summary.txt").read_text()
    )
    for canary in CANARIES:
        assert canary not in blobs, canary
    doc = json.loads((sb.out / "inventory.json").read_text())
    assert doc["redaction"] is True
    # Useful hardware facts survive redaction.
    assert "8086:7d55" in blobs and "0000:00:02.0" in blobs
    assert "GuC firmware loaded" in blobs


def test_no_redact_flag_shows_shims_really_leak(sb: Sandbox) -> None:
    """Control: proves the previous test's canaries come from the tools, not luck."""
    _install_leaky_tools(sb)
    r = sb.run("--no-redact")
    blob = r.stdout
    assert json.loads(blob)["redaction"] is False
    for canary in (HOST_CANARY, IPV4_CANARY, MAC_CANARY):
        assert canary in blob, canary


# --------------------------------------------------------------------------- #
# Runtime probes
# --------------------------------------------------------------------------- #


def test_no_runtime_probes_does_not_execute_runtime_tools(sb: Sandbox, tmp_path: Path) -> None:
    marker = tmp_path.parent / (tmp_path.name + "-ovms-ran")
    sb.shim("ovms", f'echo ran >> "{marker}"\necho "OpenVINO Model Server 2099.1"\n')
    try:
        r = sb.run("--no-runtime-probes")
        f = _fields(r.stdout)
        assert not marker.exists(), "ovms executed despite --no-runtime-probes"
        assert f["tool.ovms"]["status"] == "VERIFIED"  # presence only
        assert f["version.ovms"]["status"] == "UNAVAILABLE"
        assert "skipped" in f["version.ovms"]["reason"].lower()

        r = sb.run()
        f = _fields(r.stdout)
        assert marker.exists()
        assert f["version.ovms"]["status"] == "VERIFIED"
        assert "2099.1" in f["version.ovms"]["value"]
    finally:
        marker.unlink(missing_ok=True)
