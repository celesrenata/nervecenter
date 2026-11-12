"""Spike 8.4: cross-channel adapter contract — no permission cross-contamination."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
)
from backend.mail_security.research import cross_channel as xc
from backend.policy.pdp import PolicyDecisionPoint
from schemas.nc_platform import NCPolicy, NCPrincipal

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
A, B = "conn:aaaa", "conn:bbbb"
HUMAN = NCPrincipal(
    id="r", principal_id="prn_" + "1" * 26, kind="human", display_name="me", namespace="personal"
)
ALLOW = NCPolicy(
    id="row-allow",
    policy_id="pol_" + "C" * 26,
    effect="ALLOW",
    principal_match="kind:human,namespace:personal",
    action_match=xc.READ_ACTION,
    resource_match="nc://personal/nervecenter/*",
)
SMS, IMSG = CommunicationChannel.SMS, CommunicationChannel.IMESSAGE


def _msg(channel, sender, t, person="person:dana", cid=None):
    return Communication(
        communication_id=cid or f"{channel.value}:{sender}:{t.isoformat()}",
        person_id=person,
        channel=channel,
        direction=CommunicationDirection.INCOMING,
        sender=sender,
        timestamp=t,
        content_ref="enc://never-read",
    )


class SpySource:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def list_metadata(self, scope, channel, since):
        self.calls.append((scope, channel))
        return [
            m for s, m in self.rows if s == scope and m.channel is channel and m.timestamp >= since
        ]


def _grant(scope=A, channel=SMS, days=30):
    return xc.ChannelGrant(scope, channel, xc.PURPOSE, "owner", NOW + timedelta(days=days))


ROWS = [
    (A, _msg(SMS, "+15550001", NOW - timedelta(days=30))),
    (A, _msg(SMS, "+15550002", NOW - timedelta(hours=2))),  # new handle, after flagged email
    (A, _msg(IMSG, "dana@icloud.example", NOW - timedelta(days=5))),
    (B, _msg(SMS, "+15559999", NOW - timedelta(hours=1))),
]


def _adapter(grants, policies=(ALLOW,), rows=ROWS):
    src = SpySource(rows)
    return xc.CrossChannelAdapter(
        PolicyDecisionPoint(list(policies)), src, b"s" * 16, list(grants)
    ), src


def _views(report):
    return {v.channel: v for v in report.channels}


def test_ungranted_channels_are_never_fetched_and_report_nothing():
    adapter, src = _adapter([])
    rep = adapter.report(
        HUMAN,
        account_scope=A,
        person_ref="person:dana",
        flagged_email_at=NOW - timedelta(hours=3),
        now=NOW,
    )
    assert src.calls == []
    assert all(v.status is xc.Evaluated.NOT_EVALUATED and v.signals == () for v in rep.channels)


def test_sms_grant_reads_only_sms_for_that_account():
    adapter, src = _adapter([_grant()])
    rep = adapter.report(
        HUMAN,
        account_scope=A,
        person_ref="person:dana",
        flagged_email_at=NOW - timedelta(hours=3),
        now=NOW,
    )
    assert src.calls == [(A, SMS)]  # no iMessage, no account B
    v = _views(rep)
    assert v[SMS].status is xc.Evaluated.EVALUATED
    assert v[SMS].signals == (
        xc.CrossSignal.NEW_HANDLE_FOR_KNOWN_PERSON,
        xc.CrossSignal.CROSS_CHANNEL_PRESSURE,
    )
    assert v[IMSG].status is xc.Evaluated.NOT_EVALUATED


def test_grant_on_one_account_never_reads_another():
    adapter, src = _adapter([_grant(scope=A)])
    adapter.report(HUMAN, account_scope=B, person_ref="person:dana", flagged_email_at=None, now=NOW)
    assert src.calls == []


def test_expired_grant_or_missing_policy_denies():
    adapter, src = _adapter([_grant(days=-1)])
    adapter.report(HUMAN, account_scope=A, person_ref="person:dana", flagged_email_at=None, now=NOW)
    adapter2, src2 = _adapter([_grant()], policies=())  # PDP default deny
    adapter2.report(
        HUMAN, account_scope=A, person_ref="person:dana", flagged_email_at=None, now=NOW
    )
    assert src.calls == [] and src2.calls == []


@pytest.mark.parametrize("kind", ["agent", "workflow", "function", "connector", "device"])
def test_machine_principals_cannot_correlate(kind):
    adapter, src = _adapter([_grant()])
    adapter.report(
        HUMAN.model_copy(update={"kind": kind}),
        account_scope=A,
        person_ref="person:dana",
        flagged_email_at=None,
        now=NOW,
    )
    assert src.calls == []


def test_no_pressure_without_flagged_email_and_no_score_field():
    adapter, _ = _adapter([_grant()])
    rep = adapter.report(
        HUMAN, account_scope=A, person_ref="person:dana", flagged_email_at=None, now=NOW
    )
    assert _views(rep)[SMS].signals == (xc.CrossSignal.NEW_HANDLE_FOR_KNOWN_PERSON,)
    fields = set(xc.CrossChannelReport.__dataclass_fields__)
    assert not {f for f in fields if "score" in f or "trust" in f}
    # Signals only ever add review reasons.
    assert {s.value for s in xc.CrossSignal} == {
        "NEW_HANDLE_FOR_KNOWN_PERSON",
        "CROSS_CHANNEL_PRESSURE",
    }


def test_report_carries_no_handles_or_content_refs():
    adapter, _ = _adapter([_grant(), _grant(channel=IMSG)])
    rep = adapter.report(
        HUMAN,
        account_scope=A,
        person_ref="person:dana",
        flagged_email_at=NOW - timedelta(hours=3),
        now=NOW,
    )
    blob = repr(rep)
    for needle in ("+1555", "icloud", "enc://"):
        assert needle not in blob


def test_resources_are_valid_nc_uris():
    from schemas.nc_platform import validate_nc_uri

    for ch in CommunicationChannel:
        validate_nc_uri(xc.resource_for(ch))


def test_adapter_imports_no_connector_or_device_api():
    src = Path(xc.__file__).read_text()
    mods = {n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)}
    mods |= {a.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import) for a in n.names}
    forbidden = (
        "backend.connectors",
        "backend.ingest",
        "backend.messages",
        "backend.voicemail",
        "apple",
        "httpx",
        "requests",
        "socket",
        "subprocess",
    )
    assert not [m for m in mods if m and m.startswith(forbidden)]
    assert "content_ref" not in src.split('"""', 2)[2].replace("never resolves", "")
