"""OmniRoute adapter for inference request routing.

This module implements the ``OmniRouteAdapter`` that routes inference
requests to appropriate backends based on routing decisions according
to Section 28 (OmniRoute Adapter) of the NerveCenter specification.

The adapter:
- Converts routing decisions into backend calls
- Handles provider-specific authentication
- Manages request/response translation
- Collects telemetry for policy refinement

The module also carries the real v0 transport (ADR-0010):
``OmniRouteTransport`` speaks HTTP/JSON to an external OmniRoute
deployment (or replays recorded fixtures when
``NERVE_OMNIROUTE_FIXTURE=1``). It is an injected-client-compatible
provider: ``OmniRouteAdapter.add_provider(name, transport)`` makes the
adapter delegate every hop to it, and the transport never fabricates a
response — an unreachable hop raises ``OmniRouteUnavailableError`` so
the fallback chain can be exercised, and the explicit error propagates
when every candidate in the route is exhausted.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field

from schemas.routes import InferenceRoute, OmniRouteTelemetry

log = structlog.get_logger(__name__)


class OmniRouteUnavailableError(RuntimeError):
    """Raised when no OmniRoute route candidate is reachable.

    Carries the ordered list of (provider, model) candidates that were
    tried and the sanitized reason for each. The message is safe to log
    and surface: it never contains the API key, request body, or full
    remote URL — only candidate names and a transport-level failure
    class (ADR-0010, house no-secret-logging rule).
    """

    def __init__(
        self,
        attempted: list[tuple[str, str]],
        reasons: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.attempted = list(attempted)
        self.reasons = dict(reasons or {})
        detail = ", ".join(
            f"{provider}/{model} ({self.reasons.get((provider, model), 'unreachable')})"
            for provider, model in self.attempted
        )
        super().__init__(
            f"OmniRoute unavailable: all {len(self.attempted)} route candidate(s) "
            f"unreachable — {detail}"
        )


def _truthy(value: str | None) -> bool:
    """Parse a truthy env var (matches the ``NERVE_SEED_*`` convention)."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


class _FixtureMissError(FileNotFoundError):
    """Internal: a fixture replay was requested but no fixture was found."""


class InferenceRequest(BaseModel):
    """An inference request to be routed."""

    model_config = {"extra": "forbid"}

    route_key: str = Field(..., description="Key identifying the route to use.")
    prompt: str = Field(..., description="Input prompt for the model.")
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Model generation parameters.",
    )
    timeout_ms: int | None = Field(
        default=None,
        ge=1,
        description="Request timeout in milliseconds.",
    )


class InferenceResponse(BaseModel):
    """Response from an inference call."""

    model_config = {"extra": "forbid"}

    provider: str = Field(..., description="Provider that handled the request.")
    model: str = Field(..., description="Model that generated the response.")
    content: str = Field(..., description="Generated content.")
    input_tokens: int = Field(..., description="Number of input tokens.")
    output_tokens: int = Field(..., description="Number of output tokens.")
    ttft_ms: float | None = Field(
        default=None,
        description="Time to first token in milliseconds.",
    )
    e2e_latency_ms: float | None = Field(
        default=None,
        description="End-to-end latency in milliseconds.",
    )


class RouteCandidate(BaseModel):
    """One route candidate reported by an OmniRoute deployment (spec §32).

    Mirrors the JSON envelope from the Route Candidate Interface::

        {"routes": [{"model": "...", "backend": "...", "node": "...",
                     "availability": "ready"}]}
    """

    model_config = {"extra": "forbid"}

    model: str = Field(..., description="Model id offered by the candidate.")
    backend: str | None = Field(
        default=None, description="Backend (e.g. mlx, vllm) if reported."
    )
    node: str | None = Field(
        default=None, description="Node serving the candidate, if reported."
    )
    availability: str = Field(
        default="ready", description="Candidate availability state."
    )


def _parse_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize an OmniRoute response envelope into the adapter dict.

    Accepts the flat envelope (``content`` / ``usage`` / latency fields)
    and OpenAI-style ``choices`` payloads. Raises
    :class:`ValueError` when no content field is present — the transport
    must never invent content.
    """
    if "content" in payload or "text" in payload:
        content = str(payload.get("content", payload.get("text", "")))
    else:
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message", first)
                if isinstance(message, dict) and "content" in message:
                    content = str(message["content"])
                elif "text" in first:
                    content = str(first["text"])
                else:
                    raise ValueError("choice entry has no content/text field")
            else:
                raise ValueError("choice entry is not an object")
        else:
            raise ValueError("response envelope has no content field")

    raw_usage = payload.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    return {
        "content": content,
        "input_tokens": int(usage.get("input_tokens", usage.get("prompt_tokens", 0))),
        "output_tokens": int(usage.get("output_tokens", usage.get("completion_tokens", 0))),
        "ttft_ms": _as_number(payload.get("ttft_ms")),
        "e2e_latency_ms": _as_number(payload.get("e2e_latency_ms")),
    }


def _as_number(value: Any) -> float | None:
    """Coerce an optional numeric field (None when absent/invalid)."""
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


@dataclass
class OmniRouteTransport:
    """Real HTTP/JSON transport to an external OmniRoute deployment.

    Configuration (constructor or environment):

    - ``NERVE_OMNIROUTE_URL`` — base URL of the OmniRoute deployment
      (e.g. ``https://omniroute.internal``);
    - ``NERVE_OMNIROUTE_API_KEY`` — bearer key, sent as
      ``Authorization: Bearer …``. Env-only, **never logged** and never
      echoed in error messages (house no-secret-logging rule);
    - ``NERVE_OMNIROUTE_FIXTURE`` — when truthy (``1``/``true``/``yes``/
      ``on``), the transport replays recorded fixture JSON from
      ``fixture_dir`` instead of hitting the network, keeping tests and
      CI deterministic. Fixture files are named ``<model>.json`` (or
      ``<provider>--<model>.json``) and contain the raw response
      envelope; a file with ``"status"`` >= 400 simulates a failed hop.

    Behaviour:

    - ``generate`` implements the injected-client seam (the same
      ``generate(model, prompt, parameters, timeout)`` contract the
      ``OmniRouteAdapter`` already consumes) and raises
      :class:`OmniRouteUnavailableError` when the endpoint is
      unreachable or no fixture exists — **never** a fabricated
      response.
    - When ``telemetry`` is set (an ``InferenceTelemetry``), every hop
      attempt — success *or* failure — is recorded through its public
      ``record_success`` / ``record_error`` API, so route telemetry
      flows to the routes plane without changing the public API.
    - ``route_candidates`` fetches the spec §32 candidate envelope.
    """

    base_url: str | None = None
    api_key: str | None = None
    fixture_dir: str | None = None
    default_timeout_ms: int = 30_000
    telemetry: Any = None  # InferenceTelemetry | None (Any: avoid circular import)

    # Transport knobs (used by tests; default is a lazily-created client).
    _client: Any = None

    def __post_init__(self) -> None:
        if self.base_url is None:
            self.base_url = os.environ.get("NERVE_OMNIROUTE_URL", "").strip() or None
        if self.api_key is None:
            key = os.environ.get("NERVE_OMNIROUTE_API_KEY", "").strip()
            self.api_key = key or None
        if self.fixture_dir is None and _truthy(os.environ.get("NERVE_OMNIROUTE_FIXTURE")):
            self.fixture_dir = os.environ.get(
                "NERVE_OMNIROUTE_FIXTURE_DIR",
                "tests/fixtures/omniroute",
            ).strip() or "tests/fixtures/omniroute"

    @classmethod
    def from_env(cls) -> OmniRouteTransport:
        """Build a transport from the ``NERVE_OMNIROUTE_*`` environment."""
        return cls()

    @property
    def fixture_mode(self) -> bool:
        """Whether this transport replays fixtures instead of the network."""
        return self.fixture_dir is not None

    # ------------------------------------------------------------------
    # Provider-client seam
    # ------------------------------------------------------------------

    def generate(
        self,
        model: str,
        prompt: str,
        parameters: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Run one inference call against the configured deployment.

        Args:
            model: Model identifier for this hop.
            prompt: Input prompt.
            parameters: Model generation parameters.
            timeout: Per-request timeout in milliseconds.

        Returns:
            The normalized result dict (``content``, ``input_tokens``,
            ``output_tokens``, ``ttft_ms``, ``e2e_latency_ms``).

        Raises:
            OmniRouteUnavailableError: When the deployment is
                unreachable (network error, HTTP >= 400, bad envelope,
                or a missing fixture in fixture mode). The message is
                safe to log: no API key, no request body.
        """
        timeout_ms = timeout or self.default_timeout_ms

        if self.fixture_mode:
            started = time.monotonic()
            try:
                payload = self._read_fixture(model)
            except _FixtureMissError as exc:
                self._record_failure(model, model, started)
                raise OmniRouteUnavailableError(
                    [(model, model)], {(model, model): "no fixture"}
                ) from exc
            e2e = (time.monotonic() - started) * 1000.0
            fixture_status = _as_number(payload.get("status", 200))
            if fixture_status is not None and fixture_status >= 400:
                self._record_failure(model, model, started)
                raise OmniRouteUnavailableError(
                    [(model, model)],
                    {(model, model): f"fixture status {int(fixture_status)}"},
                )
            try:
                result = _parse_envelope(payload)
            except ValueError as exc:
                self._record_failure(model, model, started)
                raise OmniRouteUnavailableError(
                    [(model, model)], {(model, model): f"bad fixture: {exc}"}
                ) from exc
            if result["e2e_latency_ms"] is None:
                result["e2e_latency_ms"] = max(e2e, 0.01)
            if self.telemetry is not None:
                self.telemetry.record_success(
                    provider=model,
                    model=model,
                    ttft_ms=result["ttft_ms"],
                    e2e_latency_ms=result["e2e_latency_ms"],
                    input_tokens=result["input_tokens"],
                    output_tokens=result["output_tokens"],
                    estimated_cost_usd=0.0,
                )
            return result

        if not self.base_url:
            raise OmniRouteUnavailableError(
                [(model, model)], {(model, model): "NERVE_OMNIROUTE_URL not configured"}
            )

        request_body = {
            "model": model,
            "prompt": prompt,
            "parameters": dict(parameters or {}),
            "timeout_ms": timeout_ms,
        }
        started = time.monotonic()
        try:
            response = self._http_request(
                method="POST",
                path="/v1/inference",
                body=request_body,
                timeout_s=timeout_ms / 1000.0,
            )
        except OmniRouteUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 — transport-level catch-all
            reason = type(exc).__name__
            self._record_failure(model, model, started)
            raise OmniRouteUnavailableError(
                [(model, model)], {(model, model): reason}
            ) from exc

        status = getattr(response, "status_code", 0)
        if status >= 400:
            self._record_failure(model, model, started)
            raise OmniRouteUnavailableError(
                [(model, model)], {(model, model): f"http_{status}"}
            )

        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("response body is not a JSON object")
            result = _parse_envelope(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            self._record_failure(model, model, started)
            raise OmniRouteUnavailableError(
                [(model, model)], {(model, model): f"bad_envelope: {exc}"}
            ) from exc

        if result["e2e_latency_ms"] is None:
            result["e2e_latency_ms"] = (time.monotonic() - started) * 1000.0
        if self.telemetry is not None:
            self.telemetry.record_success(
                provider=model,
                model=model,
                ttft_ms=result["ttft_ms"],
                e2e_latency_ms=result["e2e_latency_ms"],
                input_tokens=result["input_tokens"],
                output_tokens=result["output_tokens"],
                estimated_cost_usd=0.0,
            )
        return result

    # ------------------------------------------------------------------
    # Route Candidate Interface (spec §32)
    # ------------------------------------------------------------------

    def route_candidates(self, timeout_s: float | None = None) -> list[RouteCandidate]:
        """Fetch the ordered route-candidate list from the deployment.

        Fixture mode replays ``candidates.json`` from the fixture dir.

        Raises:
            OmniRouteUnavailableError: When unreachable or the envelope
                cannot be parsed.
        """
        if self.fixture_mode:
            try:
                payload = self._read_candidates_fixture()
            except _FixtureMissError as exc:
                raise OmniRouteUnavailableError(
                    [("omniroute", "candidates")],
                    {("omniroute", "candidates"): "no candidates fixture"},
                ) from exc
            raw_routes = payload.get("routes")
        else:
            if not self.base_url:
                raise OmniRouteUnavailableError(
                    [("omniroute", "candidates")],
                    {("omniroute", "candidates"): "NERVE_OMNIROUTE_URL not configured"},
                )
            timeout = timeout_s or (self.default_timeout_ms / 1000.0)
            try:
                response = self._http_request(
                    method="GET", path="/v1/routes", body=None, timeout_s=timeout
                )
            except Exception as exc:  # noqa: BLE001
                raise OmniRouteUnavailableError(
                    [("omniroute", "candidates")],
                    {("omniroute", "candidates"): type(exc).__name__},
                ) from exc
            if getattr(response, "status_code", 0) >= 400:
                raise OmniRouteUnavailableError(
                    [("omniroute", "candidates")],
                    {
                        (
                            "omniroute",
                            "candidates",
                        ): f"http_{getattr(response, 'status_code', 0)}"
                    },
                )
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise OmniRouteUnavailableError(
                    [("omniroute", "candidates")],
                    {("omniroute", "candidates"): f"bad_envelope: {exc}"},
                ) from exc
            raw_routes = payload.get("routes")

        if not isinstance(payload, dict) or not isinstance(raw_routes, list):
            raise OmniRouteUnavailableError(
                [("omniroute", "candidates")],
                {("omniroute", "candidates"): "bad_envelope: missing routes list"},
            )
        candidates = []
        for item in raw_routes:
            if isinstance(item, dict):
                fields = {k: v for k, v in item.items() if k in RouteCandidate.model_fields}
                candidates.append(RouteCandidate(**fields))
        return candidates

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _http_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        timeout_s: float,
    ) -> Any:
        """Perform one HTTP request, injecting the auth header (never logged)."""
        client = self._client if self._client is not None else httpx.Client(
            timeout=timeout_s
        )
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        assert self.base_url is not None
        url = self.base_url.rstrip("/") + path
        request = client.build_request(method, url, json=body, headers=headers)
        return client.send(request)

    def _record_failure(self, provider: str, model: str, started: float) -> None:
        if self.telemetry is not None:
            self.telemetry.record_error(
                provider=provider,
                model=model,
                e2e_latency_ms=(time.monotonic() - started) * 1000.0,
            )

    def _read_fixture(self, model: str) -> dict[str, Any]:
        """Resolve and load the fixture for one hop.

        Resolution order: ``<model>.json``, then any provider-scoped
        ``*--<model>.json`` (first alphabetically). Raises
        :class:`_FixtureMissError` when nothing matches.
        """
        assert self.fixture_dir is not None
        base = Path(self.fixture_dir)
        plain = base / f"{model}.json"
        if plain.is_file():
            return self._load_fixture(plain)
        scoped = sorted(base.glob(f"*--{model}.json"))
        if scoped:
            return self._load_fixture(scoped[0])
        raise _FixtureMissError(f"no fixture for model {model}")

    def _read_candidates_fixture(self) -> dict[str, Any]:
        assert self.fixture_dir is not None
        path = Path(self.fixture_dir) / "candidates.json"
        if not path.is_file():
            raise _FixtureMissError("no candidates fixture")
        return self._load_fixture(path)

    @staticmethod
    def _load_fixture(path: Path) -> dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise _FixtureMissError(f"fixture {path.name} is not a JSON object")
        return data


class OmniRouteAdapter(BaseModel):
    """Routes inference requests to appropriate backends.

    The adapter translates routing decisions into actual calls to
    inference providers. It handles provider-specific authentication,
    request formatting, and response parsing.

    Usage
    -----
    >>> adapter = OmniRouteAdapter(
    ...     clients={
    ...         "openai": OpenAIClient(api_key="..."),
    ...         "anthropic": AnthropicClient(api_key="...")
    ...     }
    ... )
    >>> response = adapter.route(route, request)

    The real v0 transport is an injected client in its own right (ADR-0010):

    >>> transport = OmniRouteTransport.from_env()
    >>> adapter.add_provider("omniroute", transport)
    """

    model_config = {"extra": "forbid"}

    clients: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific HTTP clients.",
    )
    default_timeout: int = Field(
        default=30000,
        ge=1,
        description="Default timeout in milliseconds.",
    )

    def add_provider(self, name: str, client: Any) -> None:
        """Add or replace a provider client."""
        self.clients[name] = client

    def route(self, route: InferenceRoute, request: InferenceRequest) -> InferenceResponse:
        """Route an inference request through the fallback chain.

        Args:
            route: The routing decision to use.
            request: The inference request to route.

        Returns:
            The response from the successful provider.

        Raises:
            OmniRouteUnavailableError: If all providers in the fallback
                chain fail (or no provider is configured).
        """
        providers_tried = []
        reasons: dict[tuple[str, str], str] = {}

        # Try primary provider first
        result = self._call_provider(
            route.primary_provider,
            route.primary_model,
            request,
            reasons,
        )
        if result:
            self._record_telemetry(route.primary_provider, route.primary_model, result, True)
            return result

        providers_tried.append((route.primary_provider, route.primary_model))

        # Try fallback hops
        for hop in route.hops:
            result = self._call_provider(hop.provider, hop.model, request, reasons)
            if result:
                self._record_telemetry(hop.provider, hop.model, result, True)
                return result
            providers_tried.append((hop.provider, hop.model))

        self._record_telemetry(
            route.primary_provider,
            route.primary_model,
            None,
            False,
            providers_tried,
        )
        raise OmniRouteUnavailableError(providers_tried, reasons)

    def _call_provider(
        self,
        provider: str,
        model: str,
        request: InferenceRequest,
        reasons: dict[tuple[str, str], str] | None = None,
    ) -> InferenceResponse | None:
        """Call a single provider. Returns None on failure."""
        if provider not in self.clients:
            if reasons is not None:
                reasons[(provider, model)] = "no_client"
            return None

        client = self.clients[provider]
        timeout = request.timeout_ms or self.default_timeout

        try:
            result = client.generate(
                model=model,
                prompt=request.prompt,
                parameters=request.parameters,
                timeout=timeout,
            )
            return InferenceResponse(
                provider=provider,
                model=model,
                content=result["content"],
                input_tokens=result.get("input_tokens", 0),
                output_tokens=result.get("output_tokens", 0),
                ttft_ms=result.get("ttft_ms"),
                e2e_latency_ms=result.get("e2e_latency_ms"),
            )
        except Exception as exc:  # noqa: BLE001 — provider boundary
            if reasons is not None:
                reasons[(provider, model)] = type(exc).__name__
            return None

    def _record_telemetry(
        self,
        provider: str,
        model: str,
        response: InferenceResponse | None,
        success: bool,
        providers_tried: list[tuple[str, str]] | None = None,
    ) -> None:
        """Record telemetry for policy refinement."""
        sample = OmniRouteTelemetry(
            id=f"telemetry-{provider}-{model}",
            provider=provider,
            model=model,
            sample_at=datetime.now(UTC),
            requests=1,
            success_rate=1.0 if success else 0.0,
            ttft_ms=response.ttft_ms if response else None,
            tokens_per_second=None,
            e2e_latency_ms=response.e2e_latency_ms if response else None,
            p95_latency_ms=None,
            errors=0 if success else 1,
            circuit_state="open" if not success else "closed",
        )

        # Store telemetry for policy engine to consume
        if not hasattr(self, "_telemetry_history"):
            self._telemetry_history: dict[tuple[str, str], list[OmniRouteTelemetry]] = {}
        key = (provider, model)
        if key not in self._telemetry_history:
            self._telemetry_history[key] = []
        self._telemetry_history[key].append(sample)

    def get_telemetry(self) -> dict[tuple[str, str], list[OmniRouteTelemetry]]:
        """Get accumulated telemetry samples."""
        if not hasattr(self, "_telemetry_history"):
            self._telemetry_history = {}
        return self._telemetry_history
