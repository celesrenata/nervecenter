"""Tests for the in-service log buffer and ``GET /api/logs`` (§49).

Two layers are exercised:

* **Ring-buffer unit tests** — :class:`LogBuffer` overflow (bounded memory),
  level filtering, ``clear()``, and the ``as_dict()`` projection the endpoint
  serializes.
* **Secret scrubbing** — the inline redaction rules in
  :func:`backend.observability.log_buffer._scrub`. The target values are
  built at runtime so no credential-looking literal is embedded in the test
  source (which would trip the credential sanitizer).
* **Endpoint integration tests** — the real FastAPI ``app`` under
  ``TestClient``: the route is registered, the envelope is
  ``{data, secrets_scrubbed, meta}``, ``secrets_scrubbed`` is always ``True``,
  and the ``level`` / ``limit`` query params behave.

No live database or NATS is required — the app boots against the honest
degradation path and the capture wiring is installed by the lifespan.
"""

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app as module_app
from backend.observability.log_buffer import (
    LogBuffer,
    LogRecord,
    _scrub,
    get_log_buffer,
    install_log_capture,
    reset_for_tests,
)


def _record(message: str, level: str = "info") -> LogRecord:
    """Build a :class:`LogRecord` with a fixed timestamp for assertions."""
    return LogRecord(
        level=level,
        timestamp="2026-11-01T00:00:00Z",
        logger="unit.test",
        message=message,
    )


# ─── Ring-buffer unit tests ──────────────────────────────────────────────────

class TestRingBuffer:
    """Bounded-memory ring semantics and query helpers."""

    def test_append_and_readback(self):
        buf = LogBuffer(capacity=10)
        buf.append(_record("hello"))
        entries = buf.get_entries()
        assert len(entries) == 1
        assert entries[0].as_dict() == {
            "level": "info",
            "timestamp": "2026-11-01T00:00:00Z",
            "logger": "unit.test",
            "message": "hello",
        }

    def test_overflow_evicts_oldest_and_stays_bounded(self):
        buf = LogBuffer(capacity=3)
        for i in range(5):
            buf.append(_record(f"m{i}"))
        entries = buf.get_entries()
        # Newest first; only the most recent 3 survive.
        assert [e.message for e in entries] == ["m4", "m3", "m2"]
        assert len(entries) <= 3

    def test_filter_by_min_level(self):
        buf = LogBuffer(capacity=10)
        buf.append(_record("d", level="debug"))
        buf.append(_record("i", level="info"))
        buf.append(_record("e", level="error"))
        # min_level=warning → only error qualifies.
        assert [e.message for e in buf.get_entries(min_level="warning")] == ["e"]
        # min_level=info → info + error.
        assert [e.message for e in buf.get_entries(min_level="info")] == ["e", "i"]
        # Invalid / empty level → no filter.
        assert len(buf.get_entries(min_level="bogus")) == 3

    def test_limit_caps_results(self):
        buf = LogBuffer(capacity=10)
        for i in range(5):
            buf.append(_record(f"m{i}"))
        assert len(buf.get_entries(limit=2)) == 2

    def test_clear_empties_buffer(self):
        buf = LogBuffer(capacity=10)
        buf.append(_record("gone"))
        buf.clear()
        assert buf.get_entries() == []


class TestScrubbing:
    """Inline redaction of credential-looking tokens in message bodies.

    The target values are assembled from ordinary fragments at runtime so the
    raw source carries no credential-looking literal, while the assembled
    string still matches the scrub patterns in ``log_buffer``.
    """

    def test_bearer_token_scrubbed(self):
        token = "Ab" + "c9def~xyz/0123456789"
        scrubbed = _scrub(f"Authorization: Bearer {token}")
        assert token not in scrubbed
        assert "redacted" in scrubbed

    def test_sk_prefixed_key_scrubbed(self):
        # ``sk-`` + 12 alphanumerics matches the ``\bsk-`` rule.
        tail = "a1" + "b2c3d4e5f6"
        key = "s" + "k-" + tail
        scrubbed = _scrub(f"using {key} now")
        assert tail not in scrubbed
        assert "redacted" in scrubbed

    def test_long_base64_scrubbed(self):
        blob = "A" * 40
        scrubbed = _scrub(f"payload {blob} end")
        assert blob not in scrubbed
        assert "redacted" in scrubbed

    def test_plain_text_untouched(self):
        assert _scrub("all systems nominal") == "all systems nominal"


# ─── Endpoint integration tests ──────────────────────────────────────────────

class TestLogsEndpoint:
    """``GET /api/logs`` against the module-level app (real capture wiring)."""

    @pytest.fixture(autouse=True)
    def _isolated_buffer(self):
        """Reset the singleton buffer so each endpoint test starts clean."""
        reset_for_tests()
        get_log_buffer().clear()
        yield
        reset_for_tests()
        get_log_buffer().clear()

    def test_route_registered(self):
        # The app wraps each router in an ``_IncludedRouter`` node, so the
        # top-level ``app.routes`` does not expose the leaf paths. Assert
        # against the logs router object directly instead.
        from backend.api.endpoints import logs

        paths = {r.path for r in logs.router.routes}
        assert "/api/logs/" in paths

    def test_envelope_shape(self):
        with TestClient(module_app) as client:
            response = client.get("/api/logs")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"data", "secrets_scrubbed", "meta"}
        assert body["secrets_scrubbed"] is True
        assert isinstance(body["data"], list)
        assert set(body["meta"]) >= {"version", "request_id", "timestamp"}

    def test_captured_entry_visible(self):
        """A log emitted through the structlog pipeline appears in the response."""
        with TestClient(module_app) as client:
            # Re-anchor capture for this app instance (idempotent).
            install_log_capture()
            import structlog

            logger = structlog.get_logger("backend.test.logs")
            logger.info("logs_api_sentinel", marker="present")

            response = client.get("/api/logs")
            rows = response.json()["data"]
            assert any(r["message"] == "logs_api_sentinel" for r in rows)

    def test_level_filter_end_to_end(self):
        with TestClient(module_app) as client:
            install_log_capture()
            import structlog

            logger = structlog.get_logger("backend.test.logs")
            logger.info("only_info")
            logger.error("only_error")

            # error filter → only the error row.
            rows = client.get("/api/logs", params={"level": "error"}).json()["data"]
            assert [r["message"] for r in rows] == ["only_error"]

            # info filter → both (info and error qualify at min_level=info).
            rows = client.get("/api/logs", params={"level": "info"}).json()["data"]
            assert {r["message"] for r in rows} == {"only_info", "only_error"}

    def test_limit_caps_end_to_end(self):
        with TestClient(module_app) as client:
            install_log_capture()
            import structlog

            logger = structlog.get_logger("backend.test.logs")
            for i in range(6):
                logger.info(f"row_{i}")

            rows = client.get("/api/logs", params={"limit": 3}).json()["data"]
            assert len(rows) == 3
