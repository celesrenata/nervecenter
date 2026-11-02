"""Tests for the selectable inference surface (INFER CONTROL CONTRACT §1).

Covers the two honest helpers backing ``GET /api/inference/available`` and the
endpoint integration:

* **Registry unit tests** — :func:`available_models` (env list → provider
  entries → ``[]``, never a fabricated list) and :func:`default_inference`
  (env overrides → AskService defaults), plus :func:`_sanitize_binding` (the
  contract §3 binding projection: strip, drop unknown keys, empty → ``None``).
* **Endpoint integration** — ``GET /api/inference/available`` against the
  module-level app: route registered, honest empty list when unconfigured, and
  the configured env list echoed when set.

No live database or inference backend is required.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app as module_app
from backend.inference.providers import (
    DEFAULT_MODEL_ENV_VAR,
    DEFAULT_PROVIDER_ENV_VAR,
    INFER_MODELS_ENV_VAR,
    PROVIDERS_ENV_VAR,
    _sanitize_binding,
    available_models,
    default_inference,
)


def _providers_openai() -> str:
    """A minimal valid single-provider config (openai, key absent → honest)."""
    return (
        '[{"provider": "openai", "model_id": "gpt-4o-mini", '
        '"api_key_env": "NC_TEST_OPENAI_KEY"}]'
    )


# ─── available_models() ───────────────────────────────────────────────────────


class TestAvailableModels:
    """``available_models`` resolution order (env list → provider entries → [])."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv(INFER_MODELS_ENV_VAR, raising=False)
        monkeypatch.delenv(PROVIDERS_ENV_VAR, raising=False)
        monkeypatch.delenv("NC_TEST_OPENAI_KEY", raising=False)

    def test_empty_when_unconfigured(self):
        # No env list, no provider entries → honest empty list.
        assert available_models() == []

    def test_env_list_wins(self, monkeypatch):
        monkeypatch.setenv(INFER_MODELS_ENV_VAR, '["a", "b", "a"]')
        # The env list is echoed verbatim (no de-dup of the explicit list).
        assert available_models() == ["a", "b", "a"]

    def test_env_list_invalid_degrades_to_provider_entries(self, monkeypatch):
        monkeypatch.setenv(INFER_MODELS_ENV_VAR, "{not json")
        monkeypatch.setenv(PROVIDERS_ENV_VAR, _providers_openai())
        assert available_models() == ["gpt-4o-mini"]

    def test_falls_back_to_provider_entries(self, monkeypatch):
        monkeypatch.setenv(PROVIDERS_ENV_VAR, _providers_openai())
        assert available_models() == ["gpt-4o-mini"]


# ─── default_inference() ──────────────────────────────────────────────────────


class TestDefaultInference:
    """``default_inference`` returns ``{"provider", "model"}`` (contract §1)."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv(DEFAULT_PROVIDER_ENV_VAR, raising=False)
        monkeypatch.delenv(DEFAULT_MODEL_ENV_VAR, raising=False)

    def test_defaults_when_unset(self):
        di = default_inference()
        assert di == {"provider": "omniroute", "model": "nerve-ask"}

    def test_env_overrides_win(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_PROVIDER_ENV_VAR, "openai")
        monkeypatch.setenv(DEFAULT_MODEL_ENV_VAR, "gpt-4o-mini")
        assert default_inference() == {"provider": "openai", "model": "gpt-4o-mini"}

    def test_partial_override_keeps_other_default(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_PROVIDER_ENV_VAR, "anthropic")
        di = default_inference()
        assert di["provider"] == "anthropic"
        assert di["model"] == "nerve-ask"


# ─── _sanitize_binding() (contract §3 projection) ──────────────────────────────


class TestSanitizeBinding:
    """``_sanitize_binding`` projects a raw binding into the clean wire shape."""

    def test_none_is_none(self):
        assert _sanitize_binding(None) is None

    def test_empty_or_whitespace_dict_is_none(self):
        assert _sanitize_binding({}) is None
        assert _sanitize_binding({"provider": "", "model": "  "}) is None

    def test_full_binding_round_trips(self):
        assert _sanitize_binding({"provider": "openai", "model": "gpt-4o-mini"}) == {
            "provider": "openai",
            "model": "gpt-4o-mini",
        }

    def test_partial_binding_keeps_only_supplied_keys(self):
        assert _sanitize_binding({"model": "claude-sonnet-4"}) == {"model": "claude-sonnet-4"}

    def test_unknown_keys_dropped(self):
        out = _sanitize_binding(
            {"provider": "openai", "model": "m", "temperature": 0.2, "extra": "x"}
        )
        assert out == {"provider": "openai", "model": "m"}


# ─── GET /api/inference/available (endpoint) ──────────────────────────────────


class TestInferenceAvailableEndpoint:
    """``GET /api/inference/available`` against the module-level app."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv(INFER_MODELS_ENV_VAR, raising=False)
        monkeypatch.delenv(PROVIDERS_ENV_VAR, raising=False)
        monkeypatch.delenv(DEFAULT_PROVIDER_ENV_VAR, raising=False)
        monkeypatch.delenv(DEFAULT_MODEL_ENV_VAR, raising=False)
        monkeypatch.delenv("NC_TEST_OPENAI_KEY", raising=False)

    def test_route_registered(self):
        # The app wraps each router in an _IncludedRouter node, so the top-level
        # app.routes does not expose the leaf paths — assert against the router.
        from backend.api.endpoints import inference

        paths = {r.path for r in inference.router.routes}
        assert "/api/inference/available" in paths
        assert "/api/inference/available/" in paths

    def test_honest_empty_when_unconfigured(self):
        with TestClient(module_app) as client:
            resp = client.get("/api/inference/available")
        assert resp.status_code == 200
        body = resp.json()
        # Contract §1 wire shape: result.{providers, models, default}.
        assert body["result"]["models"] == []
        assert body["result"]["default"] == {"provider": "omniroute", "model": "nerve-ask"}
        assert body["result"]["providers"] == []

    def test_returns_env_list_when_set(self, monkeypatch):
        monkeypatch.setenv(INFER_MODELS_ENV_VAR, '["nerve-ask", "claude-sonnet-4"]')
        with TestClient(module_app) as client:
            resp = client.get("/api/inference/available")
        assert resp.status_code == 200
        assert resp.json()["result"]["models"] == ["nerve-ask", "claude-sonnet-4"]
