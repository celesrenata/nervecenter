"""Spike 8.4: cross-channel trust adapter contract (research; contract/adapter only).

Question: can email security evidence be correlated with other channels
(iMessage, SMS, RCS, voicemail, calendar) **without** cross-contaminating
permissions, and without assuming any new connector?

Contract (R-017, R-023, R-024):

* **No new connectors.** The adapter only consumes the existing normalized
  :class:`~backend.communications.models.Communication` records, through a
  caller-supplied :class:`ChannelSource`. It never opens a device, relay,
  carrier or Apple API.
* **Explicit, per-channel, per-account grants.** A channel is read only if
  there is an unexpired :class:`ChannelGrant` for exactly that
  ``(account_scope, channel, purpose)`` *and* the existing PDP allows
  ``resource.read:security.correlation`` on that channel's resource. An email
  grant never implies SMS; a grant on account A never reads account B. An
  ungranted channel is not fetched at all and is reported only as
  ``NOT_EVALUATED`` (not even a count).
* **Metadata only.** The adapter reads person ref, channel, direction,
  timestamp and a hashed handle. It never resolves ``content_ref``.
* **No universal trust score.** Output is per-channel signals. Signals can
  only *add* review reasons; nothing here lowers risk or vouches for a
  sender on another channel.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from backend.communications.models import Communication, CommunicationChannel
from backend.policy.pdp import PolicyDecisionPoint
from schemas.nc_platform import NCPrincipal

PURPOSE = "SECURITY_CORRELATION"
READ_ACTION = "resource.read:security.correlation"
PRESSURE_WINDOW = timedelta(hours=24)


class Evaluated(StrEnum):
    EVALUATED = "EVALUATED"
    NOT_EVALUATED = "NOT_EVALUATED"


class CrossSignal(StrEnum):
    #: A known person appears from a handle never seen before on that channel.
    NEW_HANDLE_FOR_KNOWN_PERSON = "NEW_HANDLE_FOR_KNOWN_PERSON"
    #: Within 24h of a flagged email from a person, the same person reaches out
    #: on another channel from a new handle (classic "I also texted you").
    CROSS_CHANNEL_PRESSURE = "CROSS_CHANNEL_PRESSURE"


@dataclass(frozen=True)
class ChannelGrant:
    account_scope: str
    channel: CommunicationChannel
    purpose: str
    granted_by: str
    expires_at: datetime

    def covers(self, scope: str, channel: CommunicationChannel, now: datetime) -> bool:
        return (
            self.account_scope == scope
            and self.channel is channel
            and self.purpose == PURPOSE
            and now < self.expires_at
        )


class ChannelSource(Protocol):
    """Existing communications plane (caller-supplied; read-only)."""

    def list_metadata(
        self, account_scope: str, channel: CommunicationChannel, since: datetime
    ) -> Iterable[Communication]: ...


@dataclass(frozen=True)
class ChannelView:
    channel: CommunicationChannel
    status: Evaluated
    signals: tuple[CrossSignal, ...] = ()


@dataclass(frozen=True)
class CrossChannelReport:
    account_scope: str
    person_ref: str
    channels: tuple[ChannelView, ...]
    #: Deliberately absent: any combined or universal trust score.


def resource_for(channel: CommunicationChannel) -> str:
    """Frozen nc-grammar v1: namespace/provider/scope/kind/resource-id."""
    return f"nc://personal/nervecenter/{channel.value}/correlation/security"


def _handle(salt: bytes, value: str) -> str:
    return hmac.new(salt, value.strip().lower().encode(), hashlib.sha256).hexdigest()[:24]


@dataclass
class CrossChannelAdapter:
    pdp: PolicyDecisionPoint
    source: ChannelSource
    salt: bytes
    grants: list[ChannelGrant] = field(default_factory=list)

    def _allowed(
        self, principal: NCPrincipal, scope: str, channel: CommunicationChannel, now: datetime
    ) -> bool:
        if principal.kind != "human":
            return False
        if not any(g.covers(scope, channel, now) for g in self.grants):
            return False
        decision = self.pdp.evaluate(
            principal,
            READ_ACTION,
            resource_for(channel),
            {"purpose": PURPOSE, "channel": channel.value},
            now=now,
        )
        return decision.effect == "ALLOW"

    def report(
        self,
        principal: NCPrincipal,
        *,
        account_scope: str,
        person_ref: str,
        flagged_email_at: datetime | None,
        now: datetime,
        lookback: timedelta = timedelta(days=90),
    ) -> CrossChannelReport:
        views: list[ChannelView] = []
        for channel in CommunicationChannel:
            if channel is CommunicationChannel.EMAIL:
                continue  # the email side is the NerveShield verdict itself
            if not self._allowed(principal, account_scope, channel, now):
                views.append(ChannelView(channel, Evaluated.NOT_EVALUATED))
                continue
            rows = [
                c
                for c in self.source.list_metadata(account_scope, channel, now - lookback)
                if c.person_id == person_ref and c.channel is channel
            ]
            rows.sort(key=lambda c: c.timestamp)
            seen: set[str] = set()
            signals: list[CrossSignal] = []
            for c in rows:
                h = _handle(self.salt, c.sender)
                is_new = bool(seen) and h not in seen
                seen.add(h)
                if not is_new:
                    continue
                if CrossSignal.NEW_HANDLE_FOR_KNOWN_PERSON not in signals:
                    signals.append(CrossSignal.NEW_HANDLE_FOR_KNOWN_PERSON)
                if (
                    flagged_email_at is not None
                    and timedelta(0) <= c.timestamp - flagged_email_at <= PRESSURE_WINDOW
                    and CrossSignal.CROSS_CHANNEL_PRESSURE not in signals
                ):
                    signals.append(CrossSignal.CROSS_CHANNEL_PRESSURE)
            views.append(ChannelView(channel, Evaluated.EVALUATED, tuple(signals)))
        return CrossChannelReport(account_scope, person_ref, tuple(views))


__all__ = [
    "PRESSURE_WINDOW",
    "PURPOSE",
    "READ_ACTION",
    "ChannelGrant",
    "ChannelSource",
    "ChannelView",
    "CrossChannelAdapter",
    "CrossChannelReport",
    "CrossSignal",
    "Evaluated",
    "resource_for",
]
