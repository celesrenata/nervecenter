"""Trusted acquisition provenance -> evidence fidelity (NS-ADR-024).

Evidence fidelity describes HOW the stored bytes were captured. That is a fact
about the ingest code path that stored the object, never a property of the
bytes themselves: an attacker controls every byte of a message body and can
make it begin with ``From:``/``Authentication-Results:`` lines. So:

- The trusted ingest seam sites record an :class:`AcquisitionMode` in the job
  ``meta`` (key :data:`ACQUISITION_META_KEY`). Everything the system stores
  today is decoded body text -> ``LEGACY_BODY_ONLY`` (Phase 0 evidence).
- The security worker derives fidelity from that recorded mode ONLY. A missing,
  unknown or unparseable mode fails safe to ``LEGACY_BODY_ONLY``/``BODY_ONLY``.
  Content bytes never promote fidelity (I-05, T-28).
- Raw-bytes fidelities (``ORIGINAL_RFC822`` and friends) are structurally
  UNREACHABLE today: :data:`RAW_CAPTURE_SEAM_ENABLED` is ``False`` and both the
  seam (on write) and :func:`effective_acquisition_mode` (on read) demote any
  raw mode to ``LEGACY_BODY_ONLY`` while it is. Only a future trusted raw
  capture seam (NS-ADR-002) may flip it, together with its own tests. This is
  deliberate, like the unreachable ``SAFE_TO_PROCESS`` (NS-ADR-013).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.mail_security.models import (
    AcquisitionMode,
    Code,
    EvidenceFidelity,
)

#: Job ``meta`` key carrying the trusted acquisition mode string.
ACQUISITION_META_KEY = "acquisition_mode"

#: Safe default for anything missing/unknown/garbled (fails safe, T-28).
SAFE_DEFAULT_MODE = AcquisitionMode.LEGACY_BODY_ONLY

#: Flipped to ``True`` only by a future trusted raw-capture seam (NS-ADR-002)
#: that really stores unmodified RFC822 bytes. Do not flip it for decoded text.
RAW_CAPTURE_SEAM_ENABLED = False

#: Modes that claim the stored object is raw message bytes / provider-full.
_RAW_BYTES_MODES = frozenset(
    {
        AcquisitionMode.GMAIL_FULL,
        AcquisitionMode.GMAIL_RAW,
        AcquisitionMode.IMAP_RFC822,
        AcquisitionMode.TAKEOUT_MBOX,
        AcquisitionMode.EML,
    }
)

_FIDELITY_BY_MODE: dict[AcquisitionMode, EvidenceFidelity] = {
    AcquisitionMode.LEGACY_BODY_ONLY: EvidenceFidelity.BODY_ONLY,
    AcquisitionMode.GMAIL_FULL: EvidenceFidelity.PROVIDER_FULL,
    AcquisitionMode.GMAIL_RAW: EvidenceFidelity.ORIGINAL_RFC822,
    AcquisitionMode.IMAP_RFC822: EvidenceFidelity.ORIGINAL_RFC822,
    AcquisitionMode.TAKEOUT_MBOX: EvidenceFidelity.ORIGINAL_RFC822,
    AcquisitionMode.EML: EvidenceFidelity.ORIGINAL_RFC822,
}

#: Evidence honestly missing from a body-only capture (R-002.3).
BODY_ONLY_MISSING_PARTS: tuple[Code, ...] = (
    "HEADERS_NOT_CAPTURED",
    "MIME_STRUCTURE_NOT_CAPTURED",
    "ATTACHMENTS_NOT_CAPTURED",
    "AUTHENTICATION_EVIDENCE_NOT_CAPTURED",
    "ENVELOPE_NOT_CAPTURED",
)


def coerce_acquisition_mode(value: object) -> AcquisitionMode:
    """Parse a recorded mode; anything unusable -> the safe default."""
    if isinstance(value, AcquisitionMode):
        return value
    if isinstance(value, str):
        try:
            return AcquisitionMode(value)
        except ValueError:
            return SAFE_DEFAULT_MODE
    return SAFE_DEFAULT_MODE


def trusted_mode_for_write(mode: object) -> AcquisitionMode:
    """Mode the seam may record: raw modes are demoted while no raw seam exists."""
    resolved = coerce_acquisition_mode(mode)
    if resolved in _RAW_BYTES_MODES and not RAW_CAPTURE_SEAM_ENABLED:
        return SAFE_DEFAULT_MODE
    return resolved


def effective_acquisition_mode(meta: Mapping[str, Any] | None) -> AcquisitionMode:
    """Acquisition mode for a job, from its ``meta`` ONLY (never from content)."""
    if not isinstance(meta, Mapping):
        return SAFE_DEFAULT_MODE
    return trusted_mode_for_write(meta.get(ACQUISITION_META_KEY))


def fidelity_for_mode(mode: AcquisitionMode) -> EvidenceFidelity:
    """Evidence fidelity implied by a (trusted) acquisition mode."""
    return _FIDELITY_BY_MODE.get(mode, EvidenceFidelity.BODY_ONLY)


def is_body_only(mode: AcquisitionMode) -> bool:
    """True when headers/auth in the parsed bytes must not be trusted."""
    return fidelity_for_mode(mode) is EvidenceFidelity.BODY_ONLY


def missing_parts_for_mode(mode: AcquisitionMode) -> list[Code]:
    """Evidence parts honestly absent for this capture (R-002.3)."""
    return list(BODY_ONLY_MISSING_PARTS) if is_body_only(mode) else []


__all__ = [
    "ACQUISITION_META_KEY",
    "BODY_ONLY_MISSING_PARTS",
    "RAW_CAPTURE_SEAM_ENABLED",
    "SAFE_DEFAULT_MODE",
    "coerce_acquisition_mode",
    "effective_acquisition_mode",
    "fidelity_for_mode",
    "is_body_only",
    "missing_parts_for_mode",
    "trusted_mode_for_write",
]
