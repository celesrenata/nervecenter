"""Property 9: file classification.

classify_file is openable iff size <= 2_097_152 and the content is valid UTF-8;
otherwise the exact size and detected type are reported.

Validates: Requirements 4.6
"""
from __future__ import annotations

from hypothesis import given, settings, strategies as st

from backend.workbench.validation import MAX_OPEN_BYTES, classify_file

LIMIT = 2_097_152


def _valid_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


contents = st.one_of(
    st.text(max_size=200).map(lambda s: s.encode("utf-8")),
    st.binary(max_size=200),
)

# Sizes cluster around the 2 MiB boundary plus arbitrary values.
sizes = st.one_of(
    st.integers(min_value=LIMIT - 2, max_value=LIMIT + 2),
    st.integers(min_value=0, max_value=LIMIT * 4),
)


def test_limit_constant():
    assert MAX_OPEN_BYTES == LIMIT


@settings(max_examples=200, deadline=None)
@given(data=contents)
def test_size_defaults_to_len(data: bytes):
    klass = classify_file(data)
    assert klass.size == len(data)
    assert klass.openable == _valid_utf8(data)
    assert klass.detected_type == ("text" if _valid_utf8(data) else "binary")


@settings(max_examples=300, deadline=None)
@given(data=contents, size=sizes)
def test_openable_iff_small_and_utf8(data: bytes, size: int):
    # Treat `data` as a full read when size == len(data); otherwise only the
    # content validity matters, so keep the prefix whole by sizing >= len.
    size = max(size, len(data))
    if size > len(data) and not _valid_utf8(data):
        # A prefix may legitimately end mid-character; covered separately.
        return
    klass = classify_file(data, size)
    expected_text = _valid_utf8(data)
    assert klass.size == size
    assert klass.detected_type == ("text" if expected_text else "binary")
    assert klass.openable == (size <= LIMIT and expected_text)


@settings(max_examples=100, deadline=None)
@given(data=st.binary(min_size=1, max_size=200).filter(lambda b: not _valid_utf8(b)))
def test_invalid_utf8_reports_exact_size_and_binary(data: bytes):
    klass = classify_file(data)
    assert not klass.openable
    assert klass.size == len(data)
    assert klass.detected_type == "binary"


@settings(max_examples=100, deadline=None)
@given(text=st.text(max_size=100), size=st.integers(min_value=LIMIT + 1, max_value=LIMIT * 8))
def test_oversize_text_refused_with_exact_size(text: str, size: int):
    klass = classify_file(text.encode("utf-8"), size)
    assert not klass.openable
    assert klass.size == size
    assert klass.detected_type == "text"


def test_boundary_exact_limit_is_openable():
    data = b"a" * LIMIT
    assert classify_file(data).openable
    over = classify_file(data + b"a")
    assert not over.openable
    assert over.size == LIMIT + 1
    assert over.detected_type == "text"
