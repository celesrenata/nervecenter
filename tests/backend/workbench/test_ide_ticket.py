"""nc_ide ticket cookie: signed, slug-bound, expiring."""
from backend.workbench import ide_ticket as t


def test_valid_ticket_for_its_slug_only():
    tok = t.issue("alice", "proj-a", now=1000)
    assert t.verify(tok, "proj-a", now=1001)
    assert not t.verify(tok, "proj-b", now=1001)


def test_expired_and_tampered_tickets_rejected():
    tok = t.issue("alice", "proj-a", now=1000, ttl=10)
    assert not t.verify(tok, "proj-a", now=1011)
    head, sig = tok.rsplit(".", 1)
    assert not t.verify(head + "." + ("A" if sig[0] != "A" else "B") + sig[1:], "proj-a", now=1001)
    forged = tok.replace(".1010.", ".9999999999.")
    assert not t.verify(forged, "proj-a", now=1001)


def test_from_headers_reads_cookie():
    tok = t.issue("alice", "proj-a")
    assert t.from_headers({"cookie": f"other=1; {t.COOKIE_NAME}={tok}"}, "proj-a")
    assert not t.from_headers({"cookie": f"{t.COOKIE_NAME}={tok}"}, "proj-b")
    assert not t.from_headers({}, "proj-a")
    assert not t.from_headers({"cookie": "garbage;;=="}, "proj-a")


def test_ws_upstream_headers_drop_forwarded_host():
    from backend.workbench.ide_proxy import filter_request_headers

    h = {"x-forwarded-host": "nervecenter.celestium.life", "x-forwarded-proto": "https"}
    assert "x-forwarded-host" not in filter_request_headers(h.items(), websocket=True)
    assert "x-forwarded-host" in filter_request_headers(h.items())
