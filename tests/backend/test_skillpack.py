"""NerveCenter skill pack generator (NS-ADR-112)."""

from __future__ import annotations

import json
import re

from backend.agents.registry import _create_native_manifests
from backend.agents.skillpack import (
    DOMAIN,
    FEATURE_AGENTS,
    MERGED_INTO,
    build_pack,
    manifest_text,
    pack_hashes,
)

_FRONT = re.compile(
    r"\A---\nname: (?P<name>[a-z0-9-]+)\ndescription: (?P<desc>\".+\")\n(?:.+\n)*?---\n"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_KEYLIKE = re.compile(r"\b(?:sk|pk|ghp|xox[bp])[-_][A-Za-z0-9_-]{16,}|[A-Za-z0-9+/=_-]{40,}")
_ADDRESS = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|https?://", re.IGNORECASE)


def test_count_equals_manifests_plus_features() -> None:
    pack = build_pack()
    assert len(pack) == len(_create_native_manifests()) + len(FEATURE_AGENTS) - len(MERGED_INTO)
    assert {"finance", "mental-health", "health", "researcher", "coding-handoff"} <= set(pack)


def test_research_skills_are_merged_into_one() -> None:
    pack = build_pack()
    assert "research-agent" not in pack
    assert [slug for slug in pack if "research" in slug] == ["researcher"]
    researcher = pack["researcher"]
    assert "- `fetch`" in researcher and "Find and synthesize information" in researcher


def test_frontmatter_is_valid_and_names_are_unique() -> None:
    names = []
    for slug, text in build_pack().items():
        match = _FRONT.match(text)
        assert match, slug
        names.append(match["name"])
        assert match["name"] == f"{DOMAIN}-{slug}"
        assert json.loads(match["desc"]).strip()
    assert len(names) == len(set(names))


def test_output_is_deterministic() -> None:
    assert build_pack() == build_pack()
    assert pack_hashes() == pack_hashes(build_pack())
    assert manifest_text() == manifest_text()
    assert json.loads(manifest_text())["skills"] == pack_hashes()


def test_no_personal_data_addresses_or_keys() -> None:
    for slug, text in build_pack().items():
        for pattern in (_EMAIL, _PHONE, _KEYLIKE, _ADDRESS):
            assert not pattern.search(text), (slug, pattern.pattern)
