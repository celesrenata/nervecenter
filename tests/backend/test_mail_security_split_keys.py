"""Closing 6.2 gaps: thread keys, campaign clusterer, signing-key custody."""

from __future__ import annotations

import json
import os
import random
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
from backend.mail_security.signing_keys import (
    FileSigningKeyStore,
    KeyCustodyError,
    key_id,
    pinned_keys,
)
from backend.mail_security.split_keys import campaign_keys, thread_key, thread_root
from backend.mail_security.training_selection import (
    SelectedExample,
    SelectionManifest,
    SourceKind,
    TrainingLabel,
)

SECRET = b"k" * 32
NOW = datetime(2026, 11, 11, tzinfo=UTC)


# -- thread keys -------------------------------------------------------------------------


def test_thread_root_follows_references_then_in_reply_to_then_own_id() -> None:
    assert thread_root("<c@x>", "<b@x>", "<a@x> <b@x>") == "a@x"
    assert thread_root("<c@x>", "<b@x>", None) == "b@x"
    assert thread_root("<c@x>") == "c@x"
    assert thread_root(None) is None


def test_a_whole_thread_shares_one_key() -> None:
    first = thread_key(SECRET, "a", message_id="<a@x>", fallback="s1")
    reply = thread_key(
        SECRET, "a", message_id="<b@x>", in_reply_to="<a@x>", references="<a@x>", fallback="s2"
    )
    deep = thread_key(
        SECRET,
        "a",
        message_id="<c@x>",
        in_reply_to="<b@x>",
        references="<A@X> <b@x>",
        fallback="s3",
    )
    assert first == reply == deep
    assert thread_key(SECRET, "a", message_id="<z@x>", fallback="s4") != first


def test_thread_keys_are_opaque_and_account_scoped() -> None:
    a = thread_key(SECRET, "gmail:a", message_id="<secret-id@corp.example>", fallback="s")
    b = thread_key(SECRET, "gmail:b", message_id="<secret-id@corp.example>", fallback="s")
    assert a != b and "corp" not in a and a.startswith("t:")


def test_no_message_id_uses_the_fallback() -> None:
    assert thread_key(SECRET, "a", message_id=None, fallback="sha-1") != thread_key(
        SECRET, "a", message_id=None, fallback="sha-2"
    )


# -- campaigns ---------------------------------------------------------------------------


_PHISH_BODY = (
    "dear customer we detected unusual sign in activity on your mailbox and as a precaution "
    "your account will be suspended unless you verify your details at <URL_0a1b2c3d4e> within "
    "24 hours failure to complete verification will result in loss of access to your mailbox "
    "stored files and shared folders this is an automated security notice please do not reply "
    "to this message if you have already verified you can ignore this notice thank you for "
    "helping us keep your account safe regards it support team help desk"
).split()


def _phish(i: int, rng: random.Random, swaps: int = 2, words: list[str] | None = None) -> str:
    """One campaign copy: the same body with a couple of per-recipient token edits."""
    body = list(words or _PHISH_BODY)
    for j in rng.sample(range(len(body)), swaps):
        body[j] = f"x{i}"
    return " ".join(body)


def _unique(i: int) -> str:
    return " ".join(f"note{i}w{j}" for j in range(30))


def test_mutated_campaign_copies_cluster_and_unrelated_mail_does_not() -> None:
    rng = random.Random(0)
    texts = {f"p{i:02d}": _phish(i, rng) for i in range(12)}
    texts |= {f"u{i:02d}": _unique(i) for i in range(30)}
    keys = campaign_keys(SECRET, "a", texts)
    assert len({keys[f"p{i:02d}"] for i in range(12)}) == 1
    unique_keys = {keys[f"u{i:02d}"] for i in range(30)}
    assert len(unique_keys) == 30 and keys["p00"] not in unique_keys


def test_campaign_keys_are_deterministic_order_independent_and_scoped() -> None:
    rng = random.Random(1)
    texts = {f"p{i}": _phish(i, rng) for i in range(6)} | {"u": _unique(1)}
    a = campaign_keys(SECRET, "a", texts)
    b = campaign_keys(SECRET, "a", dict(reversed(list(texts.items()))))
    c = campaign_keys(SECRET, "other", texts)
    assert a == b and set(a.values()).isdisjoint(c.values())


def test_known_limit_short_heavily_edited_copies_do_not_cluster() -> None:
    """Documented limit: a 30-word text with 4 edits per copy is not recognised."""
    rng = random.Random(3)
    short = _PHISH_BODY[:30]
    keys = campaign_keys(SECRET, "a", {f"p{i}": _phish(i, rng, 4, short) for i in range(8)})
    assert len(set(keys.values())) > 1


def test_empty_text_is_its_own_campaign() -> None:
    keys = campaign_keys(SECRET, "a", {"e1": "", "e2": ""})
    assert keys["e1"] != keys["e2"]


def test_campaign_crossing_cutoff_is_kept_out_of_both_splits() -> None:
    """End to end: copies of one campaign before and after the cutoff."""
    rng = random.Random(2)
    cutoff = datetime(2026, 7, 1, tzinfo=UTC)
    examples, meta, texts = [], {}, {}
    for i in range(10):
        eid = f"ex-{i:02d}"
        texts[eid] = _phish(i, rng) if i < 4 else _unique(i)
        day = [150, 160, 200, 210][i] if i < 4 else (20 + i * 3 if i < 7 else 220 + i)
        examples.append(
            SelectedExample(
                eid, TrainingLabel.MALICIOUS, f"fb-{i}:0", "c1", SourceKind.SYNTHETIC, texts[eid]
            )
        )
        meta[eid] = (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day), f"<m{i}@x>")
    ckeys = campaign_keys(SECRET, "a", texts)
    metas = {
        eid: ExampleMeta(when, thread_key(SECRET, "a", message_id=mid, fallback=eid), ckeys[eid])
        for eid, (when, mid) in meta.items()
    }
    assigned, dropped = assign_splits(examples, metas, cutoff=cutoff, embargo=timedelta(days=7))
    assert all(f"ex-{i:02d}" in dropped for i in range(4))
    assert {assigned[f"ex-{i:02d}"] for i in range(4, 7)} == {Split.TRAIN}
    assert {assigned[f"ex-{i:02d}"] for i in range(7, 10)} == {Split.EVAL}


# -- key custody -------------------------------------------------------------------------


def test_create_load_and_sign_with_pinned_key(tmp_path: Path) -> None:
    store = FileSigningKeyStore(tmp_path / "keys" / "ed25519.key")
    pub = store.create()
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700
    body = build_manifest(
        "ds-1", SelectionManifest("v", "a", NOW), [], {}, cutoff=NOW, created_at=NOW
    )
    signed = sign_manifest(body, HumanReview("rev-1", NOW), store.load())
    assert signed.body["signing_key_id"] == key_id(pub)
    assert verify_manifest(signed.to_json(), pinned_keys([pub]))["dataset_id"] == "ds-1"
    assert verify_manifest(signed.to_json(), pub)["dataset_id"] == "ds-1"


def test_unpinned_key_is_refused(tmp_path: Path) -> None:
    ours = FileSigningKeyStore(tmp_path / "a" / "k").create()
    other_store = FileSigningKeyStore(tmp_path / "b" / "k")
    other_store.create()
    body = build_manifest(
        "ds-1", SelectionManifest("v", "a", NOW), [], {}, cutoff=NOW, created_at=NOW
    )
    signed = sign_manifest(body, HumanReview("rev-1", NOW), other_store.load())
    with pytest.raises(ManifestError, match="unpinned"):
        verify_manifest(signed.to_json(), pinned_keys([ours]))
    with pytest.raises(ManifestError):
        verify_manifest(signed.to_json(), ours)


def test_tampered_key_id_breaks_signature(tmp_path: Path) -> None:
    store = FileSigningKeyStore(tmp_path / "k" / "key")
    pub = store.create()
    body = build_manifest(
        "ds-1", SelectionManifest("v", "a", NOW), [], {}, cutoff=NOW, created_at=NOW
    )
    doc = json.loads(sign_manifest(body, HumanReview("rev-1", NOW), store.load()).to_json())
    doc["body"]["signing_key_id"] = "0" * 16
    with pytest.raises(ManifestError):
        verify_manifest(json.dumps(doc), pinned_keys([pub]))


def test_create_refuses_to_overwrite(tmp_path: Path) -> None:
    store = FileSigningKeyStore(tmp_path / "k" / "key")
    store.create()
    with pytest.raises(KeyCustodyError, match="overwrite"):
        store.create()


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_load_refuses_loose_file_permissions(tmp_path: Path, mode: int) -> None:
    store = FileSigningKeyStore(tmp_path / "k" / "key")
    store.create()
    os.chmod(store.path, mode)
    with pytest.raises(KeyCustodyError, match="readable"):
        store.load()


def test_load_refuses_loose_directory(tmp_path: Path) -> None:
    store = FileSigningKeyStore(tmp_path / "k" / "key")
    store.create()
    os.chmod(store.path.parent, 0o755)
    with pytest.raises(KeyCustodyError, match="directory"):
        store.load()


def test_load_refuses_symlink_missing_and_malformed(tmp_path: Path) -> None:
    real = FileSigningKeyStore(tmp_path / "k" / "key")
    real.create()
    link = tmp_path / "k" / "link"
    link.symlink_to(real.path)
    with pytest.raises(KeyCustodyError, match="link"):
        FileSigningKeyStore(link).load()
    with pytest.raises(KeyCustodyError, match="no signing key"):
        FileSigningKeyStore(tmp_path / "k" / "absent").load()
    bad = tmp_path / "k" / "bad"
    bad.write_bytes(b"not base64!!")
    os.chmod(bad, 0o600)
    with pytest.raises(KeyCustodyError, match="malformed"):
        FileSigningKeyStore(bad).load()


def test_key_inside_a_git_work_tree_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(KeyCustodyError, match="git"):
        FileSigningKeyStore(repo / "keys" / "ed25519.key").create()
    assert not (repo / "keys" / "ed25519.key").exists()


def test_default_path_is_outside_this_repo() -> None:
    store = FileSigningKeyStore()
    repo = Path(__file__).resolve().parents[2]
    assert repo not in store.path.parents
