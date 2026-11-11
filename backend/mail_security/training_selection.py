"""Opt-in training data selection (NerveShield task 6.1).

Library only; nothing calls it, nothing trains. It turns a pool of candidate
examples into a *selection* for human review, in the design's order (section
11): candidate pool -> de-dup -> permission/consent -> secret/PII detection ->
pseudonymisation -> poisoning heuristics -> (human review, later).

Rules (R-014, R-015, R-016, R-017):

* **Consent first.** An example is eligible only if its account has an active,
  unexpired, unrevoked grant covering its source kind and the TRAINING
  purpose. No grant means excluded, never "default yes".
* **Labels from approved feedback only.** A label must come from an
  ``APPROVED`` TRAINING proposal of the 5.5 feedback ledger for the same
  account. Provider labels, model output and unreviewed feedback are never
  labels.
* **Secrets exclude, PII is pseudonymised.** Anything that looks like a
  credential, private key, card number, bank/IBAN number, national id or
  one-time code excludes the whole example (done criterion). Emails, phone
  numbers and URLs are replaced with per-dataset HMAC tokens (stable within
  one selection, unlinkable across selections). A post-redaction re-scan must
  come back clean or the example is excluded.
* **Duplicates and poisoning.** Exact and near duplicates (word 5-shingle
  Jaccard) are dropped; near-identical texts carrying different labels are
  all excluded as a label conflict; texts with instructions aimed at a model
  are held out; no single sender may exceed a share of the dataset.
* **Per-account.** Selections never mix accounts.
* The output manifest holds counts and reason codes; included examples hold
  pseudonymised text only. Selection status is ``PENDING_HUMAN_REVIEW``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from backend.mail_security.feedback import Destination, FeedbackKind, Proposal, ProposalStatus

SELECTION_VERSION = "mail-training-selection@1"
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
MAX_TEXT_CHARS = 8000


class SourceKind(StrEnum):
    TAKEOUT_IMPORT = "TAKEOUT_IMPORT"
    LIVE_MAIL = "LIVE_MAIL"
    SYNTHETIC = "SYNTHETIC"


class TrainingLabel(StrEnum):
    MALICIOUS = "MALICIOUS"
    BENIGN = "BENIGN"


class Exclusion(StrEnum):
    INVALID_EXAMPLE = "INVALID_EXAMPLE"
    WRONG_ACCOUNT = "WRONG_ACCOUNT"
    DUPLICATE_EXACT = "DUPLICATE_EXACT"
    DUPLICATE_NEAR = "DUPLICATE_NEAR"
    NO_CONSENT = "NO_CONSENT"
    CONSENT_EXPIRED = "CONSENT_EXPIRED"
    CONSENT_REVOKED = "CONSENT_REVOKED"
    SOURCE_NOT_CONSENTED = "SOURCE_NOT_CONSENTED"
    NO_APPROVED_LABEL = "NO_APPROVED_LABEL"
    LABEL_UNSUPPORTED = "LABEL_UNSUPPORTED"
    SECRET_DETECTED = "SECRET_DETECTED"
    REDACTION_INCOMPLETE = "REDACTION_INCOMPLETE"
    LABEL_CONFLICT = "LABEL_CONFLICT"
    MODEL_INSTRUCTION_TEXT = "MODEL_INSTRUCTION_TEXT"
    SENDER_OVERREPRESENTED = "SENDER_OVERREPRESENTED"


class SecretKind(StrEnum):
    PRIVATE_KEY = "PRIVATE_KEY"
    API_TOKEN = "API_TOKEN"
    JWT = "JWT"
    PASSWORD = "PASSWORD"
    ONE_TIME_CODE = "ONE_TIME_CODE"
    PAYMENT_CARD = "PAYMENT_CARD"
    IBAN = "IBAN"
    BANK_ACCOUNT = "BANK_ACCOUNT"
    NATIONAL_ID = "NATIONAL_ID"


class SelectionStatus(StrEnum):
    PENDING_HUMAN_REVIEW = "PENDING_HUMAN_REVIEW"


@dataclass(frozen=True)
class ConsentGrant:
    account_scope: str
    consent_ref: str
    sources: frozenset[SourceKind]
    granted_at: datetime
    expires_at: datetime
    purposes: frozenset[str] = frozenset({"TRAINING"})
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class CandidateExample:
    """Already-extracted text for one message. Lives in process memory only."""

    example_id: str
    account_scope: str
    source: SourceKind
    sender_digest: str  # opaque, e.g. trust-index digest; never an address
    assessment_ref: str  # joins to feedback proposals
    subject: str = field(repr=False)
    body: str = field(repr=False)


@dataclass(frozen=True)
class SelectedExample:
    example_id: str
    label: TrainingLabel
    label_proposal_id: str  # provenance: the approved feedback proposal
    consent_ref: str
    source: SourceKind
    text: str = field(repr=False)  # pseudonymised
    pii_replacements: int = 0


@dataclass
class SelectionManifest:
    version: str
    account_scope: str
    created_at: datetime
    status: SelectionStatus = SelectionStatus.PENDING_HUMAN_REVIEW
    candidates: int = 0
    included: int = 0
    excluded: Counter[str] = field(default_factory=Counter)
    secrets_by_kind: Counter[str] = field(default_factory=Counter)
    labels: Counter[str] = field(default_factory=Counter)
    consent_refs: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "account_scope": self.account_scope,
            "created_at": self.created_at.isoformat(),
            "status": self.status.value,
            "candidates": self.candidates,
            "included": self.included,
            "excluded": {str(k): v for k, v in sorted(self.excluded.items()) if v},
            "secrets_by_kind": dict(sorted(self.secrets_by_kind.items())),
            "labels": dict(sorted(self.labels.items())),
            "consent_refs": sorted(self.consent_refs),
        }


@dataclass(frozen=True)
class SelectionPolicy:
    near_duplicate_jaccard: float = 0.85
    max_sender_share: float = 0.2  # of included examples, once at least min_for_share
    min_for_share: int = 10


# -- secret / PII detection --------------------------------------------------------------

_SECRET_PATTERNS: tuple[tuple[SecretKind, re.Pattern[str]], ...] = (
    (SecretKind.PRIVATE_KEY, re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        SecretKind.API_TOKEN,
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}"
            r"|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|glpat-[A-Za-z0-9_-]{20,})"
        ),
    ),
    (SecretKind.JWT, re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    (
        SecretKind.PASSWORD,
        re.compile(r"(?i)\b(?:password|passwort|passcode|pwd|pin)\s*(?:is|:|=)\s*\S{4,}"),
    ),
    (
        SecretKind.ONE_TIME_CODE,
        re.compile(
            r"(?i)\b(?:code|otp|verification|security code|one[- ]time)\b[^\n]{0,40}?\b\d{4,8}\b"
        ),
    ),
    (SecretKind.IBAN, re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")),
    (
        SecretKind.BANK_ACCOUNT,
        re.compile(r"(?i)\b(?:account|acct|routing|sort code|aba)\b[^\n]{0,20}?\b\d{6,17}\b"),
    ),
    (SecretKind.NATIONAL_ID, re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
)
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def detect_secrets(text: str) -> set[SecretKind]:
    found = {kind for kind, pattern in _SECRET_PATTERNS if pattern.search(text)}
    for match in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.add(SecretKind.PAYMENT_CARD)
            break
    return found


_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("URL", re.compile(r"\b(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("PHONE", re.compile(r"(?<![\w])\+?\d[\d ().-]{7,}\d(?![\w])")),
)
_TOKEN_RE = re.compile(r"<(?:URL|EMAIL|PHONE)_[0-9a-f]{10}>")


def pseudonymize(text: str, salt: bytes) -> tuple[str, int]:
    """Replace URLs, emails and phone numbers with stable salted tokens."""
    count = 0

    def token(kind: str) -> object:
        def sub(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            value = match.group().strip().lower()
            digest = hmac.new(salt, f"{kind}\x1f{value}".encode(), hashlib.sha256).hexdigest()
            return f"<{kind}_{digest[:10]}>"

        return sub

    for kind, pattern in _PII_PATTERNS:
        text = pattern.sub(token(kind), text)  # type: ignore[arg-type]
    return text, count


def _residual_pii(text: str) -> bool:
    stripped = _TOKEN_RE.sub(" ", text)
    return any(p.search(stripped) for _, p in _PII_PATTERNS)


# -- poisoning / duplicates --------------------------------------------------------------

_INSTRUCTION_RE = re.compile(
    r"(?i)(ignore (?:all |any )?(?:previous|prior|above) (?:instructions|prompts)"
    r"|disregard (?:the )?(?:system|previous) (?:prompt|instructions)"
    r"|you are now (?:an?|in) |\bsystem prompt\b|\bas an ai\b|<\|im_start\|>|\[INST\])"
)


def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[^\w<>]+", " ", text.lower()).split())


def _shingles(text: str, k: int = 5) -> frozenset[str]:
    words = text.split()
    if len(words) < k:
        return frozenset({" ".join(words)}) if words else frozenset()
    return frozenset(" ".join(words[i : i + k]) for i in range(len(words) - k + 1))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_LABEL_FOR_KIND = {
    FeedbackKind.CONFIRMED_THREAT: TrainingLabel.MALICIOUS,
    FeedbackKind.FALSE_NEGATIVE: TrainingLabel.MALICIOUS,
    FeedbackKind.FALSE_POSITIVE: TrainingLabel.BENIGN,
}


# -- selection ---------------------------------------------------------------------------


def _consent_for(
    example: CandidateExample, grants: list[ConsentGrant], now: datetime
) -> tuple[ConsentGrant | None, Exclusion | None]:
    mine = [
        g for g in grants if g.account_scope == example.account_scope and "TRAINING" in g.purposes
    ]
    if not mine:
        return None, Exclusion.NO_CONSENT
    best: Exclusion = Exclusion.NO_CONSENT
    for grant in mine:
        if grant.revoked_at is not None and grant.revoked_at <= now:
            best = Exclusion.CONSENT_REVOKED
            continue
        if not grant.granted_at <= now < grant.expires_at:
            best = Exclusion.CONSENT_EXPIRED if best is Exclusion.NO_CONSENT else best
            continue
        if example.source not in grant.sources:
            best = Exclusion.SOURCE_NOT_CONSENTED if best is Exclusion.NO_CONSENT else best
            continue
        return grant, None
    return None, best


def select_training_examples(
    account_scope: str,
    candidates: list[CandidateExample],
    grants: list[ConsentGrant],
    proposals: list[Proposal],
    *,
    salt: bytes,
    now: datetime,
    policy: SelectionPolicy | None = None,
) -> tuple[SelectionManifest, list[SelectedExample]]:
    if not _ACCOUNT_RE.fullmatch(account_scope):
        raise ValueError("invalid account scope")
    if not isinstance(salt, bytes) or len(salt) < 32:
        raise ValueError("salt must be at least 32 bytes")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    policy = policy or SelectionPolicy()
    manifest = SelectionManifest(SELECTION_VERSION, account_scope, now)
    manifest.candidates = len(candidates)

    by_assessment: dict[str, list[tuple[TrainingLabel | None, str]]] = {}
    for p in proposals:
        if (
            p.account_scope == account_scope
            and p.destination is Destination.TRAINING
            and p.status is ProposalStatus.APPROVED
            and p.assessment_ref
        ):
            by_assessment.setdefault(p.assessment_ref, []).append(
                (_LABEL_FOR_KIND.get(p.kind), p.proposal_id)
            )

    # stage 1: validity, account, exact and near duplicates (pool order is kept)
    staged: list[tuple[CandidateExample, str, frozenset[str]]] = []
    seen_exact: set[str] = set()
    for ex in candidates:
        if (
            not isinstance(ex, CandidateExample)
            or not isinstance(ex.subject, str)
            or not isinstance(ex.body, str)
        ):
            manifest.excluded[Exclusion.INVALID_EXAMPLE] += 1
            continue
        if ex.account_scope != account_scope:
            manifest.excluded[Exclusion.WRONG_ACCOUNT] += 1
            continue
        text = f"{ex.subject}\n{ex.body}"[:MAX_TEXT_CHARS]
        norm = _normalize(text)
        digest = hashlib.sha256(norm.encode()).hexdigest()
        if digest in seen_exact:
            manifest.excluded[Exclusion.DUPLICATE_EXACT] += 1
            continue
        seen_exact.add(digest)
        shingles = _shingles(norm)
        staged.append((ex, text, shingles))

    # stage 2: consent, label, secrets, pseudonymisation, instruction text
    survivors: list[tuple[CandidateExample, str, frozenset[str], TrainingLabel, str, str, int]] = []
    for ex, text, shingles in staged:
        grant, why = _consent_for(ex, grants, now)
        if grant is None:
            manifest.excluded[why or Exclusion.NO_CONSENT] += 1
            continue
        found = by_assessment.get(ex.assessment_ref, [])
        if not found:
            manifest.excluded[Exclusion.NO_APPROVED_LABEL] += 1
            continue
        distinct = {label for label, _ in found}
        if None in distinct or len(distinct) != 1:
            manifest.excluded[
                Exclusion.LABEL_UNSUPPORTED if None in distinct else Exclusion.LABEL_CONFLICT
            ] += 1
            continue
        label, proposal_id = found[-1]
        assert label is not None  # noqa: S101 - None filtered above
        secrets = detect_secrets(text)
        if secrets:
            manifest.excluded[Exclusion.SECRET_DETECTED] += 1
            manifest.secrets_by_kind.update(s.value for s in secrets)
            continue
        if _INSTRUCTION_RE.search(text):
            manifest.excluded[Exclusion.MODEL_INSTRUCTION_TEXT] += 1
            continue
        clean, replaced = pseudonymize(text, salt)
        if detect_secrets(clean) or _residual_pii(clean):
            manifest.excluded[Exclusion.REDACTION_INCOMPLETE] += 1
            continue
        survivors.append((ex, clean, shingles, label, proposal_id, grant.consent_ref, replaced))

    # stage 3: near duplicates and label conflicts among near-identical texts
    groups: list[list[int]] = []
    for i, (_, _, shingles, *_rest) in enumerate(survivors):
        for group in groups:
            if _jaccard(shingles, survivors[group[0]][2]) >= policy.near_duplicate_jaccard:
                group.append(i)
                break
        else:
            groups.append([i])
    kept: list[int] = []
    for group in groups:
        group_labels = {survivors[i][3] for i in group}
        if len(group_labels) > 1:
            manifest.excluded[Exclusion.LABEL_CONFLICT] += len(group)
            continue
        kept.append(group[0])
        if len(group) > 1:
            manifest.excluded[Exclusion.DUPLICATE_NEAR] += len(group) - 1

    # stage 4: sender share cap
    out: list[SelectedExample] = []
    per_sender: Counter[str] = Counter()
    cap = max(1, int(policy.max_sender_share * max(len(kept), policy.min_for_share)))
    for i in sorted(kept):
        ex, clean, _, label, proposal_id, consent_ref, replaced = survivors[i]
        if per_sender[ex.sender_digest] >= cap:
            manifest.excluded[Exclusion.SENDER_OVERREPRESENTED] += 1
            continue
        per_sender[ex.sender_digest] += 1
        out.append(
            SelectedExample(
                ex.example_id, label, proposal_id, consent_ref, ex.source, clean, replaced
            )
        )
        manifest.labels[label.value] += 1
        manifest.consent_refs.add(consent_ref)
    manifest.included = len(out)
    return manifest, out


__all__ = [
    "SELECTION_VERSION",
    "CandidateExample",
    "ConsentGrant",
    "Exclusion",
    "SecretKind",
    "SelectedExample",
    "SelectionManifest",
    "SelectionPolicy",
    "SelectionStatus",
    "SourceKind",
    "TrainingLabel",
    "detect_secrets",
    "pseudonymize",
    "select_training_examples",
]
