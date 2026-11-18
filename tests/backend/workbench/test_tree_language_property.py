"""Property 10: tree ordering and language selection.

**Validates: Requirements 4.1, 4.2**
"""

from __future__ import annotations

from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.workbench.validation import LANGUAGES, language_for, sort_tree

_names = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="/"),
    min_size=1,
    max_size=12,
)
_entries = st.lists(
    st.fixed_dictionaries({"name": _names, "is_dir": st.booleans()}), max_size=30
)


def _key(e: dict) -> tuple:
    return (e["name"], e["is_dir"])


@settings(max_examples=100)
@given(_entries)
def test_sort_tree_dirs_first_then_name_ascending(entries):
    out = sort_tree(entries)
    assert Counter(map(_key, out)) == Counter(map(_key, entries))
    assert len(out) == len(entries)
    flags = [e["is_dir"] for e in out]
    assert flags == sorted(flags, reverse=True)  # all dirs before files
    for group in (True, False):
        names = [e["name"] for e in out if e["is_dir"] is group]
        assert names == sorted(names)
    assert sort_tree(out) == out  # idempotent


_dirs = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="."),
    max_size=10,
)
_stems = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="./"),
    min_size=1,
    max_size=10,
)


@given(_dirs, _stems, st.sampled_from(sorted(LANGUAGES)))
def test_language_for_known_extension_is_deterministic(d, stem, ext):
    path = f"{d.replace('/', '_')}/{stem}{ext}"
    assert language_for(path) == LANGUAGES[ext]
    assert language_for(path) == language_for(path)
    assert language_for(path.upper()) == language_for(path.upper())


@given(_stems, st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=8))
def test_language_for_unknown_extension_is_plaintext(stem, ext):
    if f".{ext}" in LANGUAGES or stem.lower() == "dockerfile":
        return
    assert language_for(f"{stem}.{ext}") == "plaintext"
