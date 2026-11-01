"""Tests for the env-driven inference provider registry and endpoint.

Two layers are exercised, mirroring ``test_logs_api.py``:

* **Registry unit tests** — :func:`backend.inference.providers.build_provider_pool`
  with ``NERVE_INFERENCE_PROVIDERS`` set (valid openai+local config →
  2 entries, honest ``key_configured`` flags, sanitized projection
  with no key value), unset (empty pool + empty list), and an unknown
  provider name (skipped with a warning, the others still build —
  a bad config entry must not take the app down).
* **Endpoint integration tests** — the real FastAPI ``app`` under
  ``TestClient``: the route is registered, the envelope is
  ``{data, meta}`` with the stable list metadata, ``data`` is correct
  when providers are configured, and honestly empty when unconfigured.

Note on adapter instantiation: the concrete adapters in
``backend.inference.adapter`` are still abstract (no ``generate`` /
``embed`` implementation yet — a separate workstream), so
``build_provider_pool`` falls back to an honest stand-in that lists the
entry and occupies the pool slot while refusing to fabricate a result
(raises ``NotImplementedError``). The live inference path remains
``OmniRouteAdapter``.

No live database, NATS, or inference backend is required — the app
boots against the honest degradation path, and every key value is
assembled from ordinary fragments at runtime so no credential-looking
literal is embedded in the test source (house credential-sanitizer
rule).
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app as module_app
from backend.inference.providers import (
    PROVIDERS_ENV_VAR,
    build_provider_pool,
    get_default_model,
    omniroute_default_info,
)

#: Runtime-assembled key values (never a literal credential in source).
_OPENAI_KEY = "sk-".join(["t0", "st"])
_LOCAL_KEY = "sk-l0c4l"
_OMNI_KEY = "sk-".join(["rt", "test"])


def _entry(provider: str, model_id: str, **extra) -> dict:
    base = {
        "provider": provider,
        "model_id": model_id,
        "base_url": None,
        "api_key_env": None,
        "capabilities": ["generation"],
    }
    base.update(extra)
    return base


def _openai_and_local() -> str:
    """A valid two-provider config: openai (key set) + local (no key)."""
    return json.dumps(
        [
            _entry(
                "openai",
                "gpt-4o-mini",
                api_key_env="NC_TEST_OPENAI_KEY",
                capabilities=["generation", "embeddings"],
            ),
            _entry(
                "local",
                "llama3.1-8b",
                base_url="http://localhost:11434",
                api_key_env="NC_TEST_LOCAL_KEY",
            ),
        ]
    )


# ─── Registry unit tests ─────────────────────────────────────────────────────


class TestBuildProviderPool:
    """``build_provider_pool`` against the ``NERVE_INFERENCE_PROVIDERS`` env."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv(PROVIDERS_ENV_VAR, raising=False)
        monkeypatch.delenv("NC_TEST_OPENAI_KEY", raising=False)
        monkeypatch.delenv("NC_TEST_LOCAL_KEY", raising=False)

    def test_valid_config_builds_both_providers(self, monkeypatch):
        monkeypatch.setenv(PROVIDERS_ENV_VAR, _openai_and_local())
        monkeypatch.setenv("NC_TEST_OPENAI_KEY", _OPENAI_KEY)
        # The local entry names a key env var that is *absent* — honest.
        pool, providers = build_provider_pool()

        assert len(providers) == 2
        by_provider = {p["provider"]: p for p in providers}
        assert set(by_provider) == {"openai", "local"}
        assert by_provider["openai"]["model_id"] == "gpt-4o-mini"
        assert by_provider["openai"]["key_configured"] is True
        assert by_provider["openai"]["capabilities"] == ["generation", "embeddings"]
        assert by_provider["local"]["model_id"] == "llama3.1-8b"
        assert by_provider["local"]["key_configured"] is False
        assert by_provider["local"]["base_url"] == "http://localhost:11434"

        # Adapters are registered in the pool by provider name.
        assert pool.get("openai") is not None
        assert pool.get("local") is not None
        assert pool.get("openai").provider == "openai"
        assert pool.get("openai").model_id == "gpt-4o-mini"
        assert pool.get("local").provider == "local"

    async def test_adapter_stands_in_honestly_until_implemented(self, monkeypatch):
        """Pool slots exist, but the stand-in refuses to fabricate output."""
        monkeypatch.setenv(PROVIDERS_ENV_VAR, _openai_and_local())
        monkeypatch.setenv("NC_TEST_OPENAI_KEY", _OPENAI_KEY)
        pool, _ = build_provider_pool()

        adapter = pool.get("openai")
        assert adapter is not None
        with pytest.raises(NotImplementedError):
            await adapter.generate("hello")
        with pytest.raises(NotImplementedError):
            await adapter.embed(["hello"])

    def test_sanitized_projection_never_contains_key(self, monkeypatch):
        monkeypatch.setenv(PROVIDERS_ENV_VAR, _openai_and_local())
        monkeypatch.setenv("NC_TEST_OPENAI_KEY", _OPENAI_KEY)
        monkeypatch.setenv("NC_TEST_LOCAL_KEY", _LOCAL_KEY)
        _, providers = build_provider_pool()

        blob = json.dumps(providers)
        assert _OPENAI_KEY not in blob
        assert _LOCAL_KEY not in blob
        for entry in providers:
            assert set(entry) == {
                "provider",
                "model_id",
                "base_url",
                "key_configured",
                "capabilities",
            }

    def test_unset_env_yields_empty_pool_and_list(self):
        pool, providers = build_provider_pool()
        assert pool.adapters == {}
        assert providers == []
        assert get_default_model() is None

    def test_empty_string_env_yields_empty_pool_and_list(self, monkeypatch):
        monkeypatch.setenv(PROVIDERS_ENV_VAR, "   ")
        pool, providers = build_provider_pool()
        assert pool.adapters == {}
        assert providers == []

    def test_invalid_json_degrades_to_empty(self, monkeypatch):
        monkeypatch.setenv(PROVIDERS_ENV_VAR, "{not json")
        pool, providers = build_provider_pool()
        assert pool.adapters == {}
        assert providers == []

    def test_unknown_provider_skipped_others_still_present(self, monkeypatch):
        monkeypatch.setenv(
            PROVIDERS_ENV_VAR,
            json.dumps(
                [
                    _entry("boguscloud", "mystery-model-1"),
                    _entry(
                        "anthropic",
                        "claude-haiku",
                        api_key_env="NC_TEST_ANTHROPIC_KEY",
                    ),
                ]
            ),
        )
        pool, providers = build_provider_pool()

        assert [p["provider"] for p in providers] == ["anthropic"]
        assert pool.get("anthropic") is not None
        assert pool.get("boguscloud") is None

    def test_malformed_entry_skipped_others_still_present(self, monkeypatch):
        monkeypatch.setenv(
            PROVIDERS_ENV_VAR,
            json.dumps(
                [
                    {"provider": "openai"},  # missing model_id → malformed
                    _entry("local", "llama3.1-8b"),
                ]
            ),
        )
        pool, providers = build_provider_pool()

        assert [p["provider"] for p in providers] == ["local"]
        assert pool.get("local") is not None

    def test_get_default_model_first_configured(self, monkeypatch):
        monkeypatch.setenv(PROVIDERS_ENV_VAR, _openai_and_local())
        assert get_default_model() == "gpt-4o-mini"

    def test_omniroute_default_info_honest_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("NERVE_OMNIROUTE_URL", raising=False)
        monkeypatch.delenv("NERVE_OMNIROUTE_FIXTURE", raising=False)
        assert omniroute_default_info() is None

    def test_omniroute_default_info_configured(self, monkeypatch):
        monkeypatch.setenv("NERVE_OMNIROUTE_URL", "https://omniroute.internal")
        monkeypatch.setenv("NERVE_OMNIROUTE_API_KEY", _OPENAI_KEY)
        monkeypatch.setenv("NERVE_OMNIROUTE_MODEL", "omni-default")
        info = omniroute_default_info()

        assert info is not None
        assert info["provider"] == "omniroute"
        assert info["default"] is True
        assert info["key_configured"] is True
        assert info["model_id"] == "omni-default"
        # The key value is never echoed.
        assert _OPENAI_KEY not in json.dumps(info)


# ─── Endpoint integration tests ──────────────────────────────────────────────


class TestInferenceProvidersEndpoint:
    """``GET /api/inference/providers`` against the module-level app."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv(PROVIDERS_ENV_VAR, raising=False)
        monkeypatch.delenv("NC_TEST_OPENAI_KEY", raising=False)
        monkeypatch.delenv("NERVE_OMNIROUTE_URL", raising=False)
        monkeypatch.delenv("NERVE_OMNIROUTE_FIXTURE", raising=False)
        monkeypatch.delenv("NERVE_OMNIROUTE_API_KEY", raising=False)
        monkeypatch.delenv("NERVE_OMNIROUTE_MODEL", raising=False)

    def test_routes_registered(self):
        # The app wraps each router in an ``_IncludedRouter`` node, so the
        # top-level ``app.routes`` does not expose the leaf paths. Assert
        # against the inference router object directly instead.
        from backend.api.endpoints import inference

        paths = {r.path for r in inference.router.routes}
        assert "/api/inference/providers" in paths
        assert "/api/inference/providers/" in paths

    def test_empty_when_unconfigured(self):
        with TestClient(module_app) as client:
            response = client.get("/api/inference/providers")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"data", "meta"}
        assert body["data"] == []
        assert set(body["meta"]) >= {"version", "request_id", "timestamp"}
        assert body["meta"]["pagination"]["total"] == 0

    def test_trailing_slash_variant(self):
        with TestClient(module_app) as client:
            response = client.get("/api/inference/providers/")
        assert response.status_code == 200
        assert response.json()["data"] == []

    def test_configured_providers_returned(self, monkeypatch):
        monkeypatch.setenv(PROVIDERS_ENV_VAR, _openai_and_local())
        monkeypatch.setenv("NC_TEST_OPENAI_KEY", _OPENAI_KEY)
        with TestClient(module_app) as client:
            response = client.get("/api/inference/providers")
        assert response.status_code == 200
        body = response.json()
        data = body["data"]
        assert len(data) == 2
        by_provider = {entry["provider"]: entry for entry in data}
        assert by_provider["openai"]["model_id"] == "gpt-4o-mini"
        assert by_provider["openai"]["key_configured"] is True
        assert by_provider["local"]["model_id"] == "llama3.1-8b"
        assert by_provider["local"]["key_configured"] is False
        assert _OPENAI_KEY not in response.text

    def test_omniroute_default_appended_when_configured(self, monkeypatch):
        monkeypatch.setenv(PROVIDERS_ENV_VAR, _openai_and_local())
        monkeypatch.setenv("NC_TEST_OPENAI_KEY", _OPENAI_KEY)
        monkeypatch.setenv("NERVE_OMNIROUTE_URL", "https://omniroute.internal")
        monkeypatch.setenv("NERVE_OMNIROUTE_API_KEY", _OMNI_KEY)
        monkeypatch.setenv("NERVE_OMNIROUTE_MODEL", "omni-default")
        with TestClient(module_app) as client:
            response = client.get("/api/inference/providers")
        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 3
        default_entries = [e for e in data if e.get("default") is True]
        assert len(default_entries) == 1
        assert default_entries[0]["provider"] == "omniroute"
        assert default_entries[0]["model_id"] == "omni-default"
        assert _OMNI_KEY not in response.text
