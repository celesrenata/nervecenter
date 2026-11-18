"""Unit tests for Path_Guard and slug validation.

**Validates: Requirements 11.6, 7.2, 2.3**
"""
from __future__ import annotations

import pytest

from backend.workbench.paths import PathGuard, PathNotPermitted
from backend.workbench.validation import validate_slug


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    (r / "sub").mkdir(parents=True)
    (r / "sub" / "file.txt").write_text("x")
    return r


@pytest.mark.parametrize("rel", ["..", "../x", "sub/../../x", "a\\..\\b", "%2e%2e/x"])
def test_rejects_parent_segments(root, rel):
    with pytest.raises(PathNotPermitted):
        PathGuard.resolve(root, rel)


@pytest.mark.parametrize("rel", ["/etc/passwd", "\\windows", "C:\\x", "C:x"])
def test_rejects_absolute_paths(root, rel):
    with pytest.raises(PathNotPermitted):
        PathGuard.resolve(root, rel)


@pytest.mark.parametrize("rel", ["a\x00b", "\x00", "a%00b"])
def test_rejects_nul_paths(root, rel):
    with pytest.raises(PathNotPermitted):
        PathGuard.resolve(root, rel)


def test_rejects_symlink_escape(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s")
    (root / "link_out").symlink_to(outside)
    with pytest.raises(PathNotPermitted):
        PathGuard.resolve(root, "link_out/secret")
    with pytest.raises(PathNotPermitted):
        PathGuard.resolve(root, "link_out")
    with pytest.raises(PathNotPermitted):
        PathGuard.resolve(root, "link_out/new", create_leaf=True)


def test_accepts_contained_path(root):
    assert PathGuard.resolve(root, "sub/file.txt") == (root / "sub" / "file.txt").resolve()
    assert PathGuard.resolve(root, "") == root.resolve()


@pytest.mark.parametrize("slug", ["a", "abc", "my-project", "0abc", "a" * 63])
def test_slug_accepts_valid(slug):
    assert validate_slug(slug)


@pytest.mark.parametrize(
    "slug", ["", "a" * 64, "-abc", "Abc", "a_b", "a b", "a/b", "a.b", "é", "a\n", None, 5]
)
def test_slug_rejects_invalid(slug):
    assert not validate_slug(slug)
