"""Deterministic commitment / request / deadline detection (R103, AC-R04/R05/R06).

Public surface (see :mod:`backend.communications.detection.service` for the
orchestrator and :mod:`backend.communications.detection.extractors` for the
pure extractors):

- :func:`detect_communication` — pure, deterministic extraction over one
  normalized :class:`~backend.communications.models.Communication`;
  identical input → identical output.
- :class:`DetectionService` — the stateful orchestrator: runs the pure
  extractors, applies (default-disabled) :class:`InferenceAssist` refinement,
  emits §8 events, and feeds findings into the existing
  :class:`~backend.attention.service.AttentionService` through a compatible
  seam (no changes to attention's explainable ``_score``).

No-plaintext policy (§7 / R107): the extractors receive text through an
injectable resolver; the normalized ``Communication`` record itself carries
only a ``content_ref`` pointer, never the payload.
"""

from __future__ import annotations

from .dates import (
    DAY_END_BUSINESS_HOUR,
    NEXT_WEEK_OPEN_HOUR,
    TONIGHT_HOUR,
    parse_due_expression,
)
from .findings import (
    DETERMINISTIC_DETECTOR,
    Commitment,
    Deadline,
    DetectionReport,
    DueInfo,
    Finding,
    FindingStatus,
    Request,
)
from .assist import InferenceAssist
from .extractors import detect_communication
from .noise import NOISE_SUPPRESSIONS
from .service import DetectionService, get_detection_service, reset_detection_service

__all__ = [
    "DAY_END_BUSINESS_HOUR",
    "NEXT_WEEK_OPEN_HOUR",
    "TONIGHT_HOUR",
    "parse_due_expression",
    "DETERMINISTIC_DETECTOR",
    "DueInfo",
    "Finding",
    "FindingStatus",
    "Commitment",
    "Request",
    "Deadline",
    "DetectionReport",
    "InferenceAssist",
    "detect_communication",
    "NOISE_SUPPRESSIONS",
    "DetectionService",
    "get_detection_service",
    "reset_detection_service",
]
