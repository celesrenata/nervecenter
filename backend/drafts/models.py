"""Draft domain models (gaps1 G-1.4 — R105, prompt2 §40–§41, §63).

A :class:`Draft` is an auditable, human-controlled reply / communication
draft. Invariants (§13 "AI drafts, human sends", §41 no impersonation):

- **The drafts plane never sends.** The only status transition reachable
  through the drafts API is ``DRAFT -> DISCARDED``. ``SENT`` exists as a
  state but is *unreachable* from these endpoints — it can only be set by
  the relay send path, which is out of scope for this feature (G-3).
- **Every draft carries ``basis`` and ``evidence``** (§71/§72 auditability).
  ``basis`` says *why* the draft was produced; ``evidence`` holds
  communication / evidence *references*. Raw source content is never copied
  into either field — references only, no plaintext leakage.
- **Provenance is explicit.** ``model_used`` names what produced the text
  (``"manual"`` / ``"deterministic"`` / the model-assisted drafter's name)
  and ``action_history`` records every lifecycle step with its reason and,
  where applicable, the PDP policy id that decided an inference route.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from backend.communications.models import CommunicationChannel

# -- status machine (prompt2 §40/§41 — human-controlled sending) ---------------


class DraftStatus:
    """Draft lifecycle states.

    ``draft`` → ``discarded`` is the only transition the drafts API may
    perform. ``sent`` is intentionally present in the vocabulary (the relay
    send path, out of scope here, is the only thing that may reach it) but
    is unreachable through any drafts endpoint.
    """

    DRAFT = "draft"
    DISCARDED = "discarded"
    SENT = "sent"

    ALL = (DRAFT, DISCARDED, SENT)

    @classmethod
    def parse(cls, value: str) -> str:
        v = (value or "").lower().strip()
        if v not in cls.ALL:
            raise ValueError(f"unknown draft status {value!r}")
        return v


class DraftAction(BaseModel):
    """One entry in a draft's auditable action history.

    ``detail`` is a short, secret-free reason (policy reasons, drafter
    name, "created via api", ...). It must never carry draft text or raw
    communication content.
    """

    action: str = Field(
        ...,
        description=(
            "Lifecycle step: created | updated | routing_decided | "
            "policy_denied | model_assisted | model_assist_failed | "
            "fallback | discarded"
        ),
    )
    at: datetime
    detail: str | None = None
    policy_id: str | None = Field(
        default=None,
        description="PDP policy id that decided an inference route, if any.",
    )


class Draft(BaseModel):
    """One auditable draft (gaps1 G-1.4 — AC-R08).

    ``channel`` is the communication channel the draft is for;
    ``conversation_id`` / ``communication_ref`` point back into the
    R101 communications plane (refs only — the raw content is never
    stored here). ``text`` is the draft body itself (local-only,
    human-reviewed before any send).
    """

    draft_id: str
    channel: CommunicationChannel
    conversation_id: str | None = None
    communication_ref: str | None = None
    text: str
    basis: list[str] = Field(
        default_factory=list,
        description="Why this draft exists (explainability, §71).",
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Communication / evidence references backing the draft.",
    )
    model_used: str = Field(
        default="deterministic",
        description=(
            "What produced the text: 'manual', 'deterministic' (rule-based "
            "fallback), or the model-assisted drafter's name."
        ),
    )
    classification: str = Field(
        default="unclassified",
        description=(
            "Canonical sensitivity class of the draft's context "
            "('public' / 'internal' / 'confidential'), or 'unclassified' "
            "for drafts created without context."
        ),
    )
    status: str = Field(default=DraftStatus.DRAFT)
    source: str = Field(
        default="api",
        description="Where the draft came from: 'api', 'rule-based', 'model-assisted'.",
    )
    action_history: list[DraftAction] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    discarded_at: datetime | None = None

    @property
    def is_draft(self) -> bool:
        return self.status == DraftStatus.DRAFT

    @property
    def is_discarded(self) -> bool:
        return self.status == DraftStatus.DISCARDED


__all__ = ["Draft", "DraftAction", "DraftStatus"]
