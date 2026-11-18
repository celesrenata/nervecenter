"""Property 1: Path_Guard containment.

Feature: opencode-menagerie-workbench, Property 1: Path_Guard containment
**Validates: Requirements 4.12, 7.1, 7.2, 7.3, 11.6**
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backend.workbench.paths import PathGuard, PathNotPermitted

_NAMES = ["a", "sub", "link_in", "link_out", "link_abs", "loop", "file.txt", "outside", "dir"]

_segment = st.one_of(
    st.sampled_from(_NAMES),
    st.sampled_from(["..", ".", "", "%2e%2e", "%2E%2E", "%2f", "%5c", "%00", "%2e", "\x00"]),
    st.text(max_size=12),
)
_prefix = st.sampled_from(["", "", "", "/", "\\", "C:", "C:\\", "/etc/", "//"])
_sep = st.sampled_from(["/", "/", "\\", "%2f"])


@st.composite
def _rel_paths(draw):
    segs = draw(st.lists(_segment, max_size=8))
    sep = draw(_sep)
    rel = draw(_prefix) + sep.join(segs)
    kind = draw(st.integers(0, 12))
    if kind == 0:
        rel = rel + "x" * 4097
    elif kind == 1:
        rel = draw(st.text(max_size=4200))
    return rel


def _snapshot(*bases: Path) -> dict[str, tuple]:
    snap: dict[str, tuple] = {}
    for base in bases:
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            for name in dirnames + filenames:
                p = os.path.join(dirpath, name)
                st_ = os.lstat(p)
                content = None
                if os.path.isfile(p) and not os.path.islink(p):
                    with open(p, "rb") as fh:
                        content = fh.read()
                snap[p] = (st_.st_mode, st_.st_size, st_.st_mtime_ns,
                           os.readlink(p) if os.path.islink(p) else None, content)
    return snap


def _build(tmp: Path) -> tuple[Path, Path]:
    root = tmp / "root"
    outside = tmp / "outside"
    (root / "a" / "sub").mkdir(parents=True)
    (root / "dir").mkdir()
    outside.mkdir()
    (root / "file.txt").write_text("in")
    (root / "a" / "sub" / "file.txt").write_text("in2")
    (outside / "file.txt").write_text("secret")
    os.symlink(root / "a", root / "link_in")
    os.symlink(outside, root / "link_out")
    os.symlink(outside / "file.txt", root / "a" / "link_out")
    os.symlink("/etc", root / "link_abs")
    os.symlink(root / "loop", root / "loop")
    return root, outside


def _inside(real_root: str, p: Path) -> bool:
    real = os.path.realpath(p)
    return os.path.commonpath([real_root, real]) == real_root


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(rel=_rel_paths(), create_leaf=st.booleans())
def test_resolve_contained_or_raises_without_side_effects(rel, create_leaf):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(os.path.realpath(t))
        root, outside = _build(tmp)
        real_root = os.path.realpath(root)
        before = _snapshot(tmp)
        try:
            result = PathGuard.resolve(root, rel, create_leaf=create_leaf)
        except PathNotPermitted:
            assert _snapshot(tmp) == before
            return
        assert _inside(real_root, result), (rel, result)
        assert _snapshot(tmp) == before


@settings(max_examples=50, deadline=None)
@given(rel=st.sampled_from([
    "../outside/file.txt", "link_out/file.txt", "a/link_out", "%2e%2e/outside",
    "a/%2e%2e/%2e%2e/outside", "/etc/passwd", "a\x00b", "link_abs/passwd",
    "a%2fb", "..\\outside", "C:\\Windows", "x" * 4097,
]))
def test_known_escapes_are_rejected(rel):
    with tempfile.TemporaryDirectory() as t:
        root, _ = _build(Path(os.path.realpath(t)))
        try:
            result = PathGuard.resolve(root, rel)
        except PathNotPermitted:
            return
        assert _inside(os.path.realpath(root), result), (rel, result)
