"""Environment-driven provider registry for inference adapters.

The repo convention is ``NERVE_*`` env vars; provider configuration is
held in the optional ``NERVE_INFERENCE_PROVIDERS`` variable — a JSON
array of entries::

    [
      {
        "provider": "openai",
        "model_id": "gpt-4o-mini",
        "base_url": null,
        "api_key_env": "OPENAI_API_KEY",
        "capabilities": ["generation", "embeddings"]
      },
      {
        "provider": "local",
        "model_id": "llama3.1-8b",
        "base_url": "http://localhost:11434",
        "api_key_env": null,
        "capabilities": ["generation"]
      }
    ]

``api_key_env`` names the env var holding the key (keys stay in the
environment — never on disk, never in the DB). When the named env var
is absent the provider is still listed, but with
``key_configured: false`` — an honest state, never an invented key.
Key values are read once at build time, attached to the adapter, and
never logged or echoed.

Degradation is deliberately honest and non-fatal:

* unset / empty / invalid JSON → ``(AdapterPool(), [])`` — no crash;
* unknown provider name or malformed entry → skipped with a
  ``log.warning``; the remaining entries still build.

The sanitized projection (see :func:`build_provider_pool`) never
contains a key value.

Adapter instantiation is best-effort: if a configured provider's
concrete adapter cannot be built (the adapters in
:mod:`backend.inference.adapter` are still abstract, and the live
inference path remains :class:`backend.inference.omniroute.OmniRouteAdapter`),
an honest stand-in (:class:`_PendingAdapter`) occupies the pool slot
and the entry is still listed — but it raises ``NotImplementedError``
on use rather than fabricating a result.
"""
from __future__ import annotations

import json
import os
from typing import Any

import structlog
from pydantic import BaseModel, Field

from backend.inference.adapter import (
    AdapterPool,
    AnthropicAdapter,
    HuggingFaceAdapter,
    InferenceAdapter,
    InferenceResult,
    LocalAdapter,
    OpenAIAdapter,
)

log = structlog.get_logger(__name__)

#: Env var holding the JSON array of provider entries.
PROVIDERS_ENV_VAR = "NERVE_INFERENCE_PROVIDERS"

#: Provider names that map to a concrete :class:`InferenceAdapter`.
_ADAPTER_TYPES: dict[str, type[InferenceAdapter]] = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "huggingface": HuggingFaceAdapter,
    "local": LocalAdapter,
}

#: Adapters whose constructor accepts ``base_url``.
_ADAPTERS_WITH_BASE_URL = frozenset({"openai", "anthropic", "local"})


def _truthy(value: str | None) -> bool:
    """Truthy check matching :mod:`backend.inference.omniroute` semantics."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


class ProviderConfig(BaseModel):
    """One validated entry from ``NERVE_INFERENCE_PROVIDERS``."""

    model_config = {"extra": "forbid"}

    provider: str
    model_id: str
    base_url: str | None = None
    api_key_env: str | None = None
    capabilities: list[str] = Field(default_factory=list)


def _read_key(entry: ProviderConfig) -> tuple[str | None, bool]:
    """Resolve the entry's key from the env var named by ``api_key_env``.

    Returns ``(key_or_None, key_configured)``. The key value is never
    logged, echoed, or included in the sanitized projection.
    """
    if not entry.api_key_env:
        return None, False
    key = os.environ.get(entry.api_key_env, "").strip()
    return (key or None), bool(key)


class _PendingAdapter(InferenceAdapter):
    """Registry stand-in for a provider whose adapter cannot be built yet.

    The concrete adapters in :mod:`backend.inference.adapter` still
    lack ``generate`` / ``embed`` implementations, so they cannot be
    instantiated. The registry stays complete and honest: the entry is
    listed and the pool slot is occupied, but any use of the stand-in
    raises :class:`NotImplementedError` — it never fabricates a result
    (the live inference path remains
    :class:`backend.inference.omniroute.OmniRouteAdapter`). Once the
    concrete adapters are implemented, :func:`build_provider_pool`
    registers them directly and this stand-in stops being used.
    """

    def __init__(self, provider: str, model_id: str) -> None:
        super().__init__(model_id)
        self.provider = provider
        self.api_key: str | None = None
        self.base_url: str | None = None

    async def generate(
        self,
        prompt: str,
        parameters: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> InferenceResult:
        raise NotImplementedError(
            f"Provider adapter '{self.provider}' has no working "
            "implementation yet; the live inference path remains "
            "OmniRouteAdapter."
        )

    async def embed(
        self,
        texts: list[str],
        parameters: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        raise NotImplementedError(
            f"Provider adapter '{self.provider}' has no working "
            "implementation yet; the live inference path remains "
            "OmniRouteAdapter."
        )


def _instantiate(entry: ProviderConfig) -> InferenceAdapter:
    """Build the concrete adapter for a validated config entry."""
    adapter_type = _ADAPTER_TYPES[entry.provider]
    base_url = (entry.base_url or "").strip() or None
    if base_url is not None and entry.provider not in _ADAPTERS_WITH_BASE_URL:
        # Honest: the adapter cannot take a base URL — say so, drop it.
        log.warning(
            "inference.providers.base_url_ignored",
            provider=entry.provider,
            base_url=base_url,
        )
        base_url = None
    api_key, _ = _read_key(entry)
    if entry.provider == "local":
        return LocalAdapter(entry.model_id, base_url=base_url or "http://localhost:11434")
    if entry.provider in ("openai", "anthropic"):
        return adapter_type(entry.model_id, api_key=api_key, base_url=base_url)
    return HuggingFaceAdapter(entry.model_id, api_key=api_key)


def build_provider_pool() -> tuple[AdapterPool, list[dict[str, Any]]]:
    """Build the :class:`AdapterPool` from ``NERVE_INFERENCE_PROVIDERS``.

    Returns
    -------
    (pool, providers)
        ``pool`` holds one concrete adapter per valid entry
        (registered by provider name); ``providers`` is the sanitized
        list of ``{provider, model_id, base_url, key_configured,
        capabilities}`` dicts — never a key value.

    Unset / empty / invalid env var → ``(AdapterPool(), [])``. Unknown
    provider names or malformed entries are skipped with a warning and
    never raise: a bad config entry must not take the app down.
    """
    pool = AdapterPool()
    providers: list[dict[str, Any]] = []
    raw = (os.environ.get(PROVIDERS_ENV_VAR) or "").strip()
    if not raw:
        return pool, providers

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("inference.providers.invalid_json", error=str(exc))
        return pool, providers
    if not isinstance(parsed, list):
        log.warning("inference.providers.not_a_list")
        return pool, providers

    for index, item in enumerate(parsed):
        try:
            entry = ProviderConfig.model_validate(item)
        except Exception as exc:  # noqa: BLE001 - bad entry must not crash startup
            log.warning("inference.providers.bad_entry", index=index, error=str(exc))
            continue
        if entry.provider not in _ADAPTER_TYPES:
            log.warning(
                "inference.providers.unknown_provider",
                provider=entry.provider,
                index=index,
            )
            continue
        if not entry.model_id.strip():
            log.warning(
                "inference.providers.empty_model_id",
                provider=entry.provider,
                index=index,
            )
            continue

        try:
            adapter = _instantiate(entry)
        except Exception as exc:  # noqa: BLE001 - a bad build must not crash startup
            log.warning(
                "inference.providers.adapter_uninstantiable",
                provider=entry.provider,
                error=str(exc),
            )
            adapter = _PendingAdapter(entry.provider, entry.model_id)
        pool.register(adapter)
        _, key_configured = _read_key(entry)
        providers.append(
            {
                "provider": entry.provider,
                "model_id": entry.model_id,
                "base_url": entry.base_url,
                "key_configured": key_configured,
                "capabilities": list(entry.capabilities),
            }
        )

    return pool, providers


def get_default_model() -> str | None:
    """The ``model_id`` of the first configured provider, or ``None``."""
    _, providers = build_provider_pool()
    if not providers:
        return None
    return (providers[0]["model_id"] or "").strip() or None


def omniroute_default_info() -> dict[str, Any] | None:
    """Sanitized description of the live OmniRoute transport, if configured.

    The only live inference path today is
    :mod:`backend.inference.omniroute`. It counts as configured when
    ``NERVE_OMNIROUTE_URL`` is set (or fixture replay is enabled via
    ``NERVE_OMNIROUTE_FIXTURE``); otherwise there is no default model to
    report and ``None`` is returned — the endpoint must not fabricate
    one. The transport URL and bearer key are deployment plumbing, not
    provider config, so neither is echoed; ``NERVE_OMNIROUTE_MODEL``
    optionally names the default model (unset → ``None``).
    """
    url = (os.environ.get("NERVE_OMNIROUTE_URL") or "").strip()
    fixture = _truthy(os.environ.get("NERVE_OMNIROUTE_FIXTURE"))
    if not url and not fixture:
        return None
    key = (os.environ.get("NERVE_OMNIROUTE_API_KEY") or "").strip()
    model = (os.environ.get("NERVE_OMNIROUTE_MODEL") or "").strip()
    return {
        "provider": "omniroute",
        "model_id": model or None,
        "base_url": None,
        "key_configured": bool(key),
        "capabilities": ["generation"],
        "default": True,
    }
