"""NerveShield task 5.4: account-scoped personal trust / relationship index.

Synthetic addresses only. Covers allow/deny by account, sparse-history
uncertainty, thread continuity, deletion and the no-raw-content guarantee.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.mail_security.trust_index import (
    AccessDenied,
    Direction,
    HistoryLevel,
    SenderObservation,
    TrustIndex,
    TrustPolicy,
    message_ids,
    normalize_address,
)
from backend.mail_security.trust_index import (
    TrustNote as N,
)

SECRET = b"m" * 32
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
VENDOR = "Billing Team <billing@vendor.example>"
VENDOR_ADDR = "billing@vendor.example"
OTHER_AT_VENDOR = "ceo@vendor.example"
STRANGER = "someone@unknown.example"


def _in(
    sender: str, days_ago: int, mid: str, reply: tuple[str, ...] = (), auth: bool | None = True
):
    return SenderObservation(
        sender, Direction.INBOUND, NOW - timedelta(days=days_ago), mid, reply, auth
    )


def _out(to: str, days_ago: int, mid: str, reply: tuple[str, ...] = ()):
    return SenderObservation(
        "me@example.test",
        Direction.OUTBOUND,
        NOW - timedelta(days=days_ago),
        mid,
        reply,
        counterparts=(to,),
    )


@pytest.fixture
def index() -> TrustIndex:
    idx = TrustIndex(SECRET, allowed_accounts=["gmail:a", "gmail:b"])
    idx.observe("gmail:a", _in(VENDOR, 400, "<t1@vendor.example>"))
    idx.observe("gmail:a", _out(VENDOR_ADDR, 399, "<r1@me>", ("<t1@vendor.example>",)))
    idx.observe("gmail:a", _in(VENDOR, 30, "<t2@vendor.example>", ("<r1@me>",)))
    idx.observe("gmail:a", _in(VENDOR, 2, "<t3@vendor.example>"))
    return idx


# -- history levels and notes ------------------------------------------------------------


def test_established_sender_has_counts_and_never_a_verdict(index: TrustIndex) -> None:
    f = index.features("gmail:a", VENDOR, now=NOW)
    assert f.history is HistoryLevel.ESTABLISHED
    assert (f.inbound_count, f.outbound_count) == (3, 1)
    assert (f.days_since_first_seen, f.days_since_last_seen) == (400, 2)
    assert f.auth_aligned_count == 3
    assert {N.OWNER_HAS_WRITTEN_TO_SENDER, N.AUTH_ALIGNED_HISTORY} <= f.notes
    assert N.FAMILIARITY_IS_NOT_VERIFICATION in f.notes
    assert not {"safe", "verdict", "trusted", "score"} & {k.lower() for k in f.to_codes()}


def test_unknown_sender_has_no_history(index: TrustIndex) -> None:
    f = index.features("gmail:a", STRANGER, now=NOW)
    assert f.history is HistoryLevel.NONE and N.NO_HISTORY in f.notes
    assert f.days_since_first_seen is None


def test_new_sender_at_known_domain_is_flagged(index: TrustIndex) -> None:
    f = index.features("gmail:a", OTHER_AT_VENDOR, now=NOW)
    assert f.history is HistoryLevel.NONE
    assert {N.NO_HISTORY, N.DOMAIN_KNOWN_SENDER_NEW} <= f.notes


@pytest.mark.parametrize("count", [1, 2])
def test_sparse_history_is_marked_uncertain(count: int) -> None:
    idx = TrustIndex(SECRET, allowed_accounts=["a"])
    for i in range(count):
        idx.observe("a", _in(STRANGER, 10 + i, f"<s{i}@x>"))
    f = idx.features("a", STRANGER, now=NOW)
    assert f.history is HistoryLevel.SPARSE and N.SPARSE_HISTORY in f.notes
    assert N.SENDER_NEVER_ADDRESSED_BY_OWNER in f.notes


def test_sparse_threshold_is_configurable() -> None:
    idx = TrustIndex(SECRET, allowed_accounts=["a"], policy=TrustPolicy(sparse_below=1))
    idx.observe("a", _in(STRANGER, 1, "<x@y>"))
    assert idx.features("a", STRANGER, now=NOW).history is HistoryLevel.ESTABLISHED


def test_dormant_sender_and_unaligned_auth_history() -> None:
    idx = TrustIndex(SECRET, allowed_accounts=["a"])
    for i in range(3):
        idx.observe("a", _in(STRANGER, 500 + i, f"<d{i}@x>", auth=False))
    f = idx.features("a", STRANGER, now=NOW)
    assert {N.DORMANT_SENDER, N.AUTH_NOT_ALIGNED_BEFORE} <= f.notes
    assert N.AUTH_ALIGNED_HISTORY not in f.notes


# -- thread continuity -------------------------------------------------------------------


def test_reply_to_known_thread_by_a_participant(index: TrustIndex) -> None:
    f = index.features("gmail:a", VENDOR, now=NOW, in_reply_to=("<r1@me>",))
    assert f.reply_to_known_thread is True and N.REPLY_TO_KNOWN_THREAD in f.notes
    assert N.THREAD_KNOWN_SENDER_NEW_TO_THREAD not in f.notes


def test_thread_hijack_shape_known_thread_new_sender(index: TrustIndex) -> None:
    f = index.features(
        "gmail:a", "billing@vendor-payments.example", now=NOW, in_reply_to=("<t2@vendor.example>",)
    )
    assert f.history is HistoryLevel.NONE
    assert {N.REPLY_TO_KNOWN_THREAD, N.THREAD_KNOWN_SENDER_NEW_TO_THREAD} <= f.notes


def test_reply_to_unknown_thread(index: TrustIndex) -> None:
    f = index.features("gmail:a", VENDOR, now=NOW, in_reply_to=("<never@seen>",))
    assert f.reply_to_known_thread is False and N.REPLY_TO_UNKNOWN_THREAD in f.notes


def test_not_a_reply_leaves_thread_unknown(index: TrustIndex) -> None:
    assert index.features("gmail:a", VENDOR, now=NOW).reply_to_known_thread is None


# -- account isolation -------------------------------------------------------------------


def test_history_in_one_account_is_invisible_in_another(index: TrustIndex) -> None:
    f = index.features("gmail:b", VENDOR, now=NOW)
    assert f.history is HistoryLevel.NONE and N.DOMAIN_KNOWN_SENDER_NEW not in f.notes
    assert (
        index.features(
            "gmail:b", VENDOR, now=NOW, in_reply_to=("<t1@vendor.example>",)
        ).reply_to_known_thread
        is False
    )


def test_same_address_gives_unrelated_digests_per_account(index: TrustIndex) -> None:
    index.observe("gmail:b", _in(VENDOR, 1, "<t1@vendor.example>"))
    state = index.export_state()["accounts"]
    assert not set(state["gmail:a"]["senders"]) & set(state["gmail:b"]["senders"])
    threads_a = {t for s in state["gmail:a"]["senders"].values() for t in s["threads"]}
    threads_b = {t for s in state["gmail:b"]["senders"].values() for t in s["threads"]}
    assert not threads_a & threads_b


@pytest.mark.parametrize("op", ["observe", "features", "forget"])
def test_unallowed_account_is_denied_for_every_operation(index: TrustIndex, op: str) -> None:
    with pytest.raises(AccessDenied):
        if op == "observe":
            index.observe("gmail:c", _in(VENDOR, 1, "<x@y>"))
        elif op == "features":
            index.features("gmail:c", VENDOR, now=NOW)
        else:
            index.forget_sender("gmail:c", VENDOR)


def test_revoke_denies_and_deletes(index: TrustIndex) -> None:
    index.revoke("gmail:a")
    with pytest.raises(AccessDenied):
        index.features("gmail:a", VENDOR, now=NOW)
    with pytest.raises(AccessDenied):
        index.observe("gmail:a", _in(VENDOR, 1, "<x@y>"))
    with pytest.raises(AccessDenied):
        index.allow("gmail:a")
    assert "gmail:a" not in index.export_state()["accounts"]


def test_revoke_without_delete_keeps_data_but_blocks_access(index: TrustIndex) -> None:
    index.revoke("gmail:a", delete=False)
    assert "gmail:a" in index.export_state()["accounts"]
    with pytest.raises(AccessDenied):
        index.features("gmail:a", VENDOR, now=NOW)


# -- deletion ----------------------------------------------------------------------------


def test_forget_sender_removes_history_threads_and_domain(index: TrustIndex) -> None:
    assert index.forget_sender("gmail:a", VENDOR_ADDR) is True
    f = index.features("gmail:a", VENDOR, now=NOW, in_reply_to=("<t1@vendor.example>",))
    assert f.history is HistoryLevel.NONE and f.reply_to_known_thread is False
    assert (
        N.DOMAIN_KNOWN_SENDER_NEW not in index.features("gmail:a", OTHER_AT_VENDOR, now=NOW).notes
    )
    assert index.export_state()["accounts"]["gmail:a"]["senders"] == {}
    assert index.forget_sender("gmail:a", VENDOR_ADDR) is False


def test_delete_account_removes_everything_but_keeps_access(index: TrustIndex) -> None:
    index.delete_account("gmail:a")
    assert index.features("gmail:a", VENDOR, now=NOW).history is HistoryLevel.NONE
    assert "gmail:a" not in index.export_state()["accounts"]


# -- persistence and privacy -------------------------------------------------------------


def test_export_contains_no_raw_identifiers(index: TrustIndex) -> None:
    text = json.dumps(index.export_state())
    for needle in ("billing", "vendor.example", "Billing Team", "t1@", "r1@me", "me@example"):
        assert needle not in text


def test_round_trip_and_revocation_survive_restart(index: TrustIndex) -> None:
    index.revoke("gmail:b")
    state = json.loads(json.dumps(index.export_state()))
    fresh = TrustIndex(SECRET, allowed_accounts=["gmail:a"])
    fresh.import_state(state)
    before = index.features("gmail:a", VENDOR, now=NOW, in_reply_to=("<r1@me>",))
    after = fresh.features("gmail:a", VENDOR, now=NOW, in_reply_to=("<r1@me>",))
    assert before == after
    with pytest.raises(AccessDenied):
        fresh.allow("gmail:b")


def test_wrong_secret_cannot_read_history(index: TrustIndex) -> None:
    other = TrustIndex(b"x" * 32, allowed_accounts=["gmail:a"])
    other.import_state(index.export_state())
    assert other.features("gmail:a", VENDOR, now=NOW).history is HistoryLevel.NONE


def test_bad_state_and_short_secret_rejected() -> None:
    with pytest.raises(ValueError):
        TrustIndex(b"short")
    with pytest.raises(ValueError):
        TrustIndex(SECRET).import_state({"version": 99})


# -- input handling ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Billing <BILLING@Vendor.Example>", "billing@vendor.example"),
        ("billing@vendor.example", "billing@vendor.example"),
        ("a@x, b@y", None),
        ("not an address", None),
        ("", None),
        ("x" * 3000 + "@y", None),
    ],
)
def test_normalize_address(raw: str, expected: str | None) -> None:
    assert normalize_address(raw) == expected


def test_invalid_sender_and_naive_time_store_nothing() -> None:
    idx = TrustIndex(SECRET, allowed_accounts=["a"])
    assert idx.observe("a", _in("garbage", 1, "<x@y>")) is False
    naive = SenderObservation(STRANGER, Direction.INBOUND, datetime(2026, 1, 1), "<x@y>")
    assert idx.observe("a", naive) is False
    assert idx.export_state()["accounts"] == {}
    f = idx.features("a", "garbage", now=NOW)
    assert N.INVALID_SENDER in f.notes and f.history is HistoryLevel.NONE


def test_message_ids_parse_references_header() -> None:
    assert message_ids("<A@x> <b@y>\n <a@x>") == ("a@x", "b@y")
    assert message_ids(None) == ()


def test_not_wired_into_the_backend() -> None:
    backend = Path(__file__).resolve().parents[2] / "backend"
    offenders = [
        str(p.relative_to(backend))
        for p in backend.rglob("*.py")
        if p.name != "trust_index.py" and "trust_index" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_does_not_touch_the_general_knowledge_graph() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "backend/mail_security/trust_index.py"
    ).read_text()
    assert "import backend.knowledge" not in source and "from backend.knowledge" not in source
    assert "backend.persistence" not in source
