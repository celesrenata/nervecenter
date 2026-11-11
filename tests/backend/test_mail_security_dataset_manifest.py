"""NerveShield task 6.2: signed manifest and leakage-safe splits.

Done criterion: the manifest is reproducible and a leakage test passes (no
thread or campaign in both splits, no train example later than any eval one).
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.mail_security.dataset_manifest import (
    DropReason as Drop,
)
from backend.mail_security.dataset_manifest import (
    ExampleMeta,
    HumanReview,
    ManifestError,
    Split,
    assign_splits,
    build_manifest,
    sign_manifest,
    verify_manifest,
)
from backend.mail_security.training_selection import (
    SelectedExample,
    SelectionManifest,
    SourceKind,
    TrainingLabel,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 7, 1, tzinfo=UTC)
NOW = datetime(2026, 11, 11, tzinfo=UTC)
REVIEW = HumanReview("rev-1", NOW)


def _sel(n: int, label: TrainingLabel = TrainingLabel.BENIGN) -> SelectedExample:
    return SelectedExample(
        f"ex-{n:03d}",
        label,
        f"fb-{n}:0",
        "consent-1",
        SourceKind.TAKEOUT_IMPORT,
        f"pseudonymised text {n}",
    )


def _meta(day: int, thread: str, campaign: str) -> ExampleMeta:
    return ExampleMeta(T0 + timedelta(days=day), thread, campaign)


def _selection() -> SelectionManifest:
    return SelectionManifest(
        "mail-training-selection@1", "gmail:a", NOW, candidates=10, included=10
    )


def _random_corpus(seed: int, n: int = 300) -> tuple[list[SelectedExample], dict[str, ExampleMeta]]:
    """Realistic shape: short threads (1-4 messages within ~10 days), campaigns that
    span a few threads close in time, and a handful of campaigns that cross the cutoff."""
    rng = random.Random(seed)
    examples, meta = [], {}
    i = 0
    thread = 0
    campaign_base: dict[str, int] = {}
    while i < n:
        campaign = f"c{thread // 3}"
        anchor = campaign_base.setdefault(campaign, rng.randint(0, 320))
        base = anchor + rng.randint(0, 15)
        for _ in range(rng.randint(1, 4)):
            if i >= n:
                break
            ex = _sel(i, rng.choice(list(TrainingLabel)))
            examples.append(ex)
            meta[ex.example_id] = _meta(base + rng.randint(0, 10), f"t{thread}", campaign)
            i += 1
        thread += 1
    return examples, meta


# -- leakage ----------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(20))
def test_no_thread_campaign_or_time_leakage(seed: int) -> None:
    examples, meta = _random_corpus(seed)
    assigned, dropped = assign_splits(examples, meta, cutoff=CUTOFF, embargo=timedelta(days=14))
    assert set(assigned) | set(dropped) == {e.example_id for e in examples}
    assert not set(assigned) & set(dropped)
    by_split = {s: [meta[e] for e, v in assigned.items() if v is s] for s in Split}
    assert len(by_split[Split.TRAIN]) > 100 and len(by_split[Split.EVAL]) > 40  # not vacuous
    assert dropped  # the corpus does exercise straddling / embargo groups
    for key in ("thread_key", "campaign_key"):
        train = {getattr(m, key) for m in by_split[Split.TRAIN]}
        evals = {getattr(m, key) for m in by_split[Split.EVAL]}
        assert not train & evals, key
    if by_split[Split.TRAIN] and by_split[Split.EVAL]:
        latest_train = max(m.received_at for m in by_split[Split.TRAIN])
        earliest_eval = min(m.received_at for m in by_split[Split.EVAL])
        assert latest_train < CUTOFF - timedelta(days=14) <= CUTOFF <= earliest_eval


def test_group_straddling_cutoff_is_dropped_as_a_whole() -> None:
    examples = [_sel(1), _sel(2), _sel(3)]
    meta = {
        "ex-001": _meta(10, "t1", "c1"),
        "ex-002": _meta(250, "t1", "c2"),  # same thread, after cutoff
        "ex-003": _meta(300, "t9", "c2"),  # same campaign as ex-002
    }
    assigned, dropped = assign_splits(examples, meta, cutoff=CUTOFF)
    assert assigned == {}
    assert set(dropped.values()) == {Drop.STRADDLES_CUTOFF}


def test_embargo_window_is_dropped() -> None:
    examples = [_sel(1), _sel(2), _sel(3)]
    meta = {
        "ex-001": _meta(10, "a", "a"),
        "ex-002": _meta(178, "b", "b"),
        "ex-003": _meta(200, "c", "c"),
    }
    assigned, dropped = assign_splits(examples, meta, cutoff=CUTOFF, embargo=timedelta(days=7))
    assert assigned == {"ex-001": Split.TRAIN, "ex-003": Split.EVAL}
    assert dropped == {"ex-002": Drop.IN_EMBARGO}


def test_missing_or_bad_metadata_is_dropped() -> None:
    examples = [_sel(1), _sel(2), _sel(3)]
    meta = {
        "ex-002": ExampleMeta(datetime(2026, 1, 1), "t", "c"),
        "ex-003": _meta(1, "has space", "c"),
    }
    _, dropped = assign_splits(examples, meta, cutoff=CUTOFF)
    assert dropped == {e: Drop.MISSING_METADATA for e in ("ex-001", "ex-002", "ex-003")}


# -- reproducibility ---------------------------------------------------------------------


def _body(examples: list[SelectedExample], meta: dict[str, ExampleMeta]) -> dict:
    return build_manifest("ds-1", _selection(), examples, meta, cutoff=CUTOFF, created_at=NOW)


def test_manifest_is_reproducible_and_order_independent() -> None:
    examples, meta = _random_corpus(7)
    a = _body(examples, meta)
    shuffled = examples[:]
    random.Random(1).shuffle(shuffled)
    b = _body(shuffled, dict(reversed(list(meta.items()))))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    key = Ed25519PrivateKey.from_private_bytes(b"k" * 32)
    assert sign_manifest(a, REVIEW, key).digest == sign_manifest(b, REVIEW, key).digest


def test_manifest_holds_provenance_not_text() -> None:
    examples, meta = _random_corpus(3, n=40)
    body = build_manifest(
        "ds-1",
        _selection(),
        examples,
        meta,
        cutoff=CUTOFF,
        created_at=NOW,
        weak_label_hints={"ex-000": ["INBOX", "SPAM", "INBOX"]},
    )
    text = json.dumps(body)
    assert "pseudonymised text" not in text
    for e in body["entries"]:
        assert e["label_source"] == "HUMAN_FEEDBACK" and e["label_proposal_id"].startswith("fb-")
        assert len(e["content_sha256"]) == 64
    hinted = [e for e in body["entries"] if "weak_hints" in e]
    if hinted:
        assert hinted[0]["weak_hints"] == {
            "source": "PROVIDER_EXPORT",
            "strength": "WEAK",
            "used_as_label": False,
            "codes": ["INBOX", "SPAM"],
        }
    total = sum(sum(v.values()) for v in body["counts"].values()) + sum(body["dropped"].values())
    assert total == len(examples)


def test_duplicate_ids_and_naive_times_rejected() -> None:
    with pytest.raises(ManifestError):
        build_manifest("ds-1", _selection(), [_sel(1), _sel(1)], {}, cutoff=CUTOFF, created_at=NOW)
    with pytest.raises(ManifestError):
        assign_splits([], {}, cutoff=datetime(2026, 7, 1))
    with pytest.raises(ManifestError):
        build_manifest("bad id", _selection(), [], {}, cutoff=CUTOFF, created_at=NOW)


# -- signing / immutability --------------------------------------------------------------


@pytest.fixture
def signed() -> tuple[str, bytes]:
    examples, meta = _random_corpus(5, n=60)
    key = Ed25519PrivateKey.generate()
    manifest = sign_manifest(_body(examples, meta), REVIEW, key)
    return manifest.to_json(), key.public_key().public_bytes_raw()


def test_valid_manifest_verifies(signed: tuple[str, bytes]) -> None:
    raw, pub = signed
    body = verify_manifest(raw, pub)
    assert body["review"]["reviewer_ref"] == "rev-1"


@pytest.mark.parametrize(
    "tamper",
    [
        lambda d: d["body"]["entries"][0].update(
            split="TRAIN" if d["body"]["entries"][0]["split"] == "EVAL" else "EVAL"
        ),
        lambda d: d["body"]["entries"][0].update(
            label="MALICIOUS" if d["body"]["entries"][0]["label"] == "BENIGN" else "BENIGN"
        ),
        lambda d: d["body"]["entries"].pop(),
        lambda d: d["body"]["split_rule"].update(cutoff="2026-01-01T00:00:00+00:00"),
        lambda d: d["body"]["review"].update(reviewer_ref="someone-else"),
        lambda d: d["body"]["counts"].clear(),
    ],
)
def test_any_change_breaks_verification(signed: tuple[str, bytes], tamper) -> None:
    raw, pub = signed
    doc = json.loads(raw)
    tamper(doc)
    with pytest.raises(ManifestError):
        verify_manifest(json.dumps(doc), pub)


def test_recomputed_digest_still_fails_signature(signed: tuple[str, bytes]) -> None:
    import hashlib

    raw, pub = signed
    doc = json.loads(raw)
    doc["body"]["entries"].pop()
    doc["digest"] = hashlib.sha256(
        json.dumps(doc["body"], sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    with pytest.raises(ManifestError, match="bad signature"):
        verify_manifest(json.dumps(doc), pub)


def test_embedded_key_is_not_trusted(signed: tuple[str, bytes]) -> None:
    """Re-signing with an attacker key embeds that key; verification uses the trusted one."""
    raw, trusted = signed
    doc = json.loads(raw)
    forged = sign_manifest({**doc["body"], "review": None}, REVIEW, Ed25519PrivateKey.generate())
    assert forged.public_key != doc["public_key"]
    with pytest.raises(ManifestError, match="bad signature"):
        verify_manifest(forged.to_json(), trusted)


def test_signing_requires_an_approving_review() -> None:
    examples, meta = _random_corpus(2, n=10)
    body = _body(examples, meta)
    key = Ed25519PrivateKey.generate()
    with pytest.raises(ManifestError):
        sign_manifest(body, HumanReview("rev-1", NOW, decision="REJECTED"), key)
    with pytest.raises(ManifestError):
        sign_manifest(body, HumanReview("bad ref", NOW), key)
    assert body["review"] is None  # caller's body untouched
    signed = sign_manifest(body, REVIEW, key)
    with pytest.raises(ManifestError):
        sign_manifest(signed.body, REVIEW, key)


def test_unreviewed_body_does_not_verify() -> None:
    examples, meta = _random_corpus(2, n=10)
    key = Ed25519PrivateKey.generate()
    signed = sign_manifest(_body(examples, meta), REVIEW, key)
    doc = json.loads(signed.to_json())
    doc["body"]["review"] = None
    with pytest.raises(ManifestError):
        verify_manifest(json.dumps(doc), key.public_key().public_bytes_raw())


def test_not_wired_into_the_backend() -> None:
    backend = Path(__file__).resolve().parents[2] / "backend"
    offenders = [
        str(p.relative_to(backend))
        for p in backend.rglob("*.py")
        if p.name != "dataset_manifest.py" and "dataset_manifest" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_a_naive_per_message_time_split_would_leak_on_this_corpus() -> None:
    """Guards the leakage test: across the seeds it uses, campaigns do cross the cutoff."""
    leaking = 0
    for seed in range(20):
        _, meta = _random_corpus(seed)
        train = {m.campaign_key for m in meta.values() if m.received_at < CUTOFF}
        evals = {m.campaign_key for m in meta.values() if m.received_at >= CUTOFF}
        leaking += bool(train & evals)
    assert leaking >= 10
