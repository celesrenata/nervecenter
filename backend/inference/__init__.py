"""Inference: model routing and inference adapters.

This package implements the inference pipeline:
- InferencePolicyEngine: Model selection based on capabilities and metrics
- ContextCompiler: Bounded context assembly
- OmniRouteAdapter: Inference request routing
- InferenceTelemetry: Performance tracking

Modules
-------
- ``policy`` — InferencePolicyEngine for model selection
- ``omniroute`` — OmniRouteAdapter for routing
- ``adapter`` — Inference adapters for model backends
- ``telemetry`` — Inference telemetry collection
"""

from .adapter import (
    AdapterPool,
    AnthropicAdapter,
    HuggingFaceAdapter,
    InferenceAdapter,
    InferenceResult,
    LocalAdapter,
    OpenAIAdapter,
)
from .omniroute import (
    InferenceRequest,
    InferenceResponse,
    OmniRouteAdapter,
    OmniRouteTransport,
    OmniRouteUnavailableError,
    RouteCandidate,
)
from .policy import InferencePolicyEngine, PolicyRule, TaskRequirements
from .telemetry import (
    InferenceTelemetry,
    LatencySnapshot,
    LatencyWindow,
    TelemetryCollector,
)

__all__ = [
    "InferencePolicyEngine",
    "PolicyRule",
    "TaskRequirements",
    "OmniRouteAdapter",
    "OmniRouteTransport",
    "OmniRouteUnavailableError",
    "RouteCandidate",
    "InferenceRequest",
    "InferenceResponse",
    "InferenceAdapter",
    "InferenceResult",
    "LocalAdapter",
    "OpenAIAdapter",
    "AnthropicAdapter",
    "HuggingFaceAdapter",
    "AdapterPool",
    "InferenceTelemetry",
    "TelemetryCollector",
    "LatencyWindow",
    "LatencySnapshot",
]
