"""NerveShield task 5.3: weak Gmail label normalisation. Synthetic label strings only."""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

import pytest

from backend.mail_security.takeout_import import (
    ImportPolicy,
    ImportRecord,
    ImportRequest,
    ImportSource,
    SourceKind,
    run_import,
)
from backend.mail_security.takeout_labels import (
    Category,
    LabelSource,
    LabelStrength,
    WeakLabels,
    normalize_labels,
    summarize,
)
from backend.mail_security.takeout_labels import (
    LabelAmbiguity as A,
)
from backend.mail_security.takeout_labels import (
    SystemLabel as S,
)

SALT = b"0123456789abcdef-test-salt"
PRIVATE = "Divorce lawyer"


@pytest.mark.parametrize(
    ("raw", "system", "categories", "custom"),
    [
        ("Inbox,Important,Opened", {S.INBOX, S.IMPORTANT, S.OPENED}, set(), 0),
        ("Spam", {S.SPAM}, set(), 0),
        ("Trash,Opened", {S.TRASH, S.OPENED}, set(), 0),
        ("Sent,Starred", {S.SENT, S.STARRED}, set(), 0),
        ("Drafts", {S.DRAFT}, set(), 0),
        ("Archived,Unread,Chat", {S.ARCHIVED, S.UNREAD, S.CHAT}, set(), 0),
        ("INBOX, important ,  opened", {S.INBOX, S.IMPORTANT, S.OPENED}, set(), 0),
        ("Inbox,Category Promotions", {S.INBOX}, {Category.PROMOTIONS}, 0),
        ("Category Updates,Category_Social", set(), {Category.UPDATES, Category.SOCIAL}, 0),
        ("Inbox,Work/Projects,Receipts", {S.INBOX}, set(), 2),
        ('Inbox,"Bills, 2026",Family', {S.INBOX}, set(), 2),
    ],
)
def test_known_shapes(raw: str, system: set, categories: set, custom: int) -> None:
    labels = normalize_labels(raw)
    assert labels.system == system
    assert labels.categories == categories
    assert labels.custom_count == custom
    assert labels.source is LabelSource.PROVIDER_EXPORT
    assert labels.strength is LabelStrength.WEAK


def test_quoted_label_with_comma_and_escaped_quote() -> None:
    labels = normalize_labels('"Bills, 2026","Say ""hi""",Inbox')
    assert labels.custom_names == ("Bills, 2026", 'Say "hi"')
    assert labels.ambiguities == frozenset()


@pytest.mark.parametrize(
    ("raw", "flag"),
    [
        ("Spam,Inbox", A.SPAM_AND_INBOX),
        ("Trash,Inbox", A.TRASH_AND_INBOX),
        ("Spam,Sent", A.SPAM_AND_SENT),
        ("Draft,Sent", A.DRAFT_AND_SENT),
        ("Category Social,Category Forums", A.MULTIPLE_CATEGORIES),
        ('Inbox,"unterminated', A.UNBALANCED_QUOTES),
        ("Inbox,=?UTF-8?B?w6TDtsO8?=", A.ENCODED_WORD),
        ("Inbox,,Opened", A.EMPTY_LABEL),
        ("Category Newsletters", A.UNKNOWN_CATEGORY),
        ("Posteingang,Wichtig", A.POSSIBLE_LOCALIZED_SYSTEM_LABEL),
        ("Inbox,bad\u202elabel", A.CONTROL_CHARACTERS),
        ("x" * 300, A.LABEL_TOO_LONG),
        (",".join(f"L{i}" for i in range(200)), A.TOO_MANY_LABELS),
        ("Inbox," + "y" * 5000, A.RAW_TRUNCATED),
        ("", A.NO_LABELS),
        ("  ,  ", A.NO_LABELS),
    ],
)
def test_ambiguity_is_flagged_not_resolved(raw: str, flag: A) -> None:
    labels = normalize_labels(raw)
    assert flag in labels.ambiguities and labels.ambiguous


def test_contradiction_keeps_both_labels() -> None:
    labels = normalize_labels("Spam,Inbox")
    assert labels.system == {S.SPAM, S.INBOX}


def test_none_means_no_labels() -> None:
    assert normalize_labels(None).ambiguities == {A.NO_LABELS}


def test_localized_names_are_never_mapped_to_system_labels() -> None:
    labels = normalize_labels("Posteingang,Spam-Verdacht,迷惑メール")
    assert labels.system == frozenset()
    assert labels.custom_count == 3


def test_lookalike_names_are_custom() -> None:
    labels = normalize_labels("Inbox2,My Spam Folder,Spammy")
    assert labels.system == frozenset() and labels.custom_count == 3


def test_custom_names_hidden_and_digests_salted() -> None:
    labels = normalize_labels(f"Inbox,{PRIVATE}")
    assert PRIVATE not in repr(labels)
    assert PRIVATE not in json.dumps(labels.to_codes())
    digests = labels.custom_digests(SALT)
    assert len(digests) == 1 and re.fullmatch(r"[0-9a-f]{64}", digests[0])
    assert digests != normalize_labels(PRIVATE).custom_digests(b"another-salt-16bytes")
    assert digests == normalize_labels(PRIVATE.upper()).custom_digests(SALT)
    with pytest.raises(ValueError):
        labels.custom_digests(b"short")


def test_no_verdict_shaped_fields() -> None:
    fields = set(WeakLabels.__dataclass_fields__)
    banned = {"is_spam", "verdict", "disposition", "score", "label", "truth", "malicious"}
    assert not fields & banned
    source = Path(__file__).resolve().parents[2] / "backend/mail_security/takeout_labels.py"
    text = source.read_text(encoding="utf-8")
    assert "Disposition" not in text and "is_spam" not in text


@pytest.mark.parametrize("seed", range(40))
def test_random_input_never_raises(seed: int) -> None:
    rng = random.Random(seed)
    alphabet = 'abc ,"=?\\\u202e\x00Inbox Spam Category/'
    raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 600)))
    labels = normalize_labels(raw)
    assert labels.custom_count <= 128
    json.dumps(labels.to_codes())


def test_summarize_counts_per_account_without_names() -> None:
    summary = summarize(
        [
            ("gmail:a", "Inbox,Important"),
            ("gmail:a", "Spam,Inbox"),
            ("gmail:a", None),
            ("gmail:b", f"Category Promotions,{PRIVATE}"),
        ]
    )
    a, b = summary["gmail:a"].to_dict(), summary["gmail:b"].to_dict()
    assert a["messages"] == 3 and a["system"] == {"IMPORTANT": 1, "INBOX": 2, "SPAM": 1}
    assert a["ambiguities"] == {"NO_LABELS": 1, "SPAM_AND_INBOX": 1}
    assert b["categories"] == {"PROMOTIONS": 1} and b["with_custom"] == 1
    assert a["strength"] == "WEAK" and a["source"] == "PROVIDER_EXPORT"
    assert PRIVATE not in json.dumps({k: v.to_dict() for k, v in summary.items()})


def test_end_to_end_with_importer_records(tmp_path: Path) -> None:
    def eml(n: int, labels: str | None) -> bytes:
        head = [f"From: s{n}@example.test", f"Message-ID: <l{n}@example.test>", "Subject: x"]
        if labels is not None:
            head.append(f"X-Gmail-Labels: {labels}")
        return ("\n".join([*head, "", "body", ""])).encode()

    mbox = b"".join(
        f"From {i}@x Mon Nov 02 10:00:00 +0000 2026\n".encode() + m + b"\n"
        for i, m in enumerate(
            [eml(1, "Inbox,Important"), eml(2, '"Bills, 2026",Spam,Inbox'), eml(3, None)]
        )
    )
    path = tmp_path / "a.mbox"
    path.write_bytes(mbox)
    got: list[ImportRecord] = []
    run_import(
        ImportRequest(
            frozenset({"acct"}), (ImportSource("acct", path, SourceKind.MBOX),), dry_run=False
        ),
        ImportPolicy(allowed_roots=(tmp_path,)),
        sink=got.append,
    )
    labels = [normalize_labels(r.gmail_labels_raw) for r in got]
    assert labels[0].system == {S.INBOX, S.IMPORTANT}
    assert labels[1].custom_names == ("Bills, 2026",) and A.SPAM_AND_INBOX in labels[1].ambiguities
    assert labels[2].ambiguities == {A.NO_LABELS}


def test_not_wired_into_the_backend() -> None:
    backend = Path(__file__).resolve().parents[2] / "backend"
    offenders = [
        str(p.relative_to(backend))
        for p in backend.rglob("*.py")
        if p.name != "takeout_labels.py" and "takeout_labels" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
