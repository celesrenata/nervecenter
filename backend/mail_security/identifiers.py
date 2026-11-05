"""Pure deterministic identifier / revision functions for NerveShield.

Phase 1, Task 1.2. These are **pure** functions: the same inputs always
produce the same output, with no clock, randomness, global state, network or
I/O. They derive the stable logical identities the pipeline needs so that
(R-018) reprocessing the same source for a fixed analysis version produces a
stable identity (no duplicate events/tasks/attention items, I-11) and
(R-023/T-15) the same RFC ``Message-ID`` delivered to two different accounts
yields two *distinct* logical identities.

Design anchors (`.kiro/specs/nerve-shield/design.md` §9 "Canonical IDs and
replay"):

- The logical source key is **account-scoped**: a stable key derived from the
  provider UID *or* the RFC ``Message-ID`` plus folder/source semantics, with
  collision resolution and explicit versioning. A raw, attacker-controlled
  ``Message-ID`` is **never** used alone as database authority — the account
  scope is always mixed in, and the discriminator records which kind of
  identity the key was derived from so a provider-UID key can never collide
  with a ``Message-ID`` key.
- ``assessment_id`` depends on the immutable *source revision* and the
  ``analysis_version``. A rescan creates a new *revision* (new id) rather than
  overwriting lineage; a bump of the analysis version likewise yields a new id.
  Re-processing the identical ``(account, source, revision, analysis_version)``
  is stable (idempotent, I-11).
- The outbox job key is ``(account_scope, message_key, analysis_profile_version)``
  so replays do not create duplicate consequential effects (T-16).

Isolation boundary vs. privacy fingerprints (design §9 "privacy-preserving
fingerprints must not become access keys"):

- The **account scope** is the isolation boundary. It is carried into every
  derived key as a *structural, length-bounded prefix component* (not merely
  folded into a hash) so that two accounts can never share a logical identity
  even on identical message content, and so access control keys on the account
  scope remain meaningful.
- The SHA-256 digests used below are **privacy-preserving fingerprints of the
  variable identity material only** — they compress unbounded / possibly
  sensitive provider UIDs, Message-IDs and folder names into a bounded opaque
  token. They are *not* access-control keys: knowing a fingerprint must never
  grant access to another account's records (R-023.4). Isolation comes from the
  account scope component, never from the secrecy of the hash.

All emitted identifiers are restricted to the ``[A-Za-z0-9:_-]`` alphabet so
they satisfy the :data:`backend.mail_security.models.OpaqueRef` /
``account_scope`` / ``assessment_id`` contract patterns and never carry raw
email text, addresses or whitespace (I-05).
"""

from __future__ import annotations

import hashlib
import re

# -- constants ---------------------------------------------------------------

#: Hash algorithm used to derive bounded opaque fingerprints. Documented so the
#: derivation is auditable (R-021.4). SHA-256 is a privacy-preserving
#: fingerprint of the *variable* identity material only; it is **not** an
#: access-control key (design §9). Changing this constant changes every derived
#: key and so is itself a breaking identity-scheme change.
HASH_ALGORITHM = "sha256"

#: Number of hex characters of the SHA-256 digest kept in a derived token. 160
#: bits (40 hex chars) is ample collision resistance for logical identities
#: while keeping ids short enough for the ``assessment_id`` / ``OpaqueRef``
#: length caps.
_DIGEST_HEX_LEN = 40

#: Explicit version of the identity-derivation scheme itself. If the way a key
#: is composed ever changes, bump this so old and new keys never silently
#: collide (design §9 "explicit versioning").
KEY_SCHEME_VERSION = "k1"

#: Discriminator tags recording which kind of source identity a logical source
#: key was derived from. Mixing the tag into the hash input guarantees a
#: provider-UID key and a Message-ID key can never collide even if the raw
#: provider UID and Message-ID strings happen to be byte-equal.
_SOURCE_KIND_PROVIDER_UID = "puid"
_SOURCE_KIND_MESSAGE_ID = "rfcmid"

#: Account scopes are opaque, already-bounded tenant identifiers. We accept the
#: contract alphabet so the scope can be used directly as a structural key
#: component. (Mirrors the ``account_scope`` pattern on the models.)
_ACCOUNT_SCOPE_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")

#: Analysis profile / version tokens are safe, bounded version strings.
_ANALYSIS_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")

#: Field separator used when composing hash preimages. ``\x1f`` (ASCII unit
#: separator) can never appear inside any accepted input (all inputs are either
#: the contract alphabet or arbitrary-but-encoded source strings handled via
#: length-prefix framing), so it cannot be used to forge a boundary.
_FIELD_SEP = "\x1f"


# -- input validation --------------------------------------------------------


def _require_account_scope(account_scope: str) -> str:
    """Validate the account scope — the isolation boundary (R-023).

    The scope must be present and match the bounded contract alphabet so it can
    be used as a structural key component rather than being hidden inside a
    hash. An empty or malformed scope is rejected rather than silently folded
    away, because losing the scope would collapse the cross-account isolation
    boundary.
    """
    if not isinstance(account_scope, str) or not _ACCOUNT_SCOPE_RE.match(account_scope):
        raise ValueError(
            "account_scope must be 1-128 chars of [A-Za-z0-9:_-]; it is the "
            "cross-account isolation boundary and cannot be empty"
        )
    return account_scope


def _require_analysis_version(analysis_version: str) -> str:
    """Validate a stable analysis-profile / analysis-version token."""
    if not isinstance(analysis_version, str) or not _ANALYSIS_VERSION_RE.match(
        analysis_version
    ):
        raise ValueError(
            "analysis_version must be 1-100 chars matching "
            "[A-Za-z0-9][A-Za-z0-9_.-]* (a stable, bounded version token)"
        )
    return analysis_version


def _require_source_revision(source_revision: int) -> int:
    """Validate the immutable source revision (a monotonically used integer).

    A revision is an integer >= 1. A rescan produces a *new* revision rather
    than overwriting lineage (design §9), so callers increment this; the
    function never derives it from a clock.
    """
    # bool is an int subclass; reject it so True/False cannot masquerade as 1/0.
    if isinstance(source_revision, bool) or not isinstance(source_revision, int):
        raise ValueError("source_revision must be an int")
    if source_revision < 1:
        raise ValueError("source_revision must be >= 1")
    return source_revision


# -- hashing helpers ---------------------------------------------------------


def _frame(*parts: str) -> bytes:
    """Build an unambiguous hash preimage from arbitrary string parts.

    Each part is length-prefixed (``<byte-len>:<bytes>``) and joined with the
    unit separator so that no concatenation of differently-split inputs can
    collide (canonical, injective framing). This matters because provider UIDs,
    Message-IDs and folder names are attacker-influenced free text.
    """
    chunks: list[bytes] = []
    for part in parts:
        raw = part.encode("utf-8")
        chunks.append(f"{len(raw)}:".encode("ascii") + raw)
    return _FIELD_SEP.encode("ascii").join(chunks)


def _fingerprint(*parts: str) -> str:
    """Return a bounded hex SHA-256 fingerprint of the framed parts.

    This is a privacy-preserving fingerprint of variable identity material, not
    an access-control key (design §9).
    """
    digest = hashlib.sha256(_frame(*parts)).hexdigest()
    return digest[:_DIGEST_HEX_LEN]


# -- public identity functions -----------------------------------------------


def logical_source_key(
    account_scope: str,
    *,
    provider: str,
    provider_uid: str | None = None,
    rfc_message_id: str | None = None,
    folder: str | None = None,
) -> str:
    """Derive a stable, account-scoped logical source key.

    The key is composed as ``<scheme>:<account_scope>:<kind>:<fingerprint>``:

    - ``account_scope`` is carried structurally (not hashed away) so two
      accounts never share a key even on identical message content (R-023/T-15).
    - ``kind`` records whether the identity came from the provider UID or the
      RFC ``Message-ID`` so the two families can never collide.
    - ``fingerprint`` is a SHA-256 digest of the framed identity material
      (provider + uid/message-id + folder semantics). It is a privacy-preserving
      fingerprint, never an access key.

    Identity source selection (collision resolution / fallback):

    - If a non-empty ``provider_uid`` is given it is authoritative (providers
      assign stable per-mailbox UIDs), and the key uses the ``puid`` kind.
    - Otherwise the function falls back to the RFC ``Message-ID`` semantics
      (``rfcmid`` kind). A raw ``Message-ID`` is attacker-controlled, so it is
      never used alone: it is always combined with the account scope (structural)
      and the folder/source semantics, and tagged so it cannot be confused with
      a provider-UID identity.
    - ``folder`` participates in the fingerprint as source/folder semantics; the
      same message filed under different folders/labels yields distinct keys
      (design §9 "folder semantics").

    Raises ``ValueError`` if neither a provider UID nor a Message-ID is usable,
    because a source with no stable identity material must not silently collapse
    to a scope-only key (that would merge unrelated messages).
    """
    _require_account_scope(account_scope)
    if not isinstance(provider, str) or not provider:
        raise ValueError("provider must be a non-empty string")

    norm_provider_uid = provider_uid.strip() if isinstance(provider_uid, str) else ""
    norm_message_id = rfc_message_id.strip() if isinstance(rfc_message_id, str) else ""
    norm_folder = folder.strip() if isinstance(folder, str) else ""

    if norm_provider_uid:
        kind = _SOURCE_KIND_PROVIDER_UID
        identity_material = norm_provider_uid
    elif norm_message_id:
        kind = _SOURCE_KIND_MESSAGE_ID
        identity_material = norm_message_id
    else:
        raise ValueError(
            "a stable logical source key requires a provider_uid or an RFC "
            "message_id; refusing to derive a scope-only key that would merge "
            "unrelated messages"
        )

    fingerprint = _fingerprint(
        KEY_SCHEME_VERSION,
        account_scope,
        kind,
        provider,
        identity_material,
        norm_folder,
    )
    return f"{KEY_SCHEME_VERSION}:{account_scope}:{kind}:{fingerprint}"


def normalize_analysis_version(analysis_version: str) -> str:
    """Return the validated, stable analysis-profile / analysis-version token.

    This is the single choke point for analysis-version shape, so callers who
    build the outbox job key and the assessment id agree on the token.
    """
    return _require_analysis_version(analysis_version)


def outbox_job_key(
    account_scope: str,
    message_key: str,
    analysis_profile_version: str,
) -> str:
    """Derive the outbox job key ``(account_scope, message_key, version)``.

    Per design §9 this key prevents duplicate consequential effects on replay
    (T-16): the same ``(account_scope, message_key, analysis_profile_version)``
    always maps to the same job key, so a replayed event resolves to the same
    outbox job rather than a new one. The account scope is structural, so a job
    for account A can never be mistaken for account B's job.
    """
    _require_account_scope(account_scope)
    if not isinstance(message_key, str) or not message_key:
        raise ValueError("message_key must be a non-empty string")
    version = _require_analysis_version(analysis_profile_version)

    fingerprint = _fingerprint(
        KEY_SCHEME_VERSION,
        account_scope,
        message_key,
        version,
    )
    return f"{KEY_SCHEME_VERSION}:{account_scope}:job:{fingerprint}"


def assessment_id(
    account_scope: str,
    message_key: str,
    source_revision: int,
    analysis_version: str,
) -> str:
    """Derive the assessment id from source revision + analysis version.

    Stable (idempotent, I-11): identical
    ``(account_scope, message_key, source_revision, analysis_version)`` always
    yield the same id, so re-processing the same source for a fixed version does
    not create a duplicate assessment.

    Version change: changing either ``source_revision`` (a rescan creates a new
    immutable revision rather than overwriting) *or* ``analysis_version``
    produces a new id, preserving decision lineage (design §9, R-018.5).

    Cross-account isolation (R-023/T-15): the account scope is a structural
    component, so the same ``message_key`` under two accounts yields two
    distinct assessment ids.
    """
    _require_account_scope(account_scope)
    if not isinstance(message_key, str) or not message_key:
        raise ValueError("message_key must be a non-empty string")
    revision = _require_source_revision(source_revision)
    version = _require_analysis_version(analysis_version)

    fingerprint = _fingerprint(
        KEY_SCHEME_VERSION,
        account_scope,
        message_key,
        str(revision),
        version,
    )
    return f"{KEY_SCHEME_VERSION}:{account_scope}:asmt:{fingerprint}"


__all__ = [
    "HASH_ALGORITHM",
    "KEY_SCHEME_VERSION",
    "assessment_id",
    "logical_source_key",
    "normalize_analysis_version",
    "outbox_job_key",
]
