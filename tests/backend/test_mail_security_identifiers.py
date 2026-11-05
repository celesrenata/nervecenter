"""NerveShield Phase 1 Task 1.2 — pure deterministic identifier tests.

Tests-first coverage for ``backend.mail_security.identifiers``: the pure
deterministic functions that derive account-scoped logical source keys, stable
analysis-version keys, outbox job keys and assessment ids.

Requirements exercised:

- **R-018** (idempotent delivery / stable logical identity; I-11 no duplicate
  effects): identical inputs for a fixed version yield identical ids; a replay
  resolves to the same outbox job key.
- **R-023 / T-15** (multi-account isolation): the same RFC ``Message-ID`` (and
  the same derived ``message_key``) under two different account scopes yields
  *distinct* logical source keys *and* distinct assessment ids. T-15 "account A
  cannot read account B verdict" is upheld at the identity layer: ids never
  collide across accounts.

Property style — NOTE: ``hypothesis`` is **not** a repository dependency
(absent from ``pyproject.toml`` and not importable in the environment), and the
task constraints forbid adding a new dependency for this task. So, as the task
permits, these are **deterministic table-driven / parametrized** tests. Where a
"property" needs many inputs, we generate a reproducible corpus with a
fixed-seed ``random.Random`` *inside the test module only* (never in
production, which stays clock-/randomness-free) so every run exercises the same
inputs and failures reproduce exactly.
"""

from __future__ import annotations

import random
import re
import string

import pytest

from backend.mail_security import (
    HASH_ALGORITHM,
    KEY_SCHEME_VERSION,
    assessment_id,
    logical_source_key,
    normalize_analysis_version,
    outbox_job_key,
)
from backend.mail_security.identifiers import _ACCOUNT_SCOPE_RE  # noqa: PLC2701
from backend.mail_security.models import (
    MessageEvidenceManifest,
    OpaqueRef,
    SecurityAssessment,
)

# Contract patterns the derived ids must satisfy so they are usable directly as
# model field values (mirrors models.py). Kept local to avoid importing private
# regexes from the models module.
_OPAQUE_REF_RE = re.compile(r"^[A-Za-z0-9:_-]{1,200}$")
_ASSESSMENT_ID_RE = re.compile(r"^[A-Za-z0-9:_-]{1,160}$")
_ACCOUNT_SCOPE_FIELD_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")


# -- reproducible corpus -----------------------------------------------------

_SCOPE_ALPHABET = string.ascii_letters + string.digits + ":_-"
# Message-IDs / UIDs are attacker-influenced free text: include awkward chars.
_FREE_ALPHABET = string.ascii_letters + string.digits + "@.<>-_+=/ \t\x1f:"


def _rand_scope(rng: random.Random) -> str:
    n = rng.randint(1, 24)
    first = rng.choice(string.ascii_letters + string.digits)
    rest = "".join(rng.choice(_SCOPE_ALPHABET) for _ in range(n - 1))
    return first + rest


def _rand_free(rng: random.Random, *, min_len: int = 1, max_len: int = 40) -> str:
    n = rng.randint(min_len, max_len)
    return "".join(rng.choice(_FREE_ALPHABET) for _ in range(n))


def _corpus(seed: int, count: int) -> list[dict[str, object]]:
    """Build a reproducible corpus of identity inputs."""
    rng = random.Random(seed)
    rows: list[dict[str, object]] = []
    for _ in range(count):
        rows.append(
            {
                "account_scope": _rand_scope(rng),
                "provider": rng.choice(["gmail", "imap", "eml", "takeout"]),
                "provider_uid": _rand_free(rng) if rng.random() < 0.7 else "",
                "rfc_message_id": _rand_free(rng),
                "folder": rng.choice(["INBOX", "Spam", "Archive", "Label/Work", ""]),
                "source_revision": rng.randint(1, 9),
                "analysis_version": rng.choice(
                    ["1.0", "1.1", "v2", "p1.0.0", "shadow-1"]
                ),
            }
        )
    return rows


_CORPUS = _corpus(seed=20240601, count=400)


def _key_for(row: dict[str, object]) -> str:
    return logical_source_key(
        str(row["account_scope"]),
        provider=str(row["provider"]),
        provider_uid=str(row["provider_uid"]) or None,
        rfc_message_id=str(row["rfc_message_id"]),
        folder=str(row["folder"]) or None,
    )


# -- shape / contract-compatibility ------------------------------------------


def test_hash_algorithm_is_documented_sha256() -> None:
    assert HASH_ALGORITHM == "sha256"


@pytest.mark.parametrize("row", _CORPUS[:120])
def test_logical_source_key_matches_opaque_ref_contract(row: dict[str, object]) -> None:
    key = _key_for(row)
    assert _OPAQUE_REF_RE.match(key), key
    # usable directly as an OpaqueRef-typed model field
    manifest = MessageEvidenceManifest(
        source_id=key,
        account_scope=str(row["account_scope"]),
        provider=str(row["provider"]),
        provider_uid="uid-placeholder",
        acquisition_mode="EML",
        acquired_at="2024-01-01T00:00:00Z",
        evidence_fidelity="UNKNOWN",
    )
    assert manifest.source_id == key


@pytest.mark.parametrize("row", _CORPUS[:120])
def test_assessment_id_matches_contract_and_is_usable(row: dict[str, object]) -> None:
    key = _key_for(row)
    aid = assessment_id(
        str(row["account_scope"]),
        key,
        int(row["source_revision"]),
        str(row["analysis_version"]),
    )
    assert _ASSESSMENT_ID_RE.match(aid), aid
    assert _ACCOUNT_SCOPE_FIELD_RE.match(str(row["account_scope"]))


# -- (1) DEDUP / idempotency (R-018, I-11) -----------------------------------


@pytest.mark.parametrize("row", _CORPUS)
def test_logical_source_key_is_deterministic(row: dict[str, object]) -> None:
    assert _key_for(row) == _key_for(row)


@pytest.mark.parametrize("row", _CORPUS)
def test_assessment_id_is_deterministic(row: dict[str, object]) -> None:
    key = _key_for(row)
    args = (
        str(row["account_scope"]),
        key,
        int(row["source_revision"]),
        str(row["analysis_version"]),
    )
    assert assessment_id(*args) == assessment_id(*args)


@pytest.mark.parametrize("row", _CORPUS)
def test_outbox_job_key_is_stable_across_replay(row: dict[str, object]) -> None:
    key = _key_for(row)
    scope = str(row["account_scope"])
    version = str(row["analysis_version"])
    # A replay feeds identical inputs; the job key must not change (T-16).
    assert outbox_job_key(scope, key, version) == outbox_job_key(scope, key, version)


# -- (2) VERSION CHANGE (design §9, R-018.5) ---------------------------------


@pytest.mark.parametrize("row", _CORPUS)
def test_new_revision_changes_assessment_id(row: dict[str, object]) -> None:
    key = _key_for(row)
    scope = str(row["account_scope"])
    version = str(row["analysis_version"])
    rev = int(row["source_revision"])
    assert assessment_id(scope, key, rev, version) != assessment_id(
        scope, key, rev + 1, version
    )


@pytest.mark.parametrize("row", _CORPUS)
def test_new_analysis_version_changes_assessment_id(row: dict[str, object]) -> None:
    key = _key_for(row)
    scope = str(row["account_scope"])
    rev = int(row["source_revision"])
    base = str(row["analysis_version"])
    other = "9.9" if base != "9.9" else "8.8"
    assert assessment_id(scope, key, rev, base) != assessment_id(
        scope, key, rev, other
    )


def test_analysis_version_change_also_changes_outbox_job_key() -> None:
    scope = "acct-A"
    key = logical_source_key(scope, provider="gmail", provider_uid="uid-1")
    assert outbox_job_key(scope, key, "1.0") != outbox_job_key(scope, key, "1.1")


# -- (3) CROSS-ACCOUNT ISOLATION (R-023 / T-15) ------------------------------


@pytest.mark.parametrize("row", _CORPUS)
def test_same_message_id_different_account_distinct_source_key(
    row: dict[str, object],
) -> None:
    """Same provider + Message-ID + folder, two accounts => distinct keys."""
    scope_a = "tenant-A"
    scope_b = "tenant-B"
    provider = str(row["provider"])
    mid = str(row["rfc_message_id"]) or "<fallback@example>"
    folder = str(row["folder"]) or None
    key_a = logical_source_key(
        scope_a, provider=provider, rfc_message_id=mid, folder=folder
    )
    key_b = logical_source_key(
        scope_b, provider=provider, rfc_message_id=mid, folder=folder
    )
    assert key_a != key_b
    # scope appears structurally, so each key is attributable to its account
    assert f":{scope_a}:" in key_a
    assert f":{scope_b}:" in key_b


@pytest.mark.parametrize("row", _CORPUS)
def test_same_message_different_account_distinct_assessment_id(
    row: dict[str, object],
) -> None:
    scope_a = "tenant-A"
    scope_b = "tenant-B"
    provider = str(row["provider"])
    mid = str(row["rfc_message_id"]) or "<fallback@example>"
    rev = int(row["source_revision"])
    version = str(row["analysis_version"])
    key_a = logical_source_key(scope_a, provider=provider, rfc_message_id=mid)
    key_b = logical_source_key(scope_b, provider=provider, rfc_message_id=mid)
    aid_a = assessment_id(scope_a, key_a, rev, version)
    aid_b = assessment_id(scope_b, key_b, rev, version)
    assert aid_a != aid_b


def test_outbox_job_key_isolated_by_account() -> None:
    key = logical_source_key("tenant-A", provider="gmail", provider_uid="uid-1")
    key_b = logical_source_key("tenant-B", provider="gmail", provider_uid="uid-1")
    assert outbox_job_key("tenant-A", key, "1.0") != outbox_job_key(
        "tenant-B", key_b, "1.0"
    )


# -- (4) collision / fallback / edge handling --------------------------------


def test_provider_uid_takes_precedence_over_message_id() -> None:
    """When a provider UID is present it is authoritative (design §9)."""
    with_uid = logical_source_key(
        "acct",
        provider="gmail",
        provider_uid="UID-123",
        rfc_message_id="<attacker@evil>",
    )
    only_uid = logical_source_key("acct", provider="gmail", provider_uid="UID-123")
    assert with_uid == only_uid  # Message-ID is ignored when a UID exists


def test_empty_provider_uid_falls_back_to_message_id() -> None:
    """Empty/missing UID falls back to RFC Message-ID semantics."""
    for empty in ("", "   ", None):
        key = logical_source_key(
            "acct",
            provider="gmail",
            provider_uid=empty,  # type: ignore[arg-type]
            rfc_message_id="<real@example.com>",
        )
        assert ":rfcmid:" in key


def test_provider_uid_and_message_id_keys_never_collide() -> None:
    """A UID identity and a Message-ID identity with the same raw string and
    same account must produce different keys (discriminator tag, design §9)."""
    same = "duplicate-token"
    uid_key = logical_source_key("acct", provider="gmail", provider_uid=same)
    mid_key = logical_source_key("acct", provider="gmail", rfc_message_id=same)
    assert uid_key != mid_key
    assert ":puid:" in uid_key
    assert ":rfcmid:" in mid_key


def test_folder_semantics_distinguish_same_message() -> None:
    """Same message filed under different folders => distinct keys."""
    inbox = logical_source_key(
        "acct", provider="gmail", rfc_message_id="<m@x>", folder="INBOX"
    )
    spam = logical_source_key(
        "acct", provider="gmail", rfc_message_id="<m@x>", folder="Spam"
    )
    assert inbox != spam


def test_missing_both_uid_and_message_id_is_rejected() -> None:
    """A source with no stable identity material must not collapse to a
    scope-only key that would merge unrelated messages."""
    with pytest.raises(ValueError):
        logical_source_key("acct", provider="gmail")
    with pytest.raises(ValueError):
        logical_source_key("acct", provider="gmail", provider_uid="", rfc_message_id="")


def test_raw_message_id_is_not_used_alone_as_authority() -> None:
    """The raw Message-ID must never be the whole key: the scheme, account
    scope and kind tag are all present, so the attacker-controlled Message-ID
    is never database authority by itself (design §9)."""
    mid = "<attacker-controlled@evil.example>"
    key = logical_source_key("acct-1", provider="gmail", rfc_message_id=mid)
    assert mid not in key  # raw value never embedded
    assert key.startswith(f"{KEY_SCHEME_VERSION}:acct-1:rfcmid:")


# -- input validation --------------------------------------------------------


@pytest.mark.parametrize(
    "bad_scope",
    ["", "has space", "ünïcode", "a" * 129, "has/slash?", "tab\tscope"],
)
def test_invalid_account_scope_is_rejected(bad_scope: str) -> None:
    with pytest.raises(ValueError):
        logical_source_key(bad_scope, provider="gmail", provider_uid="uid-1")
    with pytest.raises(ValueError):
        assessment_id(bad_scope, "k1:acct:puid:abcd", 1, "1.0")
    with pytest.raises(ValueError):
        outbox_job_key(bad_scope, "k1:acct:puid:abcd", "1.0")


@pytest.mark.parametrize("bad_rev", [0, -1, -100])
def test_invalid_source_revision_is_rejected(bad_rev: int) -> None:
    with pytest.raises(ValueError):
        assessment_id("acct", "msgkey", bad_rev, "1.0")


@pytest.mark.parametrize("bad_rev", [True, False, 1.5, "1", None])
def test_non_int_source_revision_is_rejected(bad_rev: object) -> None:
    with pytest.raises(ValueError):
        assessment_id("acct", "msgkey", bad_rev, "1.0")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_version",
    ["", "_leading", ".dot", "has space", "a" * 101, "slash/version"],
)
def test_invalid_analysis_version_is_rejected(bad_version: str) -> None:
    with pytest.raises(ValueError):
        normalize_analysis_version(bad_version)
    with pytest.raises(ValueError):
        assessment_id("acct", "msgkey", 1, bad_version)
    with pytest.raises(ValueError):
        outbox_job_key("acct", "msgkey", bad_version)


def test_empty_message_key_is_rejected() -> None:
    with pytest.raises(ValueError):
        assessment_id("acct", "", 1, "1.0")
    with pytest.raises(ValueError):
        outbox_job_key("acct", "", "1.0")


def test_empty_provider_is_rejected() -> None:
    with pytest.raises(ValueError):
        logical_source_key("acct", provider="", provider_uid="uid-1")


def test_normalize_analysis_version_round_trips_valid_tokens() -> None:
    for version in ["1.0", "v2", "p1.0.0", "shadow-1", "A", "0"]:
        assert normalize_analysis_version(version) == version


# -- framing / injection resistance ------------------------------------------


def test_field_framing_resists_boundary_forgery() -> None:
    """Concatenation ambiguity must not let two different (uid, folder) pairs
    collide. Length-prefixed framing makes the preimage injective."""
    a = logical_source_key("acct", provider="gmail", provider_uid="ab", folder="c")
    b = logical_source_key("acct", provider="gmail", provider_uid="a", folder="bc")
    assert a != b


def test_derived_ids_satisfy_assessment_contract_patterns() -> None:
    """The derived ids satisfy the Task 1.1 ``SecurityAssessment`` field
    patterns (``assessment_id`` / ``account_scope`` / ``source_ref``), so they
    can be persisted directly without reshaping."""
    scope = "tenant-A"
    key: OpaqueRef = logical_source_key(
        scope, provider="gmail", provider_uid="UID-99", folder="INBOX"
    )
    aid = assessment_id(scope, key, 1, "1.0")
    assert _ASSESSMENT_ID_RE.match(aid)
    assert _ACCOUNT_SCOPE_FIELD_RE.match(scope)
    assert _OPAQUE_REF_RE.match(key)
    # SecurityAssessment is the intended consumer of these ids.
    assert "assessment_id" in SecurityAssessment.model_fields
    assert _ACCOUNT_SCOPE_RE is not None
