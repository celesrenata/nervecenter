"""Inference adapter for model backends.

This module implements the ``InferenceAdapter`` interface that abstracts
different inference backends (local, cloud APIs, self-hosted models)
according to Section 29 (Inference Adapter) of the NerveCenter specification.

The adapter pattern:
- Provides a uniform interface for model inference
- Handles different authentication mechanisms
- Manages model loading and caching
- Supports streaming and batch inference
"""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class InferenceResult(BaseModel):
    """Result of an inference call."""

    model_config = {"extra": "forbid"}

    provider: str = Field(..., description="Provider that served the request.")
    model: str = Field(..., description="Model identifier used.")
    content: str = Field(..., description="Generated content.")
    input_tokens: int = Field(..., description="Number of input tokens processed.")
    output_tokens: int = Field(..., description="Number of output tokens generated.")
    ttft_ms: float | None = Field(
        default=None,
        description="Time to first token in milliseconds.",
    )
    e2e_latency_ms: float | None = Field(
        default=None,
        description="End-to-end latency in milliseconds.",
    )


class InferenceAdapter(ABC):
    """Abstract base class for inference backends.

    All provider-specific adapters must implement this interface.
    """

    provider: str
    model_id: str

    def __init__(self, model_id: str, **kwargs: Any) -> None:
        self.model_id = model_id
        super().__init__()

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        parameters: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> InferenceResult:
        """Generate content from the model.

        Args:
            prompt: Input prompt for generation.
            parameters: Model generation parameters.
            timeout_ms: Request timeout in milliseconds.

        Returns:
            InferenceResult with generated content and metrics.
        """

    @abstractmethod
    async def embed(
        self,
        texts: list[str],
        parameters: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        """Generate embeddings for input texts.

        Args:
            texts: List of input texts to embed.
            parameters: Embedding generation parameters.

        Returns:
            List of embedding vectors.
        """

    async def close(self) -> None:
        """Clean up resources."""
        pass


class LocalAdapter(InferenceAdapter):
    """Adapter for local inference backends (e.g., Ollama, vLLM)."""

    provider: str = "local"

    def __init__(self, model_id: str, base_url: str = "http://localhost:11434", **kwargs: Any) -> None:
        super().__init__(model_id, **kwargs)
        self.base_url = base_url


class OpenAIAdapter(InferenceAdapter):
    """Adapter for OpenAI API."""

    provider: str = "openai"

    def __init__(
        self,
        model_id: str,
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_id, **kwargs)
        self.api_key = api_key
        self.base_url = base_url or "https://api.openai.com/v1"


class AnthropicAdapter(InferenceAdapter):
    """Adapter for Anthropic API."""

    provider: str = "anthropic"

    def __init__(
        self,
        model_id: str,
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_id, **kwargs)
        self.api_key = api_key
        self.base_url = base_url or "https://api.anthropic.com/v1"


class HuggingFaceAdapter(InferenceAdapter):
    """Adapter for Hugging Face Inference API."""

    provider: str = "huggingface"

    def __init__(
        self,
        model_id: str,
        api_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_id, **kwargs)
        self.api_key = api_key


class AdapterPool(BaseModel):
    """Manages multiple inference adapters.

    Provides a unified interface for selecting and using different
    inference backends based on routing decisions.
    """

    model_config = {"extra": "forbid"}

    adapters: dict[str, Any] = Field(
        default_factory=dict,
        description="Registered adapters by provider name.",
    )

    def register(self, adapter: InferenceAdapter) -> None:
        """Register an adapter by its provider name."""
        self.adapters[adapter.provider] = adapter

    def get(self, provider: str) -> InferenceAdapter | None:
        """Get an adapter by provider name."""
        return self.adapters.get(provider)

    async def generate(
        self,
        provider: str,
        prompt: str,
        parameters: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> InferenceResult:
        """Generate content using the specified provider."""
        adapter = self.get(provider)
        if not adapter:
            raise ValueError(f"Unknown provider: {provider}")
        return await adapter.generate(prompt, parameters, timeout_ms)

    async def close(self) -> None:
        """Close all adapters."""
        for adapter in self.adapters.values():
            await adapter.close()
