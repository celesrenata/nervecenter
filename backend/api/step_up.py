"""Step-up (recent independent sign-in) verification for high-risk approvals.

Task 7.6, R-019.3 / owner decision "recent sign-in" (NS-ADR-058): a
high-risk approval needs proof that the approver signed in within
:data:`~backend.mail_security.confirmations.STEP_UP_MAX_AGE_SECONDS`.

* **SSO (Keycloak, production).** An access token's ``iat`` is minted
  silently from an existing SSO session, so it proves nothing about when the
  user authenticated. The browser re-runs the OIDC flow with ``max_age`` and
  presents the fresh ``id_token``. It must verify against the issuer JWKS
  (signature, issuer, audience = this client), be an ``ID`` token, carry
  ``auth_time`` within the window (and not in the future), and match the
  caller's ``sid`` (else ``sub``). Its ``jti`` (else a token digest) is the
  single-use proof id.
* **Local break-glass.** NerveCenter-minted access tokens (``typ=access``)
  have no refresh path, so their ``auth_time`` (else ``iat``) is the
  password-entry time and the current token itself is the proof.

Anything else, or any doubt, is ``None`` (step-up required). Never raises.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.mail_security.confirmations import (
    STEP_UP_CLOCK_SKEW as _CLOCK_SKEW,
)
from backend.mail_security.confirmations import STEP_UP_MAX_AGE_SECONDS, StepUp


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=UTC)
    return None


def _fresh(auth_time: datetime | None, now: datetime) -> bool:
    if auth_time is None:
        return False
    if auth_time > now + _CLOCK_SKEW:
        return False
    return now - auth_time <= timedelta(seconds=STEP_UP_MAX_AGE_SECONDS)


def _same_principal(access: dict[str, Any], proof: dict[str, Any]) -> bool:
    for key in ("sid", "sub"):
        a, b = access.get(key), proof.get(key)
        if a and b:
            return a == b
    return False


async def verify_step_up(
    ctx: Any, access_claims: dict[str, Any], proof_token: str | None, *, now: datetime
) -> StepUp | None:
    if proof_token is None:
        if access_claims.get("typ") != "access":  # only local tokens self-prove
            return None
        auth_time = _as_dt(access_claims.get("auth_time") or access_claims.get("iat"))
        if not _fresh(auth_time, now):
            return None
        jti = str(access_claims.get("jti") or "")
        if not jti:
            return None
        return StepUp(auth_time=auth_time, proof_id=f"local:{jti}")  # type: ignore[arg-type]

    if not isinstance(proof_token, str) or len(proof_token) > 8192:
        return None
    try:
        proof = await ctx.verify_token(proof_token, require_session=False)
    except Exception:  # noqa: BLE001 - any verification failure means no step-up
        return None
    if proof.get("typ") != "ID":
        return None
    if not _same_principal(access_claims, proof):
        return None
    auth_time = _as_dt(proof.get("auth_time"))
    if not _fresh(auth_time, now):
        return None
    proof_id = str(proof.get("jti") or "") or hashlib.sha256(proof_token.encode()).hexdigest()[:32]
    return StepUp(auth_time=auth_time, proof_id=f"oidc:{proof_id}")  # type: ignore[arg-type]


__all__ = ["verify_step_up"]
