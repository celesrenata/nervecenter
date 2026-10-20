"""Drafter layer for the drafts plane (R105 — AC-R08, §40–§41).

Two drafters, one contract:

- :class:`DeterministicDrafter` — the honest rule-based fallback
  (``model_used == "deterministic"``). It always works, never calls any
  model, and is the *required* path: with model-assist off or denied the
  deterministic drafter still produces a draft, so the §21/§72
  deterministic + explainable contract holds.

- ``ModelAssistedDrafter`` — a protocol for an *injectable* model-assisted
  drafter. The drafts service calls the R107 inference policy
  (``PolicyDecisionPoint.infer_route``) **before** invoking it; the drafter
  is only invoked when the decision allows the tier it needs, and its
  content is only used on a local route. On any denial the service falls
  back to the deterministic drafter and records the denial in the draft's
  ``action_history`` with the PDP's reason and policy id.

The drafter interface receives only *references* and the draft text's
intended channel/classification — never the raw content of another
person's communication. (Reference-only inputs keep the plane free of
plaintext leakage by construction; a real implementation may resolve a
``communication_ref`` behind the same firewall.)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "DraftContext",
    "DeterministicDrafter",
    "ModelAssistedDrafter",
    "model_used_for",
]


@dataclass(frozen=True)
class DraftContext:
    """Context handed to a drafter.

    ``communication_ref`` / ``conversation_id`` are *references* into the
    communications plane (never raw content). ``inbound_excerpt`` is the
    draft's own target text for the deterministic fallback — e.g. the
    subject line or a short rule-safe echo — and may be ``None``.
    """

    channel: str
    conversation_id: str | None = None
    communication_ref: str | None = None
    inbound_excerpt: str | None = None
    classification: str = "unclassified"
    # Extra metadata a model-assisted drafter may use (refs only).
    extra: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ModelAssistedDrafter(Protocol):
    """Injectable model-assisted drafter (interface, not a v0 promise).

    A concrete implementation must be local-first: it is only ever
    invoked after the PDP has allowed the route it needs, and it must
    declare which inference tier it requires via ``required_tier``.
    """

    @property
    def name(self) -> str:
        """Drafter name recorded as ``model_used`` on the draft it produces."""
        ...

    @property
    def required_tier(self) -> str:
        """Model tier this drafter needs (``small-local`` / ``medium-local``
        / ``large-reasoning``)."""
        ...

    def draft(self, context: DraftContext) -> str:
        """Produce draft text for ``context``. Must never send anything."""
        ...


def model_used_for(drafter: ModelAssistedDrafter) -> str:
    """``model_used`` value recorded when ``drafter`` produced the text."""
    return drafter.name


# -- deterministic fallback ----------------------------------------------------


def _first_sentence(text: str, limit: int = 80) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    for sep in (". ", "! ", "? ", "\n"):
        idx = cleaned.find(sep)
        if idx > 0:
            cleaned = cleaned[: idx + 1].strip()
            break
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


class DeterministicDrafter:
    """Rule-based fallback drafter (``model_used == "deterministic"``).

    Mirrors the house style of ``EmailService.draft_reply``: a short,
    transparent, question-aware echo. Deterministic for identical input —
    no wall clock, no randomness, no model call.
    """

    name = "deterministic"

    @property
    def required_tier(self) -> str:
        """The deterministic drafter never routes through a model."""
        return "none"

    def draft(self, context: DraftContext) -> str:
        channel = (context.channel or "").lower()
        excerpt = (context.inbound_excerpt or "").strip()
        if excerpt:
            if excerpt.endswith("?"):
                return "Sure — let me check and get back to you."
            short = _first_sentence(excerpt)
            base = "Thanks, noted."
            if short:
                return f"{base} ({short}) I'll follow up soon."
            return f"{base} I'll follow up soon."
        if channel == "email":
            return "Thanks — noted. I'll get back to you with the details."
        if channel == "voicemail":
            return "Thanks for the voicemail — I'll call you back."
        return "Got it — I'll reply properly in a moment."
