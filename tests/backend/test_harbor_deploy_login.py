"""deployments/harbor/deploy.sh — docker login decision (sandboxed).

Extracts only the ``docker_creds_present`` function text from the script and
runs it with bash against temp DOCKER_CONFIG dirs. No docker, network, cluster
or real docker config is touched. Subprocess use is allowed in this module only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deployments" / "harbor" / "deploy.sh"
BASH = shutil.which("bash")
REGISTRY = "registry.celestium.life"

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _function_text() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^docker_creds_present\(\) \{\n.*?^\}\n", text, re.M | re.S)
    assert match, "docker_creds_present not found in deploy.sh"
    return match.group(0)


def _run(tmp_path: Path, config: object | None) -> subprocess.CompletedProcess[str]:
    cfg_dir = tmp_path / "docker"
    cfg_dir.mkdir()
    if config is not None:
        (cfg_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    script = f'REGISTRY="{REGISTRY}"\n{_function_text()}\ndocker_creds_present\n'
    return subprocess.run(
        [BASH, "-c", script],
        env={"PATH": os.environ["PATH"], "DOCKER_CONFIG": str(cfg_dir), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_auths_entry_for_registry_skips_login(tmp_path: Path) -> None:
    result = _run(tmp_path, {"auths": {REGISTRY: {"auth": "c2VjcmV0LXZhbHVl"}}})
    assert result.returncode == 0
    assert "c2VjcmV0" not in result.stdout + result.stderr


def test_creds_store_skips_login(tmp_path: Path) -> None:
    assert _run(tmp_path, {"credsStore": "osxkeychain"}).returncode == 0


def test_cred_helpers_skips_login(tmp_path: Path) -> None:
    assert _run(tmp_path, {"credHelpers": {REGISTRY: "ecr-login"}}).returncode == 0


def test_no_creds_keeps_interactive_login(tmp_path: Path) -> None:
    assert _run(tmp_path, {"auths": {"other.example.com": {"auth": "x"}}}).returncode == 1


def test_missing_or_invalid_config_keeps_interactive_login(tmp_path: Path) -> None:
    assert _run(tmp_path, None).returncode == 1
    bad = tmp_path / "bad"
    bad.mkdir()
    assert _run(bad, ["not", "a", "dict"]).returncode == 1


def test_script_wiring_preserves_env_and_skip_precedence() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    env_i = text.index('if [[ -n "${HARBOR_USER:-}" && -n "${HARBOR_PASS:-}" ]]; then')
    skip_i = text.index('elif [[ "${SKIP_DOCKER_LOGIN:-}" == "1" ]] || docker_creds_present; then')
    interactive_i = text.index('docker login "${REGISTRY}" -u "${HARBOR_USER}"\nfi')
    assert env_i < skip_i < interactive_i
