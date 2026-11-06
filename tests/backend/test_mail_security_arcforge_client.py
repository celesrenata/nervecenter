"""NerveShield task 3.5 -- ArcForge client + pool, tested ONLY against a local fake.

Honesty: every test talks to ``arcforge_fake_server.FakeArcForge`` on 127.0.0.1.
Passing proves the client/pool behave as specified against that fake. It proves
NOTHING about the real hardware, its latency, or its real failure modes.
"""

from __future__ import annotations

import ast
import json
import logging
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import arcforge_fake_server as F
import pytest

from backend.mail_security.classifier import arcforge_client as C
from backend.mail_security.classifier import synthetic
from backend.mail_security.classifier.features import FeatureRecord

CANARY = "CANARY_ZXQ_RULE"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def records(n: int = 2) -> list[FeatureRecord]:
    return [e.features for e in synthetic.generate_examples(seed=3, count=n)]


def canary_record() -> FeatureRecord:
    return FeatureRecord(finding_rule_counts={CANARY: 2}, url_count=1)


@pytest.fixture
def fake() -> Iterator[F.FakeArcForge]:
    with F.FakeArcForge(dim=16) as f:
        yield f


@pytest.fixture
def fake2() -> Iterator[F.FakeArcForge]:
    with F.FakeArcForge(dim=16) as f:
        yield f


def pool_for(*fakes: F.FakeArcForge, clock: FakeClock | None = None, **kw) -> C.ArcForgePool:
    cfg_kw = {k: v for k, v in kw.items() if k in C.PoolConfig.__dataclass_fields__}
    client_kw = {k: v for k, v in kw.items() if k in C.ClientConfig.__dataclass_fields__}
    if client_kw:
        cfg_kw["client"] = C.ClientConfig(**client_kw)
    cfg = C.PoolConfig(**cfg_kw)
    ck = clock or FakeClock()
    return C.ArcForgePool([f.url for f in fakes], cfg, clock=ck, sleep=ck.sleep)


def identity_of(f: F.FakeArcForge) -> C.ModelIdentity:
    return C.ModelIdentity(f.model_id, f.version, f.dim, f.device)


# --------------------------------------------------------------------------- #
# target validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8100",
        "http://127.0.0.1:8100/",
        "http://10.1.1.12:8100",
        "http://172.16.0.5:8100",
        "http://172.31.255.1:8100",
        "http://192.168.1.5:8100",
        "http://[::1]:8100",
        "http://[fd00::1]:8100",
        "https://10.0.0.1:8443",
    ],
)
def test_private_targets_accepted(url: str) -> None:
    t = C.parse_target(url)
    assert t.port in (8100, 8443)


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8:8100",
        "http://172.32.0.1:8100",
        "http://11.0.0.1:8100",
        "http://169.254.169.254:80",
        "http://0.0.0.0:8100",
        "http://[2001:db8::1]:8100",
        "http://[fe80::1]:8100",
        "http://[::ffff:127.0.0.1]:8100",
        "http://[::ffff:10.0.0.1]:8100",
        "http://[::ffff:8.8.8.8]:8100",
        "http://[::ffff:7f00:1]:8100",
        "http://localhost:8100",
        "http://example.com:8100",
        "http://arc-host.lan:8100",
        "http://2130706433:8100",
        "http://0x7f.0.0.1:8100",
        "http://0x7f000001:8100",
        "http://017700000001:8100",
        "http://127.1:8100",
        "http://010.0.0.1:8100",
        "http://user:secretpw@127.0.0.1:8100",
        "http://@127.0.0.1:8100",
        "http://127.0.0.1:8100/?a=1",
        "http://127.0.0.1:8100?a=1",
        "http://127.0.0.1:8100/#frag",
        "http://127.0.0.1:8100/v1/embeddings",
        "http://127.0.0.1:8100;x",
        "ftp://127.0.0.1:8100",
        "file:///etc/passwd",
        "127.0.0.1:8100",
        "//127.0.0.1:8100",
        "http://127.0.0.1:0",
        "http://127.0.0.1:70000",
        "http://127.0.0.1:abc",
        "http://127.0.0.1:-1",
        "http://",
        "",
        "http://127.0.0.1:8100 ",
        "http://127.0.0.1\n:8100",
        "http://127.0.0.1\u2024:8100",
    ],
)
def test_bad_targets_refused_without_echo(url: str) -> None:
    with pytest.raises(C.TargetError) as exc:
        C.parse_target(url)
    assert "secretpw" not in str(exc.value)
    assert "127.0.0.1" not in str(exc.value)


def test_non_string_target_refused() -> None:
    with pytest.raises(C.TargetError):
        C.parse_target(None)  # type: ignore[arg-type]


def test_pool_refuses_bad_target_and_empty_targets() -> None:
    with pytest.raises(C.TargetError):
        C.ArcForgePool(["http://127.0.0.1:1", "http://example.com:1"])
    with pytest.raises(ValueError):
        C.ArcForgePool([])


def test_no_dns_lookup_ever(monkeypatch: pytest.MonkeyPatch, fake: F.FakeArcForge) -> None:
    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("resolver must never be called")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "gethostbyname", boom)
    with pytest.raises(C.TargetError):
        C.parse_target("http://example.com:8100")
    pool = pool_for(fake)
    assert pool.embed(records(2)).outcome is C.Outcome.OK
    assert pool.client_for("host1").metrics().ok


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        C.ClientConfig(max_items=33)
    with pytest.raises(ValueError):
        C.ClientConfig(max_items=0)
    with pytest.raises(ValueError):
        C.PoolConfig(per_host_concurrency=5)
    with pytest.raises(ValueError):
        C.PoolConfig(per_host_concurrency=0)
    assert C.ClientConfig().max_items == 16
    assert C.PoolConfig().per_host_concurrency == 2


# --------------------------------------------------------------------------- #
# request bounds (nothing is sent when a bound is exceeded)
# --------------------------------------------------------------------------- #
def test_too_many_items_sends_nothing(fake: F.FakeArcForge) -> None:
    pool = pool_for(fake, max_items=2)
    r = pool.embed(records(3))
    assert r.outcome is C.Outcome.TOO_LARGE and r.embeddings == ()
    assert fake.connections == 0


def test_item_chars_and_request_bytes_bounds(fake: F.FakeArcForge) -> None:
    assert pool_for(fake, max_chars_per_item=5).embed(records(1)).code == "ITEM_TOO_LONG"
    assert pool_for(fake, max_request_bytes=40).embed(records(2)).code == "REQUEST_TOO_LARGE"
    assert fake.embed_requests == 0


def test_client_level_bounds_without_pool(fake: F.FakeArcForge) -> None:
    client = C.ArcForgeClient(C.parse_target(fake.url), C.ClientConfig(max_items=1))
    r = client.embed(records(2), identity_of(fake))
    assert r.outcome is C.Outcome.TOO_LARGE
    assert client.embed([], identity_of(fake)).code == "EMPTY_BATCH"
    assert fake.connections == 0


def test_empty_batch_and_wrong_type(fake: F.FakeArcForge) -> None:
    pool = pool_for(fake)
    assert pool.embed([]).outcome is C.Outcome.TOO_LARGE
    with pytest.raises(TypeError):
        pool.embed(["raw text is refused"])  # type: ignore[list-item]
    assert fake.connections == 0


def test_server_413_is_too_large(fake: F.FakeArcForge) -> None:
    fake.embed_fault = "413"
    r = pool_for(fake).embed(records(1))
    assert r.outcome is C.Outcome.TOO_LARGE and r.http_status == 413 and r.embeddings == ()


def test_happy_path_validates_and_reports_infer_s(fake: F.FakeArcForge) -> None:
    pool = pool_for(fake)
    r = pool.embed(records(3))
    assert r.outcome is C.Outcome.OK and len(r.embeddings) == 3
    assert all(len(v) == 16 for v in r.embeddings)
    assert r.infer_s == pytest.approx(fake.infer_s)
    assert r.host == "host1" and r.attempts == 1
    assert pool.identity == identity_of(fake)


def test_default_dim_1024_contract() -> None:
    with F.FakeArcForge() as f:
        r = pool_for(f).embed(records(2))
        assert r.ok and len(r.embeddings[0]) == 1024


# --------------------------------------------------------------------------- #
# strict response validation: typed error, never a partial result
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("wrong_dim", "BAD_DIM"),
        ("short_data", "BAD_DATA_LENGTH"),
        ("long_data", "BAD_DATA_LENGTH"),
        ("nan", "BAD_JSON"),
        ("inf", "BAD_JSON"),
        ("overflow", "NON_FINITE"),
        ("non_unit", "BAD_NORM"),
        ("dup_index", "BAD_INDEX"),
        ("misorder", "BAD_INDEX"),
        ("bool_value", "BAD_VALUE"),
        ("not_object", "BAD_SHAPE"),
        ("truncated", "TRUNCATED"),
        ("truncated_json", "BAD_JSON"),
        ("content_type", "BAD_CONTENT_TYPE"),
        ("huge", "RESPONSE_TOO_LARGE"),
        ("identity_change", "IDENTITY_CHANGED"),
    ],
)
def test_bad_responses_are_typed_errors(fake: F.FakeArcForge, fault: str, code: str) -> None:
    fake.embed_fault = fault
    r = pool_for(fake).embed(records(3))
    assert r.outcome is C.Outcome.BAD_RESPONSE
    assert r.code == code
    assert r.embeddings == () and r.infer_s is None
    assert fake.embed_requests == 1  # BAD_RESPONSE is never retried


def test_bad_infer_s_rejected(fake: F.FakeArcForge) -> None:
    fake.infer_s = -1.0
    assert pool_for(fake).embed(records(1)).code == "BAD_INFER_S"


# --------------------------------------------------------------------------- #
# transport errors
# --------------------------------------------------------------------------- #
def test_http_500_and_400_are_http_error_with_status_only(fake: F.FakeArcForge) -> None:
    fake.embed_fault = "500"
    r = pool_for(fake).embed(records(1))
    assert (r.outcome, r.http_status, r.code) == (C.Outcome.HTTP_ERROR, 500, "HTTP_500")
    fake.embed_fault = "400"
    r = pool_for(fake).embed(records(1))
    assert (r.outcome, r.http_status) == (C.Outcome.HTTP_ERROR, 400)


def test_slow_response_times_out(fake: F.FakeArcForge) -> None:
    fake.delay_s = 0.6
    pool = pool_for(fake, read_timeout_s=0.1, total_deadline_s=2.0)
    pool.check_all()
    t0 = time.monotonic()
    r = pool.embed(records(1))
    assert r.outcome is C.Outcome.TIMEOUT and r.embeddings == ()
    assert time.monotonic() - t0 < 0.5


def test_connection_refused_and_reset(fake: F.FakeArcForge) -> None:
    client = C.ArcForgeClient(C.parse_target(fake.url))
    ident = identity_of(fake)
    fake.embed_fault = "reset"
    r = client.embed(records(1), ident)
    assert r.outcome is C.Outcome.CONNECT_ERROR and r.retryable
    dead = F.FakeArcForge(dim=16).start()
    url = dead.url
    dead.stop()
    r2 = C.ArcForgeClient(C.parse_target(url)).embed(records(1), ident)
    assert r2.outcome is C.Outcome.CONNECT_ERROR and r2.retryable


def test_total_deadline_enforced_with_injected_clock(fake: F.FakeArcForge) -> None:
    ticks = iter(range(0, 10_000, 100))
    client = C.ArcForgeClient(
        C.parse_target(fake.url), C.ClientConfig(total_deadline_s=1.0), clock=lambda: next(ticks)
    )
    r = client.embed(records(1), identity_of(fake))
    assert r.outcome is C.Outcome.TIMEOUT and r.code == "DEADLINE"
    assert fake.connections == 0


# --------------------------------------------------------------------------- #
# ready / models / metrics
# --------------------------------------------------------------------------- #
def test_ready_models_metrics_contract(fake: F.FakeArcForge) -> None:
    c = C.ArcForgeClient(C.parse_target(fake.url))
    assert c.ready().ok
    m = c.models()
    assert m.identity == identity_of(fake)
    pool_for(fake).embed(records(2))
    s = c.metrics().samples
    assert s['arcforge_embedding_requests_total{status="ok"}'] >= 1
    assert "arcforge_embedding_inference_seconds_sum" in s


def test_not_ready_and_wedged_distinguished(fake: F.FakeArcForge) -> None:
    c = C.ArcForgeClient(C.parse_target(fake.url))
    fake.ready = False
    assert c.ready().outcome is C.Outcome.NOT_READY
    fake.ready = True
    fake.wedged = True
    fake.breaker_reason = "probe failed"
    r = c.ready()
    assert r.outcome is C.Outcome.WEDGED and r.http_status == 503 and r.has_breaker_reason
    assert "probe failed" not in repr(r)


def test_ready_and_models_bad_payloads(fake: F.FakeArcForge) -> None:
    c = C.ArcForgeClient(C.parse_target(fake.url))
    fake.ready_fault = "badjson"
    assert c.ready().outcome is C.Outcome.BAD_RESPONSE
    fake.ready_fault = "500"
    assert c.ready().outcome is C.Outcome.HTTP_ERROR
    fake.models_fault = "empty"
    assert c.models().outcome is C.Outcome.BAD_RESPONSE
    fake.models_fault = "badjson"
    assert c.models().outcome is C.Outcome.BAD_RESPONSE
    fake.models_fault = "500"
    assert c.models().outcome is C.Outcome.HTTP_ERROR


def test_wedged_single_host_is_circuit_open_and_receives_no_embeddings(
    fake: F.FakeArcForge,
) -> None:
    fake.wedged = True
    pool = pool_for(fake)
    r = pool.embed(records(1))
    assert r.outcome is C.Outcome.CIRCUIT_OPEN and r.embeddings == ()
    assert fake.embed_requests == 0


# --------------------------------------------------------------------------- #
# breaker / health with injected clock
# --------------------------------------------------------------------------- #
def test_breaker_opens_half_opens_and_closes(fake: F.FakeArcForge) -> None:
    clock = FakeClock()
    pool = pool_for(fake, clock=clock, failure_threshold=3, cooldown_s=30.0)
    fake.embed_fault = "500"
    for _ in range(3):
        assert pool.embed(records(1)).outcome is C.Outcome.HTTP_ERROR
    assert pool.status()[0].state is C.HostState.OPEN
    sent = fake.embed_requests
    assert pool.embed(records(1)).outcome is C.Outcome.CIRCUIT_OPEN
    assert fake.embed_requests == sent  # nothing sent while open
    # half-open probe fails while still wedged -> stays open with a fresh cooldown
    fake.wedged = True
    clock.now += 31
    assert pool.embed(records(1)).outcome is C.Outcome.CIRCUIT_OPEN
    assert pool.status()[0].state is C.HostState.OPEN
    # recovery: cooldown elapsed again, probe passes, breaker closes
    fake.wedged = False
    fake.embed_fault = None
    clock.now += 31
    assert pool.embed(records(1)).outcome is C.Outcome.OK
    st = pool.status()[0]
    assert st.state is C.HostState.CLOSED and st.consecutive_failures == 0


def test_4xx_does_not_trip_breaker(fake: F.FakeArcForge) -> None:
    pool = pool_for(fake, failure_threshold=2)
    fake.embed_fault = "400"
    for _ in range(5):
        pool.embed(records(1))
    assert pool.status()[0].state is C.HostState.CLOSED


def test_wedged_host_skipped_then_recovered(fake: F.FakeArcForge, fake2: F.FakeArcForge) -> None:
    clock = FakeClock()
    fake.wedged = True
    pool = pool_for(fake, fake2, clock=clock)
    for _ in range(3):
        assert pool.embed(records(1)).host == "host2"
    assert fake.embed_requests == 0
    fake.wedged = False
    clock.now += 31
    assert pool.embed(records(1)).host == "host1"
    assert pool.status()[0].state is C.HostState.CLOSED


def test_identity_change_mid_run_quarantines(fake: F.FakeArcForge, fake2: F.FakeArcForge) -> None:
    clock = FakeClock()
    pool = pool_for(fake, fake2, clock=clock)
    assert pool.embed(records(1)).host == "host1"
    fake.version = "qwen3-embedding-0.6b-fp16"
    pool.check_all()
    assert pool.status()[0].state is C.HostState.QUARANTINED
    for _ in range(3):
        assert pool.embed(records(1)).host == "host2"
    clock.now += 1000  # cooldown never rescues a quarantined host
    fake.version = F.DEFAULT_VERSION
    assert pool.embed(records(1)).host == "host2"
    assert pool.status()[0].state is C.HostState.QUARANTINED


def test_dim_change_and_response_identity_change_quarantine(fake: F.FakeArcForge) -> None:
    pool = pool_for(fake)
    assert pool.embed(records(1)).ok
    fake.dim = 8
    pool.check_all()
    assert pool.status()[0].state is C.HostState.QUARANTINED
    assert pool.embed(records(1)).outcome is C.Outcome.NOT_AVAILABLE


def test_embedding_response_identity_change_quarantines(fake: F.FakeArcForge) -> None:
    pool = pool_for(fake)
    assert pool.embed(records(1)).ok
    fake.embed_fault = "identity_change"
    assert pool.embed(records(1)).code == "IDENTITY_CHANGED"
    assert pool.status()[0].state is C.HostState.QUARANTINED


def test_host_with_different_identity_at_start_is_quarantined(
    fake: F.FakeArcForge, fake2: F.FakeArcForge
) -> None:
    fake2.model_id = "other-model"
    pool = pool_for(fake, fake2)
    pool.check_all()
    states = [s.state for s in pool.status()]
    assert states == [C.HostState.CLOSED, C.HostState.QUARANTINED]
    assert pool.identity is not None and pool.identity.model_id == F.DEFAULT_MODEL


# --------------------------------------------------------------------------- #
# retry policy
# --------------------------------------------------------------------------- #
def test_retry_once_on_different_host_after_5xx(
    fake: F.FakeArcForge, fake2: F.FakeArcForge
) -> None:
    fake.embed_fault = "500"
    r = pool_for(fake, fake2).embed(records(1))
    assert r.ok and r.host == "host2" and r.attempts == 2
    assert (fake.embed_requests, fake2.embed_requests) == (1, 1)


def test_retry_on_connect_error_and_timeout(fake: F.FakeArcForge, fake2: F.FakeArcForge) -> None:
    fake.embed_fault = "reset"
    r = pool_for(fake, fake2).embed(records(1))
    assert r.ok and r.host == "host2"
    fake.embed_fault = None
    fake.delay_s = 0.5
    pool = pool_for(fake, fake2, read_timeout_s=0.1)
    pool.check_all()
    r = pool.embed(records(1))
    assert r.ok and r.host == "host2" and r.attempts == 2


def test_no_retry_on_4xx(fake: F.FakeArcForge, fake2: F.FakeArcForge) -> None:
    fake.embed_fault = "400"
    r = pool_for(fake, fake2).embed(records(1))
    assert r.outcome is C.Outcome.HTTP_ERROR and r.http_status == 400
    assert fake2.embed_requests == 0


def test_no_retry_after_partial_or_invalid_response(
    fake: F.FakeArcForge, fake2: F.FakeArcForge
) -> None:
    for fault in ("truncated", "wrong_dim", "nan"):
        fake.embed_fault = fault
        r = pool_for(fake, fake2).embed(records(1))
        assert r.outcome is C.Outcome.BAD_RESPONSE
    assert fake2.embed_requests == 0


def test_retry_happens_at_most_once(fake: F.FakeArcForge, fake2: F.FakeArcForge) -> None:
    with F.FakeArcForge(dim=16) as f3:
        for f in (fake, fake2, f3):
            f.embed_fault = "500"
        r = pool_for(fake, fake2, f3).embed(records(1))
        assert r.outcome is C.Outcome.HTTP_ERROR and r.attempts == 2
        assert fake.embed_requests + fake2.embed_requests + f3.embed_requests == 2


def test_no_retry_without_a_different_healthy_host(
    fake: F.FakeArcForge, fake2: F.FakeArcForge
) -> None:
    fake2.wedged = True
    fake.embed_fault = "500"
    r = pool_for(fake, fake2).embed(records(1))
    assert r.outcome is C.Outcome.HTTP_ERROR and r.attempts == 1
    assert fake.embed_requests == 1 and fake2.embed_requests == 0


# --------------------------------------------------------------------------- #
# concurrency, selection, backpressure
# --------------------------------------------------------------------------- #
def _run_threads(pool: C.ArcForgePool, n: int) -> list[C.EmbedResult]:
    out: list[C.EmbedResult] = []
    lock = threading.Lock()

    def work() -> None:
        r = pool.embed(records(1))
        with lock:
            out.append(r)

    threads = [threading.Thread(target=work) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return out


@pytest.mark.parametrize("cap", [1, 2, 4])
def test_per_host_concurrency_cap_never_exceeded(fake: F.FakeArcForge, cap: int) -> None:
    fake.delay_s = 0.05
    pool = C.ArcForgePool(
        [fake.url], C.PoolConfig(per_host_concurrency=cap, max_waiting=32, queue_timeout_s=20)
    )
    results = _run_threads(pool, 10)
    assert len(results) == 10 and all(r.ok for r in results)
    assert fake.max_concurrent <= cap
    assert pool.status()[0].max_outstanding <= cap


def test_cap_holds_per_host_across_two_hosts(fake: F.FakeArcForge, fake2: F.FakeArcForge) -> None:
    for f in (fake, fake2):
        f.delay_s = 0.05
    pool = C.ArcForgePool(
        [fake.url, fake2.url],
        C.PoolConfig(per_host_concurrency=2, max_waiting=32, queue_timeout_s=20),
    )
    results = _run_threads(pool, 16)
    assert all(r.ok for r in results)
    assert fake.max_concurrent <= 2 and fake2.max_concurrent <= 2
    assert fake.embed_requests > 0 and fake2.embed_requests > 0


def test_least_outstanding_spreads_load(fake: F.FakeArcForge, fake2: F.FakeArcForge) -> None:
    for f in (fake, fake2):
        f.delay_s = 0.3
    pool = C.ArcForgePool([fake.url, fake2.url], C.PoolConfig(per_host_concurrency=2))
    pool.check_all()
    t1 = threading.Thread(target=lambda: pool.embed(records(1)))
    t1.start()
    deadline = time.monotonic() + 5
    while fake.embed_requests + fake2.embed_requests < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    r = pool.embed(records(1))
    t1.join()
    assert r.ok
    assert (fake.embed_requests, fake2.embed_requests) == (1, 1)


def test_backpressure_queue_is_bounded(fake: F.FakeArcForge) -> None:
    fake.delay_s = 0.6
    pool = C.ArcForgePool(
        [fake.url], C.PoolConfig(per_host_concurrency=1, max_waiting=1, queue_timeout_s=10)
    )
    pool.check_all()
    results: list[C.EmbedResult] = []
    lock = threading.Lock()

    def work() -> None:
        r = pool.embed(records(1))
        with lock:
            results.append(r)

    def wait_for(pred) -> None:  # noqa: ANN001
        end = time.monotonic() + 5
        while not pred() and time.monotonic() < end:
            time.sleep(0.005)

    t1 = threading.Thread(target=work)
    t1.start()
    wait_for(lambda: pool.status()[0].outstanding == 1)
    t2 = threading.Thread(target=work)
    t2.start()
    wait_for(lambda: pool._waiting == 1)
    rejected = [pool.embed(records(1)) for _ in range(3)]
    t1.join()
    t2.join()
    assert all(r.outcome is C.Outcome.QUEUE_FULL and r.embeddings == () for r in rejected)
    assert sorted(r.outcome for r in results) == [C.Outcome.OK, C.Outcome.OK]
    assert fake.max_concurrent == 1
    assert fake.embed_requests == 2  # rejected calls were never buffered or sent


def test_queue_wait_timeout_is_typed(fake: F.FakeArcForge) -> None:
    fake.delay_s = 0.5
    pool = C.ArcForgePool(
        [fake.url], C.PoolConfig(per_host_concurrency=1, max_waiting=2, queue_timeout_s=0.05)
    )
    pool.check_all()
    t = threading.Thread(target=lambda: pool.embed(records(1)))
    t.start()
    end = time.monotonic() + 5
    while pool.status()[0].outstanding < 1 and time.monotonic() < end:
        time.sleep(0.005)
    r = pool.embed(records(1))
    t.join()
    assert r.outcome is C.Outcome.TIMEOUT and r.code == "QUEUE_WAIT_TIMEOUT"


# --------------------------------------------------------------------------- #
# isolation + privacy
# --------------------------------------------------------------------------- #
def test_requests_carry_no_identifiers_and_nothing_is_cached(fake: F.FakeArcForge) -> None:
    pool = pool_for(fake)
    recs = records(2)
    a = pool.embed(recs)
    b = pool.embed(recs)
    assert a.ok and b.ok
    assert fake.embed_requests == 2  # no response caching
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_.- ")
    for raw in fake.captured_bodies:
        body = json.loads(raw)
        assert set(body) == {"model", "input"}
        for text in body["input"]:
            assert isinstance(text, str) and set(text) <= allowed and "@" not in text
        for banned in (b"account", b"message", b"tenant", b"user", b"subject", b"mailbox"):
            assert banned not in raw.lower()
    assert fake.captured_bodies[0] == fake.captured_bodies[1]


@pytest.mark.parametrize(
    "fault",
    [
        "500",
        "400",
        "413",
        "reset",
        "wrong_dim",
        "nan",
        "truncated",
        "content_type",
        "identity_change",
        "huge",
    ],
)
def test_no_text_urls_or_server_body_in_results_logs_or_errors(
    fake: F.FakeArcForge, caplog: pytest.LogCaptureFixture, fault: str
) -> None:
    caplog.set_level(logging.DEBUG)
    fake.embed_fault = fault
    pool = pool_for(fake)
    r = pool.embed([canary_record()])
    assert not r.ok
    blob = repr(r) + repr(pool.status()) + caplog.text
    for forbidden in (CANARY, F.SERVER_BODY_MARKER, "127.0.0.1", str(fake.port)):
        assert forbidden not in blob


def test_no_text_in_connect_error_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    dead = F.FakeArcForge(dim=16).start()
    url, port = dead.url, dead.port
    dead.stop()
    pool = C.ArcForgePool([url], C.PoolConfig(), clock=FakeClock(), sleep=FakeClock().sleep)
    r = pool.embed([canary_record()])
    assert not r.ok
    blob = repr(r) + repr(pool.status()) + caplog.text
    assert CANARY not in blob and "127.0.0.1" not in blob and str(port) not in blob


def test_failure_never_yields_embeddings_or_cpu_fallback(fake: F.FakeArcForge) -> None:
    fake.wedged = True
    r = pool_for(fake).embed(records(2))
    assert not r.ok and r.embeddings == ()
    assert not hasattr(r, "score") and not hasattr(r, "fallback")


# --------------------------------------------------------------------------- #
# static: stdlib only, no subprocess/env
# --------------------------------------------------------------------------- #
def test_module_is_stdlib_only_without_subprocess_or_env() -> None:
    src = Path(C.__file__).read_text()
    tree = ast.parse(src)
    stdlib = set(sys.stdlib_module_names)
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods = [node.module]
        for m in mods:
            top = m.split(".")[0]
            assert top in stdlib or top == "backend", m
    for banned in ("subprocess", "os.environ", "getenv", "getaddrinfo", "gethostbyname"):
        assert banned not in src.replace("(no DNS", "")
