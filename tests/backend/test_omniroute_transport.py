"""Tests for the real OmniRoute transport (T003.AC8 / ADR-0010).

Covers: fixture-mode replay, env parsing, the unavailable-error path
(no network — closed localhost ports only), fallback-order behaviour
through the adapter seam, and the telemetry event shape.

No test in this module may reach the real network: network tests use
``127.0.0.1`` ports that are closed, and everything else runs in
fixture mode.
"""

import json
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.inference import (  # noqa: E402
    InferenceRequest,
    OmniRouteAdapter,
    OmniRouteTransport,
    OmniRouteUnavailableError,
    RouteCandidate,
)
from backend.inference.telemetry import InferenceTelemetry  # noqa: E402
from schemas.routes import InferenceRoute, RouteHop  # noqa: E402

_API_KEY = "sk-test-secret-do-not-leak"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    """A recorded-fixture directory (spec: recorded-fixture OmniRoute)."""
    (tmp_path / "primary.json").write_text(
        json.dumps(
            {
                "content": "fixture answer",
                "usage": {"input_tokens": 11, "output_tokens": 7},
                "ttft_ms": 12.5,
                "e2e_latency_ms": 42.0,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "primary.json").write_text(
        json.dumps(
            {
                "content": "fixture answer",
                "usage": {"input_tokens": 11, "output_tokens": 7},
                "ttft_ms": 12.5,
                "e2e_latency_ms": 42.0,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "down.json").write_text(
        json.dumps({"status": 403, "error": "denied"}), encoding="utf-8"
    )
    (tmp_path / "bad.json").write_text(json.dumps({"not": "content"}), encoding="utf-8")
    (tmp_path / "candidates.json").write_text(
        json.dumps(
            {
                "routes": [
                    {"model": "deepseek-v4", "backend": "mlx", "node": "stabulous",
                     "availability": "ready"},
                    {"model": "qwen-code", "backend": "vllm", "node": "esnixi",
                     "availability": "ready", "surprise": "ignored"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def fixture_transport(fixture_dir: Path) -> OmniRouteTransport:
    return OmniRouteTransport(fixture_dir=str(fixture_dir))


def _route(
    primary_provider: str, primary_model: str, hops: list[tuple[str, str]]
) -> InferenceRoute:
    return InferenceRoute(
        id="route-test",
        request_key="req-1",
        primary_provider=primary_provider,
        primary_model=primary_model,
        hops=[RouteHop(id=f"hop-{i}", provider=p, model=m) for i, (p, m) in enumerate(hops)],
    )


def _request() -> InferenceRequest:
    return InferenceRequest(route_key="req-1", prompt="hello", parameters={"temperature": 0.1})


def _closed_port() -> int:
    """Return a localhost TCP port that is (momentarily) closed."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# Fixture-mode replay
# ---------------------------------------------------------------------------


class TestFixtureReplay:
    def test_replays_recorded_json(self, fixture_transport: OmniRouteTransport) -> None:
        result = fixture_transport.generate(model="primary", prompt="hello")
        assert result["content"] == "fixture answer"
        assert result["input_tokens"] == 11
        assert result["output_tokens"] == 7
        assert result["ttft_ms"] == 12.5
        assert result["e2e_latency_ms"] == 42.0

    def test_fixture_mode_flag(self, fixture_transport: OmniRouteTransport) -> None:
        assert fixture_transport.fixture_mode is True
        assert fixture_transport.base_url is None  # no network needed

    def test_missing_fixture_is_unavailable_not_fabricated(
        self, fixture_transport: OmniRouteTransport
    ) -> None:
        with pytest.raises(OmniRouteUnavailableError) as excinfo:
            fixture_transport.generate(model="does-not-exist", prompt="hello")
        assert excinfo.value.attempted == [("does-not-exist", "does-not-exist")]
        assert "unreachable" in str(excinfo.value)

    def test_fixture_error_status_is_failure(self, fixture_transport: OmniRouteTransport) -> None:
        with pytest.raises(OmniRouteUnavailableError) as excinfo:
            fixture_transport.generate(model="down", prompt="hello")
        assert excinfo.value.reasons[("down", "down")] == "fixture status 403"

    def test_bad_envelope_fixture_is_failure(self, fixture_transport: OmniRouteTransport) -> None:
        with pytest.raises(OmniRouteUnavailableError) as excinfo:
            fixture_transport.generate(model="bad", prompt="hello")
        assert "bad fixture" in excinfo.value.reasons[("bad", "bad")]

    def test_candidates_fixture_replay(self, fixture_transport: OmniRouteTransport) -> None:
        candidates = fixture_transport.route_candidates()
        assert [c.model for c in candidates] == ["deepseek-v4", "qwen-code"]
        assert candidates[0].backend == "mlx"
        assert candidates[0].node == "stabulous"
        assert candidates[1].availability == "ready"
        assert "surprise" not in candidates[1].model_dump()

    def test_candidates_missing_fixture_unavailable(self, tmp_path: Path) -> None:
        transport = OmniRouteTransport(fixture_dir=str(tmp_path))
        with pytest.raises(OmniRouteUnavailableError):
            transport.route_candidates()

    def test_provider_scoped_fixture_name(self, tmp_path: Path) -> None:
        (tmp_path / "prov--deepseek-v4.json").write_text(
            json.dumps({"content": "scoped"}), encoding="utf-8"
        )
        transport = OmniRouteTransport(fixture_dir=str(tmp_path))
        assert transport.generate(model="deepseek-v4", prompt="hello")["content"] == "scoped"


# ---------------------------------------------------------------------------
# Env parsing
# ---------------------------------------------------------------------------


class TestEnvParsing:
    def test_from_env_reads_url_and_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NERVE_OMNIROUTE_URL", "http://omni.local:8080/")
        monkeypatch.setenv("NERVE_OMNIROUTE_API_KEY", _API_KEY)
        monkeypatch.delenv("NERVE_OMNIROUTE_FIXTURE", raising=False)

        transport = OmniRouteTransport.from_env()
        assert transport.base_url == "http://omni.local:8080/"
        assert transport.api_key == _API_KEY
        assert transport.fixture_mode is False

    def test_truthy_fixture_flag(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        for value in ("1", "true", "yes", "on"):
            monkeypatch.setenv("NERVE_OMNIROUTE_FIXTURE", value)
            monkeypatch.setenv("NERVE_OMNIROUTE_FIXTURE_DIR", str(tmp_path))
            assert OmniRouteTransport.from_env().fixture_mode is True

    def test_falsey_fixture_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for value in ("", "0", "false", "no"):
            monkeypatch.setenv("NERVE_OMNIROUTE_FIXTURE", value)
            monkeypatch.setenv("NERVE_OMNIROUTE_FIXTURE_DIR", "/nonexistent")
            assert OmniRouteTransport.from_env().fixture_mode is False

    def test_unset_env_is_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NERVE_OMNIROUTE_URL", raising=False)
        monkeypatch.delenv("NERVE_OMNIROUTE_API_KEY", raising=False)
        monkeypatch.delenv("NERVE_OMNIROUTE_FIXTURE", raising=False)

        transport = OmniRouteTransport.from_env()
        assert transport.base_url is None
        assert transport.api_key is None

    def test_api_key_never_in_error_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NERVE_OMNIROUTE_API_KEY", _API_KEY)
        monkeypatch.setenv("NERVE_OMNIROUTE_URL", "http://127.0.0.1:9")
        transport = OmniRouteTransport.from_env()
        with pytest.raises(OmniRouteUnavailableError) as excinfo:
            transport.generate(model="primary", prompt="hello")
        message = str(excinfo.value)
        assert _API_KEY not in message
        assert "127.0.0.1" not in message  # no URL echo either
        assert "primary" in message


# ---------------------------------------------------------------------------
# Unavailable error path (no network — closed localhost ports)
# ---------------------------------------------------------------------------


class TestUnavailableError:
    def test_connection_refused_raises_unavailable(
        self, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        port = _closed_port()
        monkeypatch.setenv("NERVE_OMNIROUTE_URL", f"http://127.0.0.1:{port}")
        transport = OmniRouteTransport.from_env()
        with pytest.raises(OmniRouteUnavailableError) as excinfo:
            transport.generate(model="primary", prompt="hello")
        assert excinfo.value.attempted == [("primary", "primary")]
        reason = excinfo.value.reasons[("primary", "primary")]
        assert len(reason) > 0
        assert _API_KEY not in reason  # no secret in the reason
        assert "127.0.0.1" not in reason  # no URL echo

    def test_http_500_is_unavailable(self, fixture_dir: Path) -> None:
        transport = self._transport_with_fake_client(fixture_dir, status=500)
        with pytest.raises(OmniRouteUnavailableError) as excinfo:
            transport.generate(model="primary", prompt="hello")
        assert excinfo.value.reasons[("primary", "primary")] == "http_500"

    def test_bad_envelope_is_unavailable(self, fixture_dir: Path) -> None:
        transport = self._transport_with_fake_client(fixture_dir, body={"oops": True})
        with pytest.raises(OmniRouteUnavailableError) as excinfo:
            transport.generate(model="primary", prompt="hello")
        assert excinfo.value.reasons[("primary", "primary")].startswith("bad_envelope")

    @staticmethod
    def _transport_with_fake_client(fixture_dir: Path, status: int = 200, body: dict | None = None):
        captured: dict = {}

        class _FakeResponse:
            status_code = status

            def json(self) -> dict:
                return body if body is not None else {}

        class _FakeClient:
            def build_request(self, method, url, json=None, headers=None):
                captured["method"] = method
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers
                return object()

            def send(self, request):
                return _FakeResponse()

        transport = OmniRouteTransport(
            base_url="http://omni.example", api_key=_API_KEY, _client=_FakeClient()
        )
        transport._captured = captured  # type: ignore[attr-defined]
        return transport


# ---------------------------------------------------------------------------
# Fallback order through the adapter seam
# ---------------------------------------------------------------------------


class TestFallbackOrder:
    def test_primary_succeeds_first(self, fixture_dir: Path) -> None:
        calls: list[str] = []

        class _Client:
            def __init__(self, name: str, ok: bool) -> None:
                self.name = name
                self.ok = ok

            def generate(self, model, prompt, parameters, timeout):
                calls.append(f"{self.name}:{model}")
                if not self.ok:
                    raise OmniRouteUnavailableError([(model, model)])
                return {"content": f"from-{self.name}", "input_tokens": 1, "output_tokens": 2}

        adapter = OmniRouteAdapter()
        adapter.add_provider("omni-a", _Client("omni-a", ok=False))
        adapter.add_provider("omni-b", _Client("omni-b", ok=True))

        route = _route("omni-a", "deepseek-v4", [("omni-b", "qwen-code")])
        response = adapter.route(route, _request())

        assert response.provider == "omni-b"
        assert response.model == "qwen-code"
        assert response.content == "from-omni-b"
        assert calls == ["omni-a:deepseek-v4", "omni-b:qwen-code"]  # primary first, then hop

    def test_all_exhausted_raises_with_ordered_candidates(
        self, fixture_dir: Path
    ) -> None:
        calls: list[str] = []

        class _Client:
            def __init__(self, name: str) -> None:
                self.name = name

            def generate(self, model, prompt, parameters, timeout):
                calls.append(f"{self.name}:{model}")
                raise OmniRouteUnavailableError([(model, model)])

        adapter = OmniRouteAdapter()
        adapter.add_provider("omni-a", _Client("omni-a"))
        adapter.add_provider("omni-b", _Client("omni-b"))

        route = _route("omni-a", "deepseek-v4", [("omni-b", "qwen-code")])
        with pytest.raises(OmniRouteUnavailableError) as excinfo:
            adapter.route(route, _request())

        assert excinfo.value.attempted == [
            ("omni-a", "deepseek-v4"),
            ("omni-b", "qwen-code"),
        ]
        assert calls == ["omni-a:deepseek-v4", "omni-b:qwen-code"]
        assert "no_client" not in str(excinfo.value)
        message = str(excinfo.value)
        assert "deepseek-v4" in message and "qwen-code" in message

    def test_missing_client_is_no_client_reason(self) -> None:
        adapter = OmniRouteAdapter()  # no clients at all
        route = _route("ghost", "deepseek-v4", [])
        with pytest.raises(OmniRouteUnavailableError) as excinfo:
            adapter.route(route, _request())
        assert excinfo.value.reasons[("ghost", "deepseek-v4")] == "no_client"


# ---------------------------------------------------------------------------
# Telemetry hooks + event shape
# ---------------------------------------------------------------------------


class TestTelemetry:
    def test_success_recorded_via_public_api(self, fixture_dir: Path) -> None:
        telemetry = InferenceTelemetry()
        transport = OmniRouteTransport(fixture_dir=str(fixture_dir), telemetry=telemetry)
        transport.generate(model="primary", prompt="hello")

        metrics = telemetry.get_metrics("primary", "primary")
        assert metrics.requests == 1
        assert metrics.success_rate == 1.0
        assert metrics.errors == 0
        assert metrics.ttft_ms == 12.5
        assert metrics.e2e_latency_ms == 42.0
        assert metrics.circuit_state == "closed"

    def test_failure_recorded_via_public_api(self, fixture_dir: Path) -> None:
        telemetry = InferenceTelemetry()
        transport = OmniRouteTransport(fixture_dir=str(fixture_dir), telemetry=telemetry)
        with pytest.raises(OmniRouteUnavailableError):
            transport.generate(model="down", prompt="hello")

        metrics = telemetry.get_metrics("down", "down")
        assert metrics.requests == 1
        assert metrics.errors == 1
        assert metrics.success_rate == 0.0
        assert metrics.circuit_state == "open"

    def test_no_telemetry_attached_is_still_clean(self, fixture_dir: Path) -> None:
        transport = OmniRouteTransport(fixture_dir=str(fixture_dir))
        assert transport.telemetry is None
        assert transport.generate(model="primary", prompt="hello")["content"] == "fixture answer"

    def test_adapter_telemetry_event_shape_success(
        self, fixture_dir: Path
    ) -> None:
        adapter = OmniRouteAdapter()
        adapter.add_provider("omni", OmniRouteTransport(fixture_dir=str(fixture_dir)))
        route = _route("omni", "primary", [])

        response = adapter.route(route, _request())

        history = adapter.get_telemetry()
        assert ("omni", "primary") in history
        sample = history[("omni", "primary")][-1]
        assert sample.provider == "omni"
        assert sample.model == "primary"
        assert sample.requests == 1
        assert sample.success_rate == 1.0
        assert sample.e2e_latency_ms == response.e2e_latency_ms
        assert sample.ttft_ms == response.ttft_ms
        assert sample.errors == 0
        assert sample.circuit_state == "closed"
        assert sample.sample_at is not None

    def test_adapter_telemetry_event_shape_failure(self) -> None:
        class _Down:
            def generate(self, model, prompt, parameters, timeout):
                raise OmniRouteUnavailableError([(model, model)])

        adapter = OmniRouteAdapter()
        adapter.add_provider("omni", _Down())
        route = _route("omni", "primary", [("omni2", "secondary")])

        with pytest.raises(OmniRouteUnavailableError):
            adapter.route(route, _request())

        history = adapter.get_telemetry()
        assert ("omni", "primary") in history
        sample = history[("omni", "primary")][-1]
        assert sample.success_rate == 0.0
        assert sample.errors == 1
        assert sample.circuit_state == "open"
        assert sample.e2e_latency_ms is None


# ---------------------------------------------------------------------------
# Wire format (injected fake httpx client — no network)
# ---------------------------------------------------------------------------


class TestWireFormat:
    def test_request_body_and_auth_header(self, fixture_dir: Path) -> None:
        transport = TestUnavailableError._transport_with_fake_client(
            fixture_dir, status=200, body={"content": "ok", "usage": {}}
        )
        result = transport.generate(
            model="deepseek-v4", prompt="hi", parameters={"n": 2}, timeout=1234
        )

        captured = transport._captured  # type: ignore[attr-defined]
        assert captured["method"] == "POST"
        assert captured["url"] == "http://omni.example/v1/inference"
        assert captured["json"] == {
            "model": "deepseek-v4",
            "prompt": "hi",
            "parameters": {"n": 2},
            "timeout_ms": 1234,
        }
        assert captured["headers"]["Authorization"] == f"Bearer {_API_KEY}"
        assert result["content"] == "ok"
        assert result["input_tokens"] == 0

    def test_openai_style_envelope(self, fixture_dir: Path) -> None:
        transport = TestUnavailableError._transport_with_fake_client(
            fixture_dir,
            status=200,
            body={
                "choices": [{"message": {"content": "streamed"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
        )
        result = transport.generate(model="primary", prompt="hi")
        assert result["content"] == "streamed"
        assert result["input_tokens"] == 3
        assert result["output_tokens"] == 4

    def test_route_candidates_wire(self, fixture_dir: Path) -> None:
        transport = TestUnavailableError._transport_with_fake_client(
            fixture_dir,
            status=200,
            body={"routes": [{"model": "m", "backend": "b"}]},
        )
        captured = transport._captured  # type: ignore[attr-defined]
        candidates = transport.route_candidates()
        assert captured["method"] == "GET"
        assert captured["url"] == "http://omni.example/v1/routes"
        assert isinstance(candidates, list) and isinstance(candidates[0], RouteCandidate)
        assert candidates[0].model == "m"
