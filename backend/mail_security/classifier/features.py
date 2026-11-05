"""Minimum-necessary feature record for the NerveShield CPU baseline (task 3.2).

Design §6: only de-identified, minimum-necessary features may reach a
classifier. A :class:`FeatureRecord` therefore carries ONLY values the
deterministic spine already extracts:

* short character n-grams of URL domains (lowercase ``[a-z0-9._-]``),
* finding rule ids and their counts,
* structural counts,
* sender-domain label tokens (the domain's labels, never an address).

There is deliberately NO field for subject, body, display name, address,
URL path/query, filename or any other free text, and the model forbids extra
fields, so such data cannot be smuggled in (R-021.4, I-09). The label is NOT
part of the record (see :class:`LabeledExample`) to avoid label leakage.

Classifier output is never an authority (I-08); nothing here is wired into the
worker, policy or API.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints, field_validator

from backend.mail_security.models import _StrictModel

#: Version of the feature-record schema (bump on any field change).
FEATURE_SCHEMA_VERSION = "1.0"

MAX_NGRAMS = 128
MAX_NGRAM_LEN = 8
MAX_RULES = 32
MAX_RULE_ID_LEN = 80
MAX_TOKENS = 8
MAX_TOKEN_LEN = 32
MAX_COUNT = 10_000

#: A domain n-gram: lowercase letters, digits, dot, hyphen, underscore only.
Ngram = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_NGRAM_LEN, pattern=r"^[a-z0-9._-]+$")
]
#: A sender-domain label token (one DNS label; no dots, no '@').
DomainToken = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_TOKEN_LEN, pattern=r"^[a-z0-9-]+$")
]
#: A finding rule id (e.g. ``URL_SHORTENED_LINK``, ``auth.sender.mismatch``).
RuleId = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=MAX_RULE_ID_LEN, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$"
    ),
]
Count = Annotated[int, Field(ge=0, le=MAX_COUNT)]


def char_ngrams(domain: str, n: int = 3, limit: int = MAX_NGRAMS) -> tuple[str, ...]:
    """Distinct character n-grams of an already-lowercased URL domain.

    Order of first occurrence, capped at ``limit``. Characters outside the
    schema alphabet (``[a-z0-9._-]``) are dropped, so a domain can never carry
    anything but domain characters into a record.
    """
    cleaned = "".join(ch for ch in domain.lower() if ch.isascii() and (ch.isalnum() or ch in "._-"))
    seen: dict[str, None] = {}
    for i in range(len(cleaned) - n + 1):
        seen.setdefault(cleaned[i : i + n], None)
        if len(seen) >= limit:
            break
    return tuple(seen)


class Label(StrEnum):
    """Ground-truth class of a SYNTHETIC example. Never present in a record."""

    BENIGN = "BENIGN"
    MALICIOUS = "MALICIOUS"


class FeatureRecord(_StrictModel):
    """Strict, bounded, frozen classifier input. Extra fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: str = FEATURE_SCHEMA_VERSION
    url_domain_ngrams: Annotated[tuple[Ngram, ...], Field(max_length=MAX_NGRAMS)] = ()
    finding_rule_counts: dict[RuleId, Count] = Field(default_factory=dict)
    url_count: Count = 0
    distinct_url_domain_count: Count = 0
    attachment_count: Count = 0
    mime_part_count: Count = 0
    remote_image_count: Count = 0
    sender_domain_tokens: Annotated[tuple[DomainToken, ...], Field(max_length=MAX_TOKENS)] = ()

    @field_validator("schema_version")
    @classmethod
    def _version_pinned(cls, value: str) -> str:
        if value != FEATURE_SCHEMA_VERSION:
            raise ValueError("unsupported feature schema_version")
        return value

    @field_validator("finding_rule_counts")
    @classmethod
    def _bounded_rules(cls, value: dict[str, int]) -> dict[str, int]:
        if len(value) > MAX_RULES:
            raise ValueError("too many finding rule ids")
        return value


class LabeledExample(_StrictModel):
    """A feature record plus its SYNTHETIC ground-truth label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    features: FeatureRecord
    label: Label
