"""Deterministic sensitivity classification and inference-routing policy.

R107 (``specs/personal-reasoning-policy-closure`` — AC-R13 / AC-R14,
prompt2 §36–§39). This module owns the *sensitivity side* of the personal
policy foundation:

- :class:`SensitivityClass` — the canonical routing classes
  (``public`` / ``internal`` / ``confidential``).
- :class:`ModelTier` — the inference tiers an ``INFER_ROUTE`` decision may
  select (``small-local`` / ``medium-local`` / ``large-reasoning``).
- :class:`RoutingDecision` — where content may be processed
  (``LOCAL_ONLY`` vs ``REMOTE_ALLOWED``).
- :func:`classify_sensitivity` — the deterministic ingest classifier. It maps
  raw communication content to the §36 label set (read from
  ``backend.communications.models.SensitivityLabel`` — the label *vocabulary*
  is owned by R101; we only read it) and derives the canonical
  :class:`SensitivityClass`. An explicit override (from the communications
  API) is honored verbatim.

Confidential content routes ``LOCAL_ONLY`` (§36: raw content defaults to
``LOCAL_ONLY``). No policy may lift it to a remote tier — that invariant is
enforced by :meth:`backend.policy.pdp.PolicyDecisionPoint.infer_route`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.communications.models import SensitivityLabel


class SensitivityClass(StrEnum):
    """Canonical sensitivity / routing class (§36–§38).

    ``confidential`` is the only class that is unconditionally ``LOCAL_ONLY``.
    ``public`` and ``internal`` are *remote-capable* but still gated by the
    PDP default-deny (a remote-allowed result always requires an explicit
    matching policy).
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"


class ModelTier(StrEnum):
    """Inference tiers selectable by an ``INFER_ROUTE`` decision (§37–§39).

    ``small-local`` / ``medium-local`` run on-device; ``large-reasoning`` is
    the remote tier and is only ever selected when ``remote_allowed`` is true.
    """

    SMALL_LOCAL = "small-local"
    MEDIUM_LOCAL = "medium-local"
    LARGE_REASONING = "large-reasoning"


class RoutingDecision(StrEnum):
    """Where content may be processed (§36: raw content defaults
    ``LOCAL_ONLY``)."""

    LOCAL_ONLY = "LOCAL_ONLY"
    REMOTE_ALLOWED = "REMOTE_ALLOWED"


#: §36 label set → canonical routing class. The label vocabulary is owned by
#: R101 (``SensitivityLabel``); this mapping is a read-only projection.
LABEL_TO_CLASS: dict[str, SensitivityClass] = {
    SensitivityLabel.LOW_SENSITIVITY.value: SensitivityClass.PUBLIC,
    SensitivityLabel.WORK.value: SensitivityClass.INTERNAL,
    SensitivityLabel.PROJECT.value: SensitivityClass.INTERNAL,
    SensitivityLabel.PERSONAL.value: SensitivityClass.CONFIDENTIAL,
    SensitivityLabel.PRIVATE.value: SensitivityClass.CONFIDENTIAL,
    SensitivityLabel.FINANCIAL.value: SensitivityClass.CONFIDENTIAL,
}

#: Deterministic ingest keyword rules, checked in priority order (financial
#: first, then personal). The first matching rule wins; with no match the
#: classifier falls back to the ingest-supplied label or the §36 default.
KEYWORD_RULES: tuple[tuple[SensitivityLabel, tuple[str, ...]], ...] = (
    (
        SensitivityLabel.FINANCIAL,
        (
            "invoice", "payment", "balance", "bank", "salary", "credit card",
            "credit-card", "account number", "ssn", "social security",
            "transfer", "refund", "billing", "payroll", "wire ",
        ),
    ),
    (
        SensitivityLabel.PERSONAL,
        (
            "medication", "prescription", "doctor", "clinic", "therapy",
            "diagnosis", "hospital", "mental health", "anxiety", "depression",
            "divorce", "custody",
        ),
    ),
)

#: Representative §36 label per canonical class, used to label an explicit
#: override (the least-surprising label that maps to the class).
_REPRESENTATIVE_LABEL: dict[SensitivityClass, SensitivityLabel] = {
    SensitivityClass.PUBLIC: SensitivityLabel.LOW_SENSITIVITY,
    SensitivityClass.INTERNAL: SensitivityLabel.WORK,
    SensitivityClass.CONFIDENTIAL: SensitivityLabel.PERSONAL,
}


@dataclass(frozen=True)
class SensitivityClassification:
    """Outcome of :func:`classify_sensitivity`.

    ``label`` is the §36 label (e.g. ``"FINANCIAL"``); ``sensitivity_class``
    is the derived routing class; ``routing`` is the resulting
    ``LOCAL_ONLY`` / ``REMOTE_ALLOWED`` disposition; ``overridden`` is true
    only when an explicit override was applied.
    """

    label: str
    sensitivity_class: SensitivityClass
    routing: RoutingDecision
    reason: str
    overridden: bool = False


# -- internal helpers --------------------------------------------------------


def _as_label_enum(label: SensitivityLabel | str) -> SensitivityLabel:
    """Coerce a §36 label (enum or its string value) to the enum, or fail."""
    if isinstance(label, SensitivityLabel):
        return label
    if isinstance(label, str):
        for member in SensitivityLabel:
            if member.value == label:
                return member
    raise ValueError(f"unknown §36 sensitivity label: {label!r}")


def _routing_for(class_: SensitivityClass) -> RoutingDecision:
    return (
        RoutingDecision.LOCAL_ONLY
        if class_ is SensitivityClass.CONFIDENTIAL
        else RoutingDecision.REMOTE_ALLOWED
    )


# -- public classifier -------------------------------------------------------


def classify_sensitivity(
    content: str,
    label: SensitivityLabel | str | None = None,
    *,
    override: SensitivityClass | None = None,
) -> SensitivityClassification:
    """Deterministically classify the sensitivity of communication content.

    Precedence (highest first):

    1. **explicit override** — a :class:`SensitivityClass` supplied by the
       communications API. Honored verbatim; the deterministic keyword rules
       are bypassed. (AC-R13: "overridable via the communications API".)
    2. **deterministic keyword rules** — financial keywords → ``FINANCIAL``,
       personal/health keywords → ``PERSONAL``. (AC-R13: "set at ingest by the
       deterministic classifier".)
    3. **ingest-supplied label** — the §36 ``label`` already attached at
       ingest, if any.
    4. **§36 default** — ``LOW-SENSITIVITY`` (public) when nothing else.

    Returns a :class:`SensitivityClassification`; ``confidential`` classes
    always carry the ``LOCAL_ONLY`` routing decision.
    """
    if override is not None:
        if not isinstance(override, SensitivityClass):
            raise TypeError(
                f"override must be a SensitivityClass, got {type(override).__name__}"
            )
        return SensitivityClassification(
            label=_REPRESENTATIVE_LABEL[override].value,
            sensitivity_class=override,
            routing=_routing_for(override),
            reason="explicit override of deterministic classification",
            overridden=True,
        )

    lowered = (content or "").lower()
    for target_label, keywords in KEYWORD_RULES:
        if any(kw in lowered for kw in keywords):
            return SensitivityClassification(
                label=target_label.value,
                sensitivity_class=LABEL_TO_CLASS[target_label.value],
                routing=_routing_for(LABEL_TO_CLASS[target_label.value]),
                reason=f"deterministic keyword rule -> {target_label.value}",
                overridden=False,
            )

    if label is not None:
        chosen = _as_label_enum(label)
        reason = f"ingest label {chosen.value}"
    else:
        chosen = SensitivityLabel.LOW_SENSITIVITY
        reason = "default §36 label (LOW-SENSITIVITY)"

    class_ = LABEL_TO_CLASS[chosen.value]
    return SensitivityClassification(
        label=chosen.value,
        sensitivity_class=class_,
        routing=_routing_for(class_),
        reason=reason,
        overridden=False,
    )
