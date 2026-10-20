"""Inference telemetry collection for performance tracking.

This module implements telemetry collection for inference operations
according to Section 30 (Telemetry) of the NerveCenter specification.

Telemetry features:
- Request-level metrics collection
- Provider/model performance tracking
- Latency percentiles calculation
- Error rate monitoring
"""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from schemas.routes import OmniRouteTelemetry


@dataclass
class LatencySnapshot:
    """A single latency measurement."""

    timestamp: datetime
    ttft_ms: float | None
    e2e_latency_ms: float


class LatencyWindow(BaseModel):
    """Sliding window of latency measurements for percentile calculation."""

    model_config = {"extra": "forbid"}

    size: int = Field(
        default=100,
        ge=1,
        description="Maximum number of samples to retain.",
    )
    samples: deque[LatencySnapshot] = field(
        default_factory=lambda: deque(maxlen=100),
        repr=False,
    )

    def record(self, ttft_ms: float | None, e2e_latency_ms: float) -> None:
        """Record a new latency sample."""
        self.samples.append(
            LatencySnapshot(
                timestamp=datetime.now(UTC),
                ttft_ms=ttft_ms,
                e2e_latency_ms=e2e_latency_ms,
            )
        )

    def percentile(self, p: float) -> float | None:
        """Calculate percentile for e2e latency."""
        if not self.samples:
            return None
        sorted_latencies = sorted(s.e2e_latency_ms for s in self.samples)
        idx = int(len(sorted_latencies) * p / 100)
        return sorted_latencies[min(idx, len(sorted_latencies) - 1)]

    def avg_ttft(self) -> float | None:
        """Calculate average time to first token."""
        ttfts = [s.ttft_ms for s in self.samples if s.ttft_ms is not None]
        if not ttfts:
            return None
        return sum(ttfts) / len(ttfts)

    def avg_e2e(self) -> float:
        """Calculate average end-to-end latency."""
        if not self.samples:
            return 0.0
        return sum(s.e2e_latency_ms for s in self.samples) / len(self.samples)

    def samples_count(self) -> int:
        """Return number of samples in window."""
        return len(self.samples)


class InferenceTelemetry(BaseModel):
    """Collects and aggregates inference telemetry."""

    model_config = {"extra": "forbid"}

    windows: dict[str, LatencyWindow] = Field(
        default_factory=dict,
        description="Latency windows per (provider, model).",
    )
    request_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Request counts per (provider, model).",
    )
    error_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Error counts per (provider, model).",
    )
    token_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Token counts per (provider, model).",
    )
    cost_usd: dict[str, float] = Field(
        default_factory=dict,
        description="Accumulated cost per (provider, model).",
    )

    def record_success(
        self,
        provider: str,
        model: str,
        ttft_ms: float | None,
        e2e_latency_ms: float,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float,
    ) -> None:
        """Record a successful inference request."""
        key = f"{provider}/{model}"
        self._ensure_key(key)

        window = self.windows[key]
        window.record(ttft_ms, e2e_latency_ms)

        self.request_counts[key] += 1
        self.token_counts[key] = self.token_counts.get(key, 0) + input_tokens + output_tokens
        self.cost_usd[key] = self.cost_usd.get(key, 0.0) + estimated_cost_usd

    def record_error(
        self,
        provider: str,
        model: str,
        e2e_latency_ms: float | None = None,
    ) -> None:
        """Record a failed inference request."""
        key = f"{provider}/{model}"
        self._ensure_key(key)

        if e2e_latency_ms is not None:
            window = self.windows[key]
            window.record(None, e2e_latency_ms)

        self.request_counts[key] += 1
        self.error_counts[key] = self.error_counts.get(key, 0) + 1

    def _ensure_key(self, key: str) -> None:
        """Ensure window and counters exist for key."""
        if key not in self.windows:
            self.windows[key] = LatencyWindow()
        self.request_counts.setdefault(key, 0)
        self.error_counts.setdefault(key, 0)

    def get_metrics(self, provider: str, model: str) -> OmniRouteTelemetry:
        """Generate telemetry metrics for a provider/model."""
        key = f"{provider}/{model}"
        window = self.windows.get(key)

        total_requests = self.request_counts.get(key, 0)
        total_errors = self.error_counts.get(key, 0)
        success_rate = 1.0 - (total_errors / total_requests) if total_requests > 0 else 1.0

        return OmniRouteTelemetry(
            id=f"telemetry-{provider}/{model}",
            provider=provider,
            model=model,
            sample_at=datetime.now(UTC),
            requests=total_requests,
            success_rate=success_rate,
            ttft_ms=window.avg_ttft() if window else None,
            tokens_per_second=None,
            e2e_latency_ms=window.avg_e2e() if window else None,
            p95_latency_ms=window.percentile(95) if window else None,
            errors=total_errors,
            circuit_state="open" if total_errors > total_requests * 0.5 else "closed",
        )

    def get_all_metrics(self) -> list[OmniRouteTelemetry]:
        """Get metrics for all providers/models."""
        metrics = []
        for key in set(self.windows.keys()) | set(self.request_counts.keys()):
            provider, model = key.split("/", 1)
            metrics.append(self.get_metrics(provider, model))
        return metrics

    def reset(self) -> None:
        """Reset all telemetry data."""
        self.windows.clear()
        self.request_counts.clear()
        self.error_counts.clear()
        self.token_counts.clear()
        self.cost_usd.clear()


class TelemetryCollector:
    """Async telemetry collector for inference requests."""

    def __init__(self, telemetry: InferenceTelemetry | None = None) -> None:
        self.telemetry = telemetry or InferenceTelemetry()
        self._lock = asyncio.Lock()

    async def record_request(
        self,
        provider: str,
        model: str,
        success: bool,
        ttft_ms: float | None = None,
        e2e_latency_ms: float | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        """Record an inference request."""
        async with self._lock:
            if success:
                self.telemetry.record_success(
                    provider=provider,
                    model=model,
                    ttft_ms=ttft_ms,
                    e2e_latency_ms=e2e_latency_ms or 0.0,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                )
            else:
                self.telemetry.record_error(
                    provider=provider,
                    model=model,
                    e2e_latency_ms=e2e_latency_ms or 0.0,
                )

    async def get_metrics(self, provider: str, model: str) -> OmniRouteTelemetry:
        """Get metrics for a provider/model."""
        return self.telemetry.get_metrics(provider, model)

    async def get_all_metrics(self) -> list[OmniRouteTelemetry]:
        """Get metrics for all providers/models."""
        return self.telemetry.get_all_metrics()

    async def reset(self) -> None:
        """Reset all telemetry."""
        async with self._lock:
            self.telemetry.reset()
