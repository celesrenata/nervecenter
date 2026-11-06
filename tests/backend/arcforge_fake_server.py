"""Local fake of the ArcForge embedding service (test support only).

Binds 127.0.0.1 on an ephemeral port. It mimics the verified contract of the real
service (GET /ready, GET /v1/models, POST /v1/embeddings, GET /metrics) with
scriptable faults, and records what it received so tests can assert on request
bodies and observed concurrency. It never contacts anything. The embeddings are
deterministic pseudo-random unit vectors: they carry NO meaning, they only let the
client/pool/harness code be exercised without hardware.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SERVER_BODY_MARKER = "SERVER_BODY_MARKER_Q9"
DEFAULT_MODEL = "qwen3-embedding-0.6b"
DEFAULT_VERSION = "qwen3-embedding-0.6b-int8"


class FakeArcForge:
    """One fake service instance. Mutate attributes between calls to script faults."""

    def __init__(self, *, dim: int = 1024, model_id: str = DEFAULT_MODEL) -> None:
        self.dim = dim
        self.model_id = model_id
        self.version = DEFAULT_VERSION
        self.device = "GPU"
        self.ready = True
        self.wedged = False
        self.breaker_reason = ""
        self.max_batch = 32
        self.delay_s = 0.0  # added to every embeddings request
        self.infer_s = 0.036
        #: sticky fault applied to every embeddings request (None = healthy)
        self.embed_fault: str | None = None
        #: one-shot faults consumed in order, before ``embed_fault``
        self.fault_script: list[str] = []
        self.ready_fault: str | None = None  # "500" | "badjson" | "reset"
        self.models_fault: str | None = None  # "500" | "badjson" | "empty"
        self._lock = threading.Lock()
        self.captured_bodies: list[bytes] = []
        self.request_log: list[tuple[str, str]] = []  # (method, path)
        self.embed_requests = 0
        self.ready_requests = 0
        self.connections = 0
        self.max_concurrent = 0
        self._active = 0
        self.items_total = 0
        self.status_counts: dict[str, int] = {"ok": 0, "error": 0}
        self.infer_sum = 0.0
        self.infer_count = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> FakeArcForge:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args: Any) -> None:  # silence
                return

            def setup(self) -> None:
                super().setup()
                with fake._lock:
                    fake.connections += 1

            def do_GET(self) -> None:  # noqa: N802
                fake._get(self)

            def do_POST(self) -> None:  # noqa: N802
                fake._post(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> FakeArcForge:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _send(h: BaseHTTPRequestHandler, status: int, body: bytes, ctype: str) -> None:
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        try:
            h.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client refused an oversize/aborted body; expected in some fault tests

    def _send_json(self, h: BaseHTTPRequestHandler, status: int, obj: Any) -> None:
        self._send(h, status, json.dumps(obj).encode(), "application/json")

    @staticmethod
    def _reset(h: BaseHTTPRequestHandler) -> None:
        h.request.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        h.close_connection = True
        h.request.close()

    def _get(self, h: BaseHTTPRequestHandler) -> None:
        with self._lock:
            self.request_log.append(("GET", h.path))
        if h.path == "/ready":
            with self._lock:
                self.ready_requests += 1
            if self.ready_fault == "reset":
                return self._reset(h)
            if self.ready_fault == "500":
                return self._send_json(h, 500, {"error": "x"})
            if self.ready_fault == "badjson":
                return self._send(h, 200, b"{not json", "application/json")
            ok = self.ready and not self.wedged
            body = {
                "status": "ok" if ok else "unavailable",
                "model": self.model_id,
                "device": self.device,
                "ready": self.ready and not self.wedged,
                "wedged": self.wedged,
                "breaker_reason": self.breaker_reason,
            }
            return self._send_json(h, 200 if ok else 503, body)
        if h.path == "/v1/models":
            if self.models_fault == "500":
                return self._send_json(h, 500, {"error": "x"})
            if self.models_fault == "badjson":
                return self._send(h, 200, b"[[", "application/json")
            data = [] if self.models_fault == "empty" else [self._model_entry()]
            return self._send_json(h, 200, {"object": "list", "data": data})
        if h.path == "/metrics":
            return self._send(h, 200, self.metrics_text().encode(), "text/plain; version=0.0.4")
        self._send_json(h, 404, {"error": "not found"})

    def _model_entry(self) -> dict[str, Any]:
        return {
            "id": self.model_id,
            "object": "model",
            "owned_by": "arcforge",
            "arcforge": {
                "device": self.device,
                "dim": self.dim,
                "max_length": 4096,
                "version": self.version,
            },
        }

    def metrics_text(self) -> str:
        with self._lock:
            lines = [
                "# HELP arcforge_embedding_requests_total requests",
                "# TYPE arcforge_embedding_requests_total counter",
            ]
            for status, n in sorted(self.status_counts.items()):
                lines.append(f'arcforge_embedding_requests_total{{status="{status}"}} {n}')
            lines += [
                f"arcforge_embedding_items_total {self.items_total}",
                f"arcforge_embedding_inference_seconds_count {self.infer_count}",
                f"arcforge_embedding_inference_seconds_sum {self.infer_sum}",
                "arcforge_embedding_queue_depth 0",
                "arcforge_embedding_active_inferences 0",
                f"arcforge_device_wedged {1 if self.wedged else 0}",
                "arcforge_model_compile_seconds 12.5",
                'arcforge_watchdog_probe_total{result="ok"} 3',
            ]
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ #
    def _vector(self, text: str, dim: int) -> list[float]:
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
        rng = random.Random(seed)
        vec = [rng.gauss(0.0, 1.0) for _ in range(dim)]
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec]

    def _next_fault(self) -> str | None:
        with self._lock:
            if self.fault_script:
                return self.fault_script.pop(0)
        return self.embed_fault

    def _post(self, h: BaseHTTPRequestHandler) -> None:
        length = int(h.headers.get("Content-Length", "0"))
        raw = h.rfile.read(length)
        with self._lock:
            self.request_log.append(("POST", h.path))
            if h.path == "/v1/embeddings":
                self.captured_bodies.append(raw)
                self.embed_requests += 1
                self._active += 1
                self.max_concurrent = max(self.max_concurrent, self._active)
        if h.path != "/v1/embeddings":
            return self._send_json(h, 404, {"error": "not found"})
        try:
            self._embeddings(h, raw)
        finally:
            with self._lock:
                self._active -= 1

    def _error(self, h: BaseHTTPRequestHandler, status: int, code: str) -> None:
        with self._lock:
            self.status_counts["error"] += 1
        err = {
            "error": {"message": SERVER_BODY_MARKER, "type": "invalid_request_error", "code": code}
        }
        self._send_json(h, status, err)

    def _embeddings(self, h: BaseHTTPRequestHandler, raw: bytes) -> None:
        fault = self._next_fault()
        if self.delay_s:
            time.sleep(self.delay_s)
        if fault == "reset":
            return self._reset(h)
        if fault == "500":
            return self._error(h, 500, "internal")
        if fault == "503":
            return self._error(h, 503, "unavailable")
        if fault == "400":
            return self._error(h, 400, "bad_request")
        if fault == "413":
            return self._error(h, 413, "batch_too_large")
        try:
            req = json.loads(raw)
            texts = req["input"]
            assert isinstance(texts, list)
        except Exception:
            return self._error(h, 400, "bad_request")
        if len(texts) > self.max_batch:
            return self._error(h, 413, "batch_too_large")

        dim = self.dim
        data = []
        for i, t in enumerate(texts):
            data.append(
                {
                    "object": "embedding",
                    "index": i,
                    "model": self.model_id,
                    "embedding": self._vector(str(t), dim),
                }
            )
        body: dict[str, Any] = {
            "object": "list",
            "model": self.model_id,
            "data": data,
            "usage": {
                "prompt_tokens": None,
                "total_tokens": None,
                "arcforge": {"dim": dim, "items": len(texts), "infer_s": self.infer_s},
            },
        }
        ctype = "application/json"
        if fault == "wrong_dim":
            for d in data:
                d["embedding"] = d["embedding"][:-1]
        elif fault == "short_data":
            body["data"] = data[:-1] if len(data) > 1 else []
        elif fault == "long_data":
            body["data"] = [*data, data[-1]]
        elif fault == "non_unit":
            for d in data:
                d["embedding"] = [v * 2.0 for v in d["embedding"]]
        elif fault == "dup_index":
            if len(data) > 1:
                data[1]["index"] = 0
        elif fault == "misorder":
            if len(data) > 1:
                data[0]["index"], data[1]["index"] = 1, 0
        elif fault == "bool_value":
            data[0]["embedding"][0] = True
        elif fault == "identity_change":
            body["model"] = "someone-else"
            for d in data:
                d["model"] = "someone-else"
        elif fault == "not_object":
            return self._send(h, 200, b"[1, 2]", ctype)
        elif fault == "truncated":
            full = json.dumps(body).encode()
            h.send_response(200)
            h.send_header("Content-Type", ctype)
            h.send_header("Content-Length", str(len(full)))
            h.end_headers()
            h.wfile.write(full[: len(full) // 2])
            h.close_connection = True
            return
        elif fault == "truncated_json":
            full = json.dumps(body).encode()
            return self._send(h, 200, full[: len(full) // 2], ctype)
        elif fault == "content_type":
            ctype = "text/html"
        elif fault == "huge":
            return self._send(h, 200, b" " * (5 * 1024 * 1024) + b"{}", ctype)

        text = json.dumps(body)
        if fault == "nan":
            text = text.replace(str(data[0]["embedding"][0]), "NaN", 1)
        elif fault == "inf":
            text = text.replace(str(data[0]["embedding"][0]), "Infinity", 1)
        elif fault == "overflow":
            text = text.replace(str(data[0]["embedding"][0]), "1e999", 1)
        with self._lock:
            self.status_counts["ok"] += 1
            self.items_total += len(texts)
            self.infer_sum += self.infer_s
            self.infer_count += 1
        self._send(h, 200, text.encode(), ctype)
