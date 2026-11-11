"""Weak normalisation of Gmail Takeout labels (NerveShield task 5.3).

Library only. Turns the raw ``X-Gmail-Labels`` header value carried on an
:class:`~backend.mail_security.takeout_import.ImportRecord` into a typed
:class:`WeakLabels` value (R-014.3, R-016, design section 11).

Rules:

* **Weak, never ground truth.** Every label is a provider label at export
  time (``source=PROVIDER_EXPORT``, ``strength=WEAK``). This module produces no
  verdict, disposition, score or "is spam" boolean, and nothing here may be
  used as a training label without the separate review of Phase 6.
* **Ambiguity is explicit.** Contradictory system labels (Spam with Inbox,
  Trash with Inbox, Spam with Sent, Draft with Sent), malformed quoting,
  truncated or encoded headers, unknown system-looking tokens and possible
  localised system names are reported as :class:`LabelAmbiguity` codes, never
  silently resolved.
* **Privacy.** Custom label names can be personal ("Divorce lawyer"). They are
  kept only in process (``repr`` hides them) and exposed for storage as salted
  SHA-256 digests; :func:`summarize` returns counts and enum codes only.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

#: Raw values longer than this were truncated upstream (``MAX_LABELS_CHARS``).
MAX_RAW_CHARS = 4096
MAX_LABELS = 128
MAX_LABEL_CHARS = 225  # Gmail's own label-name limit


class LabelSource(StrEnum):
    PROVIDER_EXPORT = "PROVIDER_EXPORT"


class LabelStrength(StrEnum):
    WEAK = "WEAK"


class SystemLabel(StrEnum):
    INBOX = "INBOX"
    SPAM = "SPAM"
    TRASH = "TRASH"
    SENT = "SENT"
    DRAFT = "DRAFT"
    IMPORTANT = "IMPORTANT"
    STARRED = "STARRED"
    UNREAD = "UNREAD"
    OPENED = "OPENED"
    ARCHIVED = "ARCHIVED"
    CHAT = "CHAT"


class Category(StrEnum):
    PERSONAL = "PERSONAL"
    SOCIAL = "SOCIAL"
    PROMOTIONS = "PROMOTIONS"
    UPDATES = "UPDATES"
    FORUMS = "FORUMS"
    # Newer Gmail categories, seen in a real 2026 Takeout export (NS-ADR-043).
    PURCHASES = "PURCHASES"
    BILLS = "BILLS"
    TRAVEL = "TRAVEL"


class LabelAmbiguity(StrEnum):
    NO_LABELS = "NO_LABELS"
    SPAM_AND_INBOX = "SPAM_AND_INBOX"
    TRASH_AND_INBOX = "TRASH_AND_INBOX"
    SPAM_AND_SENT = "SPAM_AND_SENT"
    DRAFT_AND_SENT = "DRAFT_AND_SENT"
    MULTIPLE_CATEGORIES = "MULTIPLE_CATEGORIES"
    UNBALANCED_QUOTES = "UNBALANCED_QUOTES"
    RAW_TRUNCATED = "RAW_TRUNCATED"
    ENCODED_WORD = "ENCODED_WORD"  # RFC 2047 text left undecoded on purpose
    TOO_MANY_LABELS = "TOO_MANY_LABELS"
    LABEL_TOO_LONG = "LABEL_TOO_LONG"
    EMPTY_LABEL = "EMPTY_LABEL"
    UNKNOWN_CATEGORY = "UNKNOWN_CATEGORY"
    POSSIBLE_LOCALIZED_SYSTEM_LABEL = "POSSIBLE_LOCALIZED_SYSTEM_LABEL"
    CONTROL_CHARACTERS = "CONTROL_CHARACTERS"


# English names as written by Takeout. Matching is case-insensitive and exact;
# anything else is a custom label (with an ambiguity flag where suspicious).
_SYSTEM_NAMES: dict[str, SystemLabel] = {
    "inbox": SystemLabel.INBOX,
    "spam": SystemLabel.SPAM,
    "trash": SystemLabel.TRASH,
    "sent": SystemLabel.SENT,
    "draft": SystemLabel.DRAFT,
    "drafts": SystemLabel.DRAFT,
    "important": SystemLabel.IMPORTANT,
    "starred": SystemLabel.STARRED,
    "unread": SystemLabel.UNREAD,
    "opened": SystemLabel.OPENED,
    "archived": SystemLabel.ARCHIVED,
    "chat": SystemLabel.CHAT,
}
_CATEGORY_RE = re.compile(r"^category[ _:/-]?(.*)$", re.IGNORECASE)
_CATEGORY_NAMES = {c.value.lower(): c for c in Category}
#: Common non-English Gmail system names. A match is NOT mapped to a system
#: label (the export locale is unknown); it is flagged so a human can decide.
_LOCALIZED_HINTS = frozenset(
    {
        "posteingang",
        "boîte de réception",
        "boite de reception",
        "recibidos",
        "bandeja de entrada",
        "posta in arrivo",
        "caixa de entrada",
        "postvak in",
        "indkakke",
        "inkorgen",
        "skräppost",
        "correo no deseado",
        "pourriel",
        "papierkorb",
        "corbeille",
        "papelera",
        "cestino",
        "lixeira",
        "gesendet",
        "envoyés",
        "enviados",
        "inviata",
        "wichtig",
        "importants",
        "importantes",
        "spam-verdacht",
        "受信トレイ",
        "迷惑メール",
        "收件箱",
        "垃圾邮件",
    }
)
_ENCODED_WORD_RE = re.compile(r"=\?[^?]+\?[bBqQ]\?[^?]*\?=")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
_CONTRADICTIONS: tuple[tuple[SystemLabel, SystemLabel, LabelAmbiguity], ...] = (
    (SystemLabel.SPAM, SystemLabel.INBOX, LabelAmbiguity.SPAM_AND_INBOX),
    (SystemLabel.TRASH, SystemLabel.INBOX, LabelAmbiguity.TRASH_AND_INBOX),
    (SystemLabel.SPAM, SystemLabel.SENT, LabelAmbiguity.SPAM_AND_SENT),
    (SystemLabel.DRAFT, SystemLabel.SENT, LabelAmbiguity.DRAFT_AND_SENT),
)


@dataclass(frozen=True)
class WeakLabels:
    """Provider labels at export time. Hints only, never truth."""

    system: frozenset[SystemLabel]
    categories: frozenset[Category]
    custom_names: tuple[str, ...] = field(repr=False)
    ambiguities: frozenset[LabelAmbiguity]
    source: LabelSource = LabelSource.PROVIDER_EXPORT
    strength: LabelStrength = LabelStrength.WEAK

    @property
    def custom_count(self) -> int:
        return len(self.custom_names)

    @property
    def ambiguous(self) -> bool:
        return bool(self.ambiguities)

    def custom_digests(self, salt: bytes) -> tuple[str, ...]:
        """Salted digests of custom labels, safe to store. Salt must be secret."""
        if not isinstance(salt, bytes) or len(salt) < 16:
            raise ValueError("salt must be at least 16 bytes")
        return tuple(
            sorted(
                hmac.new(salt, name.casefold().encode(), hashlib.sha256).hexdigest()
                for name in self.custom_names
            )
        )

    def to_codes(self) -> dict[str, object]:
        """Log-safe projection: enum codes and counts, no custom names."""
        return {
            "source": self.source.value,
            "strength": self.strength.value,
            "system": sorted(s.value for s in self.system),
            "categories": sorted(c.value for c in self.categories),
            "custom_count": self.custom_count,
            "ambiguities": sorted(a.value for a in self.ambiguities),
        }


def _split(raw: str) -> tuple[list[str], bool]:
    """Split on commas outside double quotes. Returns (items, balanced)."""
    items: list[str] = []
    buf: list[str] = []
    quoted = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '"':
            if quoted and i + 1 < len(raw) and raw[i + 1] == '"':  # "" escape
                buf.append('"')
                i += 2
                continue
            quoted = not quoted
        elif ch == "," and not quoted:
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    items.append("".join(buf))
    return items, not quoted


def normalize_labels(raw: str | None) -> WeakLabels:
    """Parse a raw ``X-Gmail-Labels`` value. Never raises; oddities become codes."""
    flags: set[LabelAmbiguity] = set()
    system: set[SystemLabel] = set()
    categories: set[Category] = set()
    custom: list[str] = []
    if raw is None or not raw.strip():
        flags.add(LabelAmbiguity.NO_LABELS)
        return WeakLabels(frozenset(), frozenset(), (), frozenset(flags))
    if len(raw) >= MAX_RAW_CHARS:
        flags.add(LabelAmbiguity.RAW_TRUNCATED)
    if _ENCODED_WORD_RE.search(raw):
        flags.add(LabelAmbiguity.ENCODED_WORD)
    if _CTRL_RE.search(raw):
        flags.add(LabelAmbiguity.CONTROL_CHARACTERS)
        raw = _CTRL_RE.sub(" ", raw)
    items, balanced = _split(raw[:MAX_RAW_CHARS])
    if not balanced:
        flags.add(LabelAmbiguity.UNBALANCED_QUOTES)
    if len(items) > MAX_LABELS:
        flags.add(LabelAmbiguity.TOO_MANY_LABELS)
        items = items[:MAX_LABELS]
    for item in items:
        name = " ".join(item.split())
        if not name:
            flags.add(LabelAmbiguity.EMPTY_LABEL)
            continue
        if len(name) > MAX_LABEL_CHARS:
            flags.add(LabelAmbiguity.LABEL_TOO_LONG)
            name = name[:MAX_LABEL_CHARS]
        key = name.casefold()
        if key in _SYSTEM_NAMES:
            system.add(_SYSTEM_NAMES[key])
            continue
        match = _CATEGORY_RE.match(name)
        if match and match.group(1).strip():
            category = _CATEGORY_NAMES.get(match.group(1).strip().casefold())
            if category is not None:
                categories.add(category)
            else:
                flags.add(LabelAmbiguity.UNKNOWN_CATEGORY)
                custom.append(name)
            continue
        if key in _LOCALIZED_HINTS:
            flags.add(LabelAmbiguity.POSSIBLE_LOCALIZED_SYSTEM_LABEL)
        custom.append(name)
    if len(categories) > 1:
        flags.add(LabelAmbiguity.MULTIPLE_CATEGORIES)
    for a, b, code in _CONTRADICTIONS:
        if a in system and b in system:
            flags.add(code)
    if not system and not categories and not custom:
        flags.add(LabelAmbiguity.NO_LABELS)
    return WeakLabels(
        frozenset(system),
        frozenset(categories),
        tuple(dict.fromkeys(custom)),
        frozenset(flags),
    )


@dataclass
class LabelSummary:
    """Counts only, per account. Safe for manifests and logs."""

    messages: int = 0
    system: Counter[str] = field(default_factory=Counter)
    categories: Counter[str] = field(default_factory=Counter)
    ambiguities: Counter[str] = field(default_factory=Counter)
    with_custom: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "messages": self.messages,
            "system": dict(sorted(self.system.items())),
            "categories": dict(sorted(self.categories.items())),
            "ambiguities": dict(sorted(self.ambiguities.items())),
            "with_custom": self.with_custom,
            "source": LabelSource.PROVIDER_EXPORT.value,
            "strength": LabelStrength.WEAK.value,
        }


def summarize(items: Iterable[tuple[str, str | None]]) -> dict[str, LabelSummary]:
    """Summarise ``(account_scope, raw_labels)`` pairs into per-account counts."""
    out: dict[str, LabelSummary] = {}
    for account, raw in items:
        labels = normalize_labels(raw)
        s = out.setdefault(account, LabelSummary())
        s.messages += 1
        s.system.update(x.value for x in labels.system)
        s.categories.update(x.value for x in labels.categories)
        s.ambiguities.update(x.value for x in labels.ambiguities)
        s.with_custom += 1 if labels.custom_count else 0
    return out


__all__ = [
    "Category",
    "LabelAmbiguity",
    "LabelSource",
    "LabelStrength",
    "LabelSummary",
    "SystemLabel",
    "WeakLabels",
    "normalize_labels",
    "summarize",
]
