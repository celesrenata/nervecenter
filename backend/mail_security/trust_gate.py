"""Pre-promotion trust gate for legacy mail promotion (Task 7.4, R-001/R-011/R-012).

The legacy ``CommunicationPipeline`` turns detected findings into durable,
actionable rows (``attention_items`` for Today, ``commitments``) synchronously
at ingest, before any security verdict exists. Adding a risk check *after*
that does nothing to stop a malicious obligation (design §8.3). This gate
sits *before* the durable write:

* **OFF (default).** No gate object is bound; the pipeline path is unchanged.
  Only the exact value ``review_gated`` of
  ``NERVE_MAIL_SECURITY_PROMOTION_GATE`` *and* the mail-security flag ON turn
  it on. Anything else (unset, empty, typo, ``enforce``) is OFF.
* **REVIEW_GATED.** For a communication whose ``content_ref`` is a committed
  mail ref (``content://mail/<uuid>/<digest>``), each actionable write is
  *held* in ``mail_security_held_promotions`` under its exact deterministic
  target id (``at-<..>`` / ``ct-<..>``) instead of being written. A release
  pass then reads the newest verdict for that message:

  - ``SAFE_TO_PROCESS`` / ``LOW_PRIORITY`` -> the original write is replayed
    with the same id (get-or-create), so ids, columns and idempotency are
    exactly what the ungated path would have produced;
  - ``REVIEW`` / ``RESTRICTED`` / ``INCOMPLETE`` -> ``REVIEW_CANDIDATE``:
    kept, shown in the security review queue, never actionable;
  - no verdict yet -> stays ``HELD`` (fail closed).

  A later reassessment can still release a review candidate; reviewer
  feedback alone never does (no implicit trust, 7.2).

Bypass paths (documented, not gated): communications whose ``content_ref`` is
not a committed mail ref (iMessage/SMS/voicemail, and authenticated
``POST /api/communications/ingest`` callers using their own refs) have no
NerveShield source and pass through unchanged. Follow-ups are created only
for OUTGOING mail, which the mail worker never ingests.

A hold failure never falls back to the actionable write.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from backend.mail_security.backfill import parse_content_ref

ENV_PROMOTION_GATE = "NERVE_MAIL_SECURITY_PROMOTION_GATE"
GATE_REVIEW_GATED = "review_gated"
GATE_POLICY_VERSION = "mail-promotion-gate@1"

RELEASE_DISPOSITIONS = frozenset({"SAFE_TO_PROCESS", "LOW_PRIORITY"})
CANDIDATE_DISPOSITIONS = frozenset({"REVIEW", "RESTRICTED", "INCOMPLETE"})

#: Column keys that carry datetimes in a held payload.
_DATETIME_KEYS = frozenset({"deadline_at", "due_at"})


class GateMode(StrEnum):
    OFF = "OFF"
    REVIEW_GATED = "REVIEW_GATED"


class HoldStatus(StrEnum):
    HELD = "HELD"
    RELEASED = "RELEASED"
    REVIEW_CANDIDATE = "REVIEW_CANDIDATE"


class TargetKind(StrEnum):
    ATTENTION = "attention"
    COMMITMENT = "commitment"


class GateDecision(StrEnum):
    RELEASE = "RELEASE"
    REVIEW_CANDIDATE = "REVIEW_CANDIDATE"
    WAIT = "WAIT"


def gate_mode(env: Mapping[str, str] | None = None) -> GateMode:
    """REVIEW_GATED only for the exact opt-in value with mail security ON."""
    from backend.mail_security.ingestion_seam import mail_security_enabled

    source = env if env is not None else os.environ
    raw = source.get(ENV_PROMOTION_GATE)
    if not (isinstance(raw, str) and raw.strip().lower() == GATE_REVIEW_GATED):
        return GateMode.OFF
    if not mail_security_enabled(dict(source)):
        return GateMode.OFF
    return GateMode.REVIEW_GATED


ENV_PROMOTION_GATE_SCOPES = "NERVE_MAIL_SECURITY_PROMOTION_GATE_SCOPES"
_SCOPE_ENTRY = re.compile(r"conn:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def gate_scopes(env: Mapping[str, str] | None = None) -> frozenset[str] | None:
    """Canary allow-list. ``*`` -> ``None`` (all mail accounts); a list of
    ``conn:<uuid>`` -> that set; unset, empty or any malformed entry ->
    empty set (no account gated)."""
    source = env if env is not None else os.environ
    raw = str(source.get(ENV_PROMOTION_GATE_SCOPES, "") or "").strip()
    if raw == "*":
        return None
    entries = [e.strip() for e in raw.split(",") if e.strip()]
    if not entries or not all(_SCOPE_ENTRY.fullmatch(e) for e in entries):
        return frozenset()
    return frozenset(entries)


def gate_scope(content_ref: object) -> str | None:
    """``conn:<uuid>`` for a committed mail ref, else ``None`` (not gated)."""
    parsed = parse_content_ref(content_ref)
    return None if parsed is None else f"conn:{parsed.connection_id}"


def decide(disposition: str | None) -> GateDecision:
    if disposition in RELEASE_DISPOSITIONS:
        return GateDecision.RELEASE
    if disposition in CANDIDATE_DISPOSITIONS:
        return GateDecision.REVIEW_CANDIDATE
    return GateDecision.WAIT


def encode_payload(columns: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in columns.items():
        out[key] = value.isoformat() if isinstance(value, datetime) else value
    return out


def decode_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    for key in _DATETIME_KEYS & set(out):
        if isinstance(out[key], str):
            out[key] = datetime.fromisoformat(out[key])
    return out


class HeldPromotionStore(Protocol):
    async def hold(
        self,
        *,
        account_scope: str,
        content_ref: str,
        communication_id: str,
        target_kind: str,
        target_id: str,
        payload: dict[str, Any],
    ) -> Any: ...


class MailPromotionGate:
    """Adapter the legacy pipeline calls. Holds; never writes actionable rows."""

    policy_version = GATE_POLICY_VERSION

    def __init__(self, store: HeldPromotionStore, scopes: frozenset[str] | None = None) -> None:
        self._store = store
        #: Canary allow-list (Task 7.8). ``None`` means every mail account
        #: (``*``); an empty set means none.
        self._scopes = scopes

    @property
    def scopes(self) -> frozenset[str] | None:
        return self._scopes

    def applies(self, communication: Any) -> bool:
        scope = gate_scope(getattr(communication, "content_ref", None))
        if scope is None:
            return False
        return self._scopes is None or scope in self._scopes

    async def hold(
        self,
        *,
        kind: TargetKind | str,
        target_id: str,
        communication: Any,
        columns: Mapping[str, Any],
    ) -> None:
        content_ref = str(getattr(communication, "content_ref", ""))
        scope = gate_scope(content_ref)
        if scope is None:  # defensive: only called after applies()
            raise ValueError("not a gated communication")
        await self._store.hold(
            account_scope=scope,
            content_ref=content_ref,
            communication_id=str(getattr(communication, "communication_id", "")),
            target_kind=TargetKind(kind).value,
            target_id=target_id,
            payload=encode_payload(columns),
        )


__all__ = [
    "CANDIDATE_DISPOSITIONS",
    "ENV_PROMOTION_GATE",
    "ENV_PROMOTION_GATE_SCOPES",
    "GATE_POLICY_VERSION",
    "GATE_REVIEW_GATED",
    "RELEASE_DISPOSITIONS",
    "GateDecision",
    "GateMode",
    "HeldPromotionStore",
    "HoldStatus",
    "MailPromotionGate",
    "TargetKind",
    "decide",
    "decode_payload",
    "encode_payload",
    "gate_mode",
    "gate_scope",
    "gate_scopes",
]
