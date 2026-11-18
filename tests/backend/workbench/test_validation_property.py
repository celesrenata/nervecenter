"""Property 2: validation rejects without side effects.

Validates: Requirements 2.3, 2.4, 2.7, 11.6
"""
from __future__ import annotations

import re
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st
from pydantic import ValidationError

from backend.workbench.validation import (
    ProjectIn,
    validate_git_url,
    validate_kind,
    validate_slug,
    validate_title,
)

_SLUG_ORACLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

slugs = st.one_of(
    st.from_regex(r"[a-z0-9][a-z0-9-]{0,62}", fullmatch=True),
    st.text(max_size=80),
    st.sampled_from(["", "-a", "A", "a_b", "a/b", "..", "a b", "a" * 64, "a\n"]),
)
titles = st.one_of(st.text(max_size=140), st.just(""), st.just("x" * 121))
kinds = st.one_of(st.sampled_from(["opencode", "menagerie"]), st.text(max_size=12))
git_urls = st.one_of(
    st.none(),
    st.builds(
        lambda s, h, p: f"{s}://{h}/{p}",
        st.sampled_from(["https", "ssh", "http", "ftp", "file", "git"]),
        st.from_regex(r"[a-z0-9.-]{0,20}", fullmatch=True),
        st.from_regex(r"[a-z0-9/_.-]{0,20}", fullmatch=True),
    ),
    st.text(max_size=40),
)


def _create(root: Path, data: dict) -> list[dict]:
    """Minimal create flow: validate first, touch disk and records only on success."""
    records: list[dict] = []
    try:
        p = ProjectIn(**data)
    except ValidationError:
        return records
    (root / p.slug).mkdir()
    records.append(p.model_dump())
    return records


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(slug=slugs, title=titles, kind=kinds, git_url=git_urls)
def test_validation_rejects_without_side_effects(tmp_path, slug, title, kind, git_url):
    expected_valid = (
        bool(_SLUG_ORACLE.fullmatch(slug))
        and 1 <= len(title) <= 120
        and kind in ("opencode", "menagerie")
        and (git_url is None or validate_git_url(git_url))
    )
    root = tmp_path / "roots"
    root.mkdir(exist_ok=True)
    before = set(root.iterdir())
    data = {"slug": slug, "title": title, "kind": kind, "git_url": git_url}

    try:
        ProjectIn(**data)
        accepted = True
    except ValidationError:
        accepted = False
    assert accepted == expected_valid

    if expected_valid:
        # clean up any prior example's directory so mkdir is deterministic
        target = root / slug
        if target.exists():
            target.rmdir()
        records = _create(root, data)
        assert len(records) == 1 and (root / slug).is_dir()
        (root / slug).rmdir()
    else:
        records = _create(root, data)
        assert records == []
        assert set(root.iterdir()) == before


@given(slug=st.text(max_size=80), title=st.text(max_size=140), kind=st.text(max_size=12))
def test_predicates_agree_with_model(slug, title, kind):
    assert validate_slug(slug) == bool(_SLUG_ORACLE.fullmatch(slug))
    assert validate_title(title) == (1 <= len(title) <= 120)
    assert validate_kind(kind) == (kind in ("opencode", "menagerie"))


@given(url=st.text(max_size=60))
def test_git_url_accepts_only_https_or_ssh_with_host(url):
    if validate_git_url(url):
        assert url.split("://", 1)[0] in ("https", "ssh")
        assert url == url.strip() and not any(c.isspace() for c in url)


def test_non_string_inputs_rejected():
    for bad in (None, 1, b"a", ["a"]):
        assert not validate_slug(bad)
        assert not validate_title(bad)
        assert not validate_kind(bad)
        assert not validate_git_url(bad)
