"""Property 22: Model locality and confirmation.

**Validates: Requirements 8.1, 8.5, 8.6**
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.handoffs.central import OpenCodeError
from backend.inference.local_first import LOCAL_PREFIXES, is_local
from backend.workbench.sessions import (
    SessionError,
    SessionService,
    build_opencode_config,
    model_needs_confirm,
)
from backend.workbench.validation import needs_nonlocal_confirm

_tail = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_.:/"),
    min_size=1,
    max_size=30,
)
_local = st.builds(lambda p, t: p + t, st.sampled_from(LOCAL_PREFIXES), _tail)
_nonlocal = st.builds(
    lambda p, t: p + t,
    st.sampled_from(["auto/", "cloud/", "hybrid/", "openai/", "anthropic/", "gpt-", "x"]),
    _tail,
).filter(lambda m: not is_local(m))
_provider_prefix = st.sampled_from(["", "omniroute/"])


class _FailingClient:
    """Stands in for opencode; any call means the confirmation gate let the request through."""

    def __init__(self) -> None:
        self.calls = 0

    async def create_session(self, **_: Any) -> str:
        self.calls += 1
        raise OpenCodeError("down")


def _service() -> tuple[SessionService, _FailingClient]:
    client = _FailingClient()
    svc = SessionService(None, client, usage=SimpleNamespace())  # type: ignore[arg-type]

    async def _project(slug: str) -> Any:
        return SimpleNamespace(
            id=1, slug=slug, title=slug, kind="opencode", push_approved=False
        )

    svc._project = _project  # type: ignore[method-assign]
    return svc, client


def _create(model: str, small: str, confirm: bool) -> tuple[str | None, int]:
    svc, client = _service()

    async def run() -> str | None:
        try:
            await svc.create("p", model=model, small_model=small, confirm_nonlocal=confirm)
        except SessionError as exc:
            return exc.code
        return None

    return asyncio.run(run()), client.calls


@settings(max_examples=100, deadline=None)
@given(model=st.one_of(_local, _nonlocal, st.none()), confirmed=st.booleans())
def test_needs_confirm_iff_nonlocal_and_unconfirmed(model: str | None, confirmed: bool) -> None:
    expected = model is not None and not is_local(model) and not confirmed
    assert needs_nonlocal_confirm(model, confirmed) is expected


@settings(max_examples=100, deadline=None)
@given(model=_local, prefix=_provider_prefix, confirmed=st.booleans())
def test_local_never_needs_confirm(model: str, prefix: str, confirmed: bool) -> None:
    assert model_needs_confirm(prefix + model, confirmed) is False


@settings(max_examples=100, deadline=None)
@given(model=_nonlocal, prefix=_provider_prefix)
def test_nonlocal_needs_confirm_only_when_unconfirmed(model: str, prefix: str) -> None:
    assert model_needs_confirm(prefix + model, False) is True
    assert model_needs_confirm(prefix + model, True) is False


@settings(max_examples=50, deadline=None)
@given(model=_nonlocal, small=st.one_of(_local, _nonlocal), swap=st.booleans())
def test_create_nonlocal_declined_starts_no_session(model: str, small: str, swap: bool) -> None:
    pair = (small, model) if swap else (model, small)
    code, calls = _create(*pair, confirm=False)
    assert code == "nonlocal_confirm_required"
    assert calls == 0


@settings(max_examples=50, deadline=None)
@given(model=st.one_of(_local, _nonlocal), small=st.one_of(_local, _nonlocal))
def test_create_confirmed_passes_gate(model: str, small: str) -> None:
    code, calls = _create(model, small, confirm=True)
    assert code == "opencode_unavailable"
    assert calls == 1


@settings(max_examples=50, deadline=None)
@given(model=_local, small=_local)
def test_create_local_needs_no_confirmation(model: str, small: str) -> None:
    code, calls = _create(model, small, confirm=False)
    assert code == "opencode_unavailable"
    assert calls == 1


@settings(max_examples=100, deadline=None)
@given(
    model=st.one_of(_local, _nonlocal, st.none()),
    small=st.one_of(_local, _nonlocal, st.none()),
    base_url=st.sampled_from(
        ["http://omniroute.nervecenter:20128/v1", "http://127.0.0.1:20128/v1"]
    ),
)
def test_config_only_omniroute_and_no_api_key(
    model: str | None, small: str | None, base_url: str
) -> None:
    cfg = build_opencode_config(model, small, base_url=base_url)
    assert list(cfg["provider"]) == ["omniroute"]
    assert cfg["enabled_providers"] == ["omniroute"]
    assert cfg["model"].startswith("omniroute/")
    assert cfg["small_model"].startswith("omniroute/")
    blob = json.dumps(cfg).lower()
    assert not re.search(r"api[_-]?key|apikey|secret|token|bearer|sk-", blob)
    assert "apikey" not in {k.lower() for k in cfg["provider"]["omniroute"]["options"]}
