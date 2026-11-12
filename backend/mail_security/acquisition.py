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
- Raw-bytes fidelities are reachable ONLY through an allow-listed trusted raw
  seam (:data:`RAW_SEAM_MODES`, NS-ADR-064): today just ``GMAIL_RAW``, written
  by the live mail worker when full capture is opted in, and only together
  with a well-formed ``raw_ref`` to the separately stored raw object. Both the
  seam (on write) and :func:`effective_acquisition_mode` (on read) demote every
  other raw mode (IMAP, Takeout mbox, EML, provider-full) and any raw mode
  without a raw ref to ``LEGACY_BODY_ONLY``.
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

#: Job ``meta`` key carrying the opaque ref of the separately stored raw
#: RFC822 object (NS-ADR-064). ``content_ref`` stays the decoded body ref so
#: the 7.1 safety-card and 7.4 gate joins keep working unchanged.
RAW_REF_META_KEY = "raw_ref"

#: The only ref prefix a trusted raw-capture seam writes raw objects under.
RAW_REF_PREFIX = "content://raw/"

#: Raw modes with a real trusted capture seam (NS-ADR-064). Only the Gmail
#: ``format=raw`` path in the live mail worker stores unmodified RFC822 bytes.
#: Every other raw mode (IMAP, Takeout mbox, EML, provider-full) stays demoted
#: until its own seam exists, with its own tests. Do not add a mode here for
#: decoded text.
RAW_SEAM_MODES: frozenset[AcquisitionMode] = frozenset({AcquisitionMode.GMAIL_RAW})

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


def is_raw_ref(value: object) -> bool:
    """True for a well-formed raw-object ref written by the trusted seam."""
    return (
        isinstance(value, str)
        and value.startswith(RAW_REF_PREFIX)
        and len(value) > len(RAW_REF_PREFIX)
        and ".." not in value
    )


def trusted_mode_for_write(mode: object, *, raw_ref: object = None) -> AcquisitionMode:
    """Mode the seam may record.

    A raw mode survives only when it has a trusted capture seam
    (:data:`RAW_SEAM_MODES`) AND the caller recorded a well-formed raw-object
    ref. Anything else is demoted to the safe default (fails safe, T-28).
    """
    resolved = coerce_acquisition_mode(mode)
    if resolved in _RAW_BYTES_MODES:
        if resolved not in RAW_SEAM_MODES or not is_raw_ref(raw_ref):
            return SAFE_DEFAULT_MODE
    return resolved


def effective_acquisition_mode(meta: Mapping[str, Any] | None) -> AcquisitionMode:
    """Acquisition mode for a job, from its ``meta`` ONLY (never from content)."""
    if not isinstance(meta, Mapping):
        return SAFE_DEFAULT_MODE
    return trusted_mode_for_write(
        meta.get(ACQUISITION_META_KEY), raw_ref=meta.get(RAW_REF_META_KEY)
    )


def scan_ref_for(meta: Mapping[str, Any] | None) -> str | None:
    """The ref the security worker should scan for this job.

    The raw object when the trusted mode is raw, else the body ref. Derived
    from the same trusted ``meta`` as the mode, so the two cannot disagree.
    """
    if not isinstance(meta, Mapping):
        return None
    body_ref = str(meta.get("content_ref") or "") or None
    mode = effective_acquisition_mode(meta)
    if mode in RAW_SEAM_MODES:
        return str(meta.get(RAW_REF_META_KEY))
    return body_ref


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
    "RAW_REF_META_KEY",
    "RAW_REF_PREFIX",
    "RAW_SEAM_MODES",
    "SAFE_DEFAULT_MODE",
    "coerce_acquisition_mode",
    "effective_acquisition_mode",
    "fidelity_for_mode",
    "is_raw_ref",
    "scan_ref_for",
    "is_body_only",
    "missing_parts_for_mode",
    "trusted_mode_for_write",
]
