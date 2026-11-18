"""Property 17: IDE routing isolation.

**Validates: Requirements 5.7**
"""
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings, strategies as st

from backend.workbench.ide import AmbiguousSlug, IdeInstance, resolve_upstream

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_slugs = st.sampled_from(["a", "b", "c", "d", "e"])
_kinds = st.sampled_from(["work", "personal"])


@st.composite
def _instances(draw):
    """Instances with unique (slug, kind) pairs and a unique pod IP each."""
    pairs = draw(st.lists(st.tuples(_slugs, _kinds), unique=True, max_size=10))
    return [
        IdeInstance(
            slug=s,
            kind=k,
            pod_name=f"ide-{n}",
            pod_ip=f"10.0.0.{n}",
            last_traffic_at=_NOW,
        )
        for n, (s, k) in enumerate(pairs)
    ]


@settings(max_examples=300, deadline=None)
@given(slug=_slugs, instances=_instances())
def test_resolves_only_matching_slug_or_raises_or_none(slug, instances):
    matches = [i for i in instances if i.slug == slug]
    kinds = {i.kind for i in matches}
    if len(kinds) > 1:
        with pytest.raises(AmbiguousSlug):
            resolve_upstream(slug, instances)
        return
    result = resolve_upstream(slug, instances)
    if not matches:
        assert result is None
    else:
        assert result is not None
        assert result.slug == slug
        assert result.pod_ip == matches[0].pod_ip
        others = {i.pod_ip for i in instances if i.slug != slug}
        assert result.pod_ip not in others
