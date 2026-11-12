"""Spike 8.1: privacy-safe campaign fingerprints (synthetic evidence, no corpus)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from backend.mail_security.research import campaign_clustering as cc

T0 = datetime(2026, 11, 2, tzinfo=UTC)
SECRET = b"s" * 32


@dataclass
class _Link:
    target: str


@dataclass
class _Att:
    filename: str


@dataclass
class _Part:
    subtype: str
    normalized: str = "BODY-CANARY dear customer"


@dataclass
class _Ev:
    headers: list
    links: list = field(default_factory=list)
    attachments: list = field(default_factory=list)
    text_parts: list = field(default_factory=list)
    images: list = field(default_factory=list)
    part_count: int = 2
    max_depth_seen: int = 1


def _phish(i: int) -> _Ev:
    return _Ev(
        headers=[
            ("From", f"Billing <billing{i}@secure-pay-{i % 2}.com>"),
            ("Subject", f"URGENT: Invoice #{1000 + i} overdue!"),
            ("Reply-To", "x@y.com"),
            ("X-Mailer", "Mass 1.0"),
        ],
        links=[
            _Link(f"http://pay-portal.net/inv/{1000 + i}/login"),
            _Link("http://pay-portal.net/help"),
        ],
        attachments=[_Att(f"invoice_{i}.zip")],
        text_parts=[_Part("html")],
    )


def _newsletter(i: int) -> _Ev:
    return _Ev(
        headers=[
            ("From", "News <news@example-news.org>"),
            ("Subject", f"Weekly digest {i}"),
            ("List-Unsubscribe", "<mailto:u@example-news.org>"),
            ("List-Id", "news"),
        ],
        links=[_Link(f"https://example-news.org/a/{i}"), _Link("https://example-news.org/u")],
        text_parts=[_Part("plain"), _Part("html")],
        part_count=3,
    )


def _sig(ev: _Ev, when: datetime = T0) -> tuple[int, ...]:
    key = cc.epoch_key(SECRET, when)
    return cc.minhash(cc.keyed(cc.structural_tokens(ev), key), key)


def test_tokens_carry_no_words_addresses_or_urls():
    toks = cc.structural_tokens(_phish(3))
    blob = " ".join(sorted(toks))
    for needle in (
        "Invoice",
        "URGENT",
        "overdue",
        "billing3",
        "BODY-CANARY",
        "customer",
        "/inv/1003/login",
        "invoice_3",
        "Mass 1.0",
        "x@y.com",
    ):
        assert needle not in blob, needle
    assert any(t.startswith("ss:") for t in toks)  # subject *shape* only
    assert cc.shape("URGENT: Invoice #1003 overdue!") == "A: Aa #9 a!"


def test_same_campaign_clusters_and_newsletters_stay_apart():
    sigs = {f"p{i}": _sig(_phish(i)) for i in range(8)}
    sigs.update({f"n{i}": _sig(_newsletter(i)) for i in range(8)})
    roots = cc.cluster(sigs, threshold=0.6)
    assert len({roots[f"p{i}"] for i in range(8)}) <= 2  # two sender domains
    assert {roots[f"p{i}"] for i in range(8)}.isdisjoint({roots[f"n{i}"] for i in range(8)})


def test_keyed_fingerprints_are_unlinkable_across_epochs():
    ev = _phish(1)
    k1 = cc.keyed(cc.structural_tokens(ev), cc.epoch_key(SECRET, T0))
    k2 = cc.keyed(cc.structural_tokens(ev), cc.epoch_key(SECRET, T0 + timedelta(days=7)))
    assert k1 and not (k1 & k2)
    assert cc.similarity(_sig(ev, T0), _sig(ev, T0 + timedelta(days=7))) < 0.2
    # Same epoch -> identical.
    assert _sig(ev, T0) == _sig(ev, T0 + timedelta(days=1))
    # Different secret -> unlinkable even in the same epoch.
    other = cc.keyed(cc.structural_tokens(ev), cc.epoch_key(b"o" * 32, T0))
    assert not (k1 & other)


def test_store_expires_and_releases_only_k_aggregates():
    store = cc.CampaignStore(ttl=timedelta(days=30), k=3)
    for d in range(3):
        store.observe("big", (1,), T0 + timedelta(days=d))
    store.observe("small", (2,), T0)
    agg = store.aggregates()
    assert agg == [
        {"size": 3, "first_seen": T0.isoformat(), "last_seen": (T0 + timedelta(days=2)).isoformat()}
    ]
    assert "big" not in str(agg) and "small" not in str(agg)
    assert store.expire(T0 + timedelta(days=31)) == 1  # "small" expired; "big" extended
    assert store.expire(T0 + timedelta(days=33)) == 1
    assert store.clusters == {}


def test_empty_evidence_is_safe():
    assert cc.minhash(frozenset(), b"k" * 32) == ()
    assert cc.similarity((), (1,)) == 0.0
    assert cc.structural_tokens(_Ev(headers=[]))  # shape tokens still present
