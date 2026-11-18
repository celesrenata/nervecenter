"""Build-file tests for the Workbench IDE image (Requirements 5.2, 8.4)."""

from __future__ import annotations

import re
from pathlib import Path

CONTEXT = Path(__file__).resolve().parents[3] / "deployments" / "docker" / "workbench-ide"
DOCKERFILE = CONTEXT / "Dockerfile"

EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
KEY_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(anthropic|openai)[a-z_]*api[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)\b(ANTHROPIC|OPENAI)_API_KEY\s*=\s*\S+"),
]


def _text() -> str:
    return DOCKERFILE.read_text()


def test_code_server_pinned_to_exact_version() -> None:
    text = _text()
    m = re.search(r"^ARG CODE_SERVER_VERSION=(\S+)$", text, re.M)
    assert m, "CODE_SERVER_VERSION must have an explicit default"
    assert EXACT_VERSION.match(m.group(1)), m.group(1)
    froms = re.findall(r"^FROM\s+(\S+)", text, re.M)
    assert froms
    for image in froms:
        assert not image.endswith(":latest")
        assert ":" in image, f"unpinned base image {image}"
    assert "${CODE_SERVER_VERSION}" in text


def test_extension_version_is_required_exact_input() -> None:
    text = _text()
    # No default for the extension args: a default could be a range or latest.
    assert re.search(r"^ARG MENAGERIE_VSIX\s*$", text, re.M)
    assert re.search(r"^ARG MENAGERIE_VERSION\s*$", text, re.M)
    assert "latest" not in text.lower().replace("no provider", "")
    assert not re.search(r"--install-extension\s+\S*@", text)
    assert not re.search(r"[\^~]\d|>=|<=", text)


def test_build_fails_without_vsix_args() -> None:
    text = _text()
    for arg in ("MENAGERIE_VSIX", "MENAGERIE_VERSION"):
        guard = re.search(
            rf'^RUN test -n "\$\{{{arg}\}}" \|\| \{{.*exit 1;? \}}', text, re.M
        )
        assert guard, f"missing RUN test -n guard for {arg}"


def test_no_provider_api_key_in_build_context() -> None:
    files = [p for p in CONTEXT.rglob("*") if p.is_file()]
    assert DOCKERFILE in files
    for path in files:
        content = path.read_text(errors="ignore")
        for pattern in KEY_PATTERNS:
            assert not pattern.search(content), f"{path.name} matches {pattern.pattern}"
