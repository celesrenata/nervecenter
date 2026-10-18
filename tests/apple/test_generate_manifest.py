"""Focused tests for the Apple distribution manifest fix.

Regression coverage for two defects:
  1. generate_manifest.py did not accept the `--artifact-root` flag the
     Fastfile passes, so argparse bailed (exit 2) and no manifest.json was
     written.
  2. The Fastfile invoked the script with `system(...)` and only warned when
     no manifest.json appeared, so the failure was swallowed.

These tests shell out to the real script (it is a standalone entry point,
not an importable package module) and assert a static property of the Fastfile.
"""

import json
import subprocess
import sys
from pathlib import Path

# Paths to the standalone script and Fastfile, relative to the repo root
# (this test lives at tests/apple/, so the root is two levels up).
MANIFEST_SCRIPT = Path(__file__).resolve().parent.parent.parent / "apple" / "Distribution" / "generate_manifest.py"
FASTFILE = Path(__file__).resolve().parent.parent.parent / "apple" / "Distribution" / "fastlane" / "Fastfile"


def _run(args):
    """Run the manifest script and return (returncode, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, str(MANIFEST_SCRIPT), *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_artifact_root_accepted_and_manifest_written(tmp_path):
    """--artifact-root must be a supported flag (the original regression)."""
    artifact_root = tmp_path / "artifacts"
    destination = tmp_path / "dist"
    artifact_root.mkdir()
    destination.mkdir()
    # Plant a recognizable artifact in the artifact-root (not in destination).
    (artifact_root / "NervePhone.ipa").write_bytes(b"phone-bytes")

    rc, out, err = _run(
        ["--destination", str(destination), "--artifact-root", str(artifact_root)]
    )

    assert rc == 0, f"expected exit 0, got {rc}\nstderr: {err}"
    manifest_path = destination / "manifest.json"
    assert manifest_path.exists(), "manifest.json was not written"

    manifest = json.loads(manifest_path.read_text())
    entries = {e["name"]: e for e in manifest["artifacts"]}
    # The file we planted is found in the artifact-root.
    phone = entries["NervePhone.ipa"]
    assert phone["present"] is True
    assert phone["bytes"] == len(b"phone-bytes")
    assert phone["sha256"]


def test_missing_artifacts_marked_not_present(tmp_path):
    """Artifacts that are absent must be recorded with present: false."""
    artifact_root = tmp_path / "artifacts"
    destination = tmp_path / "dist"
    artifact_root.mkdir()
    destination.mkdir()

    rc, out, err = _run(
        ["--destination", str(destination), "--artifact-root", str(artifact_root)]
    )
    assert rc == 0, f"expected exit 0, got {rc}\nstderr: {err}"

    manifest = json.loads((destination / "manifest.json").read_text())
    entries = {e["name"]: e for e in manifest["artifacts"]}
    for expected in (
        "NervePhone.ipa",
        "NerveWatch.ipa",
        "NerveWatchWidgetExtension.appex",
    ):
        assert entries[expected]["present"] is False
    # The widget extension ships embedded inside NerveWatch.ipa, never as a
    # standalone artifact.
    assert entries["NerveWatchWidgetExtension.appex"]["embedded"] is True


def test_artifact_root_defaults_to_destination(tmp_path):
    """Without --artifact-root, the destination is scanned for contents."""
    destination = tmp_path / "dist"
    destination.mkdir()
    (destination / "NervePhone.ipa").write_bytes(b"phone-bytes")

    rc, out, err = _run(["--destination", str(destination)])
    assert rc == 0, f"expected exit 0, got {rc}\nstderr: {err}"

    manifest = json.loads((destination / "manifest.json").read_text())
    entries = {e["name"]: e for e in manifest["artifacts"]}
    assert entries["NervePhone.ipa"]["present"] is True


def test_nonexistent_artifact_root_fails_hard(tmp_path):
    """A bad --artifact-root must exit non-zero (so fastlane can fail hard)."""
    destination = tmp_path / "dist"
    destination.mkdir()

    rc, out, err = _run(
        ["--destination", str(destination), "--artifact-root", str(tmp_path / "nope")]
    )
    assert rc != 0
    assert "artifact-root does not exist" in err


def test_fastfile_fails_hard_on_manifest_step():
    """The Fastfile must not swallow the manifest generation failure."""
    text = FASTFILE.read_text()

    # The manifest step must use fastlane's `sh` with :raise => true so a
    # non-zero exit (e.g. the argparse error) raises SystemCommandFailure and
    # aborts the lane, rather than the old `system(...)` + UI.important warning.
    assert ":raise => true" in text, "expected :raise => true on the sh call"
    # The old non-failing warning-only path must be gone.
    assert "UI.important(\"generate_manifest.py did not produce" not in text
    # The manifest must be verified to actually exist after generation.
    assert "raise" in text and "manifest.json" in text
