"""`nerve attention` — ranked attention queue (spec §65 / AC-R11).

Bare ``nerve attention`` lists the ranked queue (GET /api/attention); the
subcommands drive the lifecycle actions on one item:

- ``attention list``              GET /api/attention
- ``attention show <id>``         GET /api/attention/<id>
- ``attention resolve <id>``      POST /api/attention/<id>/resolve
- ``attention suppress <id>``     POST /api/attention/<id>/suppress
- ``attention acknowledge <id>``  POST /api/attention/<id>/acknowledge
- ``attention snooze <id> <when>`` POST /api/attention/<id>/snooze

The default (list) rendering shows a ranked table with the ``why``
explainability column (prompt2 §21/§22 — no opaque ranking).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import click

from .._helpers import command_options, run_command
from ..output import OutputFormatter, minimal_table

#: Relative keyword → day offset for ``snooze <when>`` (prompt2 §65 example:
#: ``nerve attention snooze <id> tomorrow``).
_RELATIVE_DAYS = {
    "tomorrow": 1,
    "next day": 1,
    "next week": 7,
    "next month": 30,
    "next year": 365,
}


def _parse_snooze_when(when: str) -> datetime:
    """Parse a snooze ``when`` into a timezone-aware local datetime.

    Accepts an ISO-8601 date or datetime (a bare date means local midnight)
    or a relative keyword (``tomorrow``, ``next day``, ``next week``,
    ``next month``, ``next year``). Naive values are interpreted in the
    *local* time zone so the API stores the intended wall-clock moment.
    """
    word = when.strip().lower()
    if word in _RELATIVE_DAYS:
        return (datetime.now().astimezone() + timedelta(days=_RELATIVE_DAYS[word])).astimezone()
    try:
        parsed = datetime.fromisoformat(when.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise click.UsageError(
            f"invalid snooze time {when!r}: expected an ISO-8601 date/datetime "
            f"(e.g. '2026-10-21' or '2026-10-21T09:00') or one of "
            f"{sorted(_RELATIVE_DAYS)}"
        ) from exc
    if parsed.tzinfo is None:
        # Naive → local wall-clock time (PEP 495).
        parsed = parsed.astimezone()
    return parsed


def _short_ts(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value)
    if "T" in text:
        return text.split("T", 1)[1].split("+", 1)[0].split("Z", 1)[0][:5]
    return text[:16].replace("T", " ")


def _render_attention(data: Any, output_format: str) -> str:
    """Ranked attention table with the ``why`` column (spec AC-R11)."""
    if not isinstance(data, list):
        return OutputFormatter.render(data, output_format)
    if not data:
        return "(empty)"
    headers = ["#", "priority", "status", "due", "reason", "why"]
    rows: list[list[Any]] = []
    for rank, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            rows.append([rank, "", "", "", str(item), ""])
            continue
        priority = item.get("priority")
        why = item.get("why") or []
        rows.append(
            [
                rank,
                f"{float(priority):.2f}"
                if isinstance(priority, (int, float))
                else str(priority or ""),
                item.get("status", ""),
                _short_ts(item.get("due")),
                item.get("reason", ""),
                "; ".join(str(w) for w in why),
            ]
        )
    return minimal_table((headers, rows))


def _list_attention(
    ctx: click.Context,
    api_url: str,
    as_json: bool,
    output_format: str,
) -> None:
    run_command(
        ctx,
        "GET",
        "/api/attention",
        api_url=api_url,
        as_json=as_json,
        output_format=output_format,
        render=_render_attention,
    )


@click.group("attention", invoke_without_command=True)
@command_options
@click.pass_context
def attention(
    ctx: click.Context, api_url: str, as_json: bool, output_format: str
) -> None:
    """Attention queue — ranked, explainable (GET /api/attention).

    Bare ``attention`` lists the queue; use a subcommand for one item.
    """
    if ctx.invoked_subcommand is None:
        _list_attention(ctx, api_url, as_json, output_format)


@attention.command("list")
@command_options
@click.pass_context
def attention_list(
    ctx: click.Context, api_url: str, as_json: bool, output_format: str
) -> None:
    """List the ranked attention queue (GET /api/attention)."""
    _list_attention(ctx, api_url, as_json, output_format)


@attention.command("show")
@click.argument("attention_id")
@command_options
@click.pass_context
def attention_show(
    ctx: click.Context,
    attention_id: str,
    api_url: str,
    as_json: bool,
    output_format: str,
) -> None:
    """Show one attention item (GET /api/attention/<id>)."""
    run_command(
        ctx,
        "GET",
        f"/api/attention/{attention_id}",
        api_url=api_url,
        as_json=as_json,
        output_format=output_format,
    )


@attention.command("resolve")
@click.argument("attention_id")
@command_options
@click.pass_context
def attention_resolve(
    ctx: click.Context,
    attention_id: str,
    api_url: str,
    as_json: bool,
    output_format: str,
) -> None:
    """Resolve an item (POST /api/attention/<id>/resolve)."""
    run_command(
        ctx,
        "POST",
        f"/api/attention/{attention_id}/resolve",
        api_url=api_url,
        as_json=as_json,
        output_format=output_format,
        json_body={},
    )


@attention.command("suppress")
@click.argument("attention_id")
@command_options
@click.pass_context
def attention_suppress(
    ctx: click.Context,
    attention_id: str,
    api_url: str,
    as_json: bool,
    output_format: str,
) -> None:
    """Suppress (mute) an item (POST /api/attention/<id>/suppress)."""
    run_command(
        ctx,
        "POST",
        f"/api/attention/{attention_id}/suppress",
        api_url=api_url,
        as_json=as_json,
        output_format=output_format,
        json_body={},
    )


@attention.command("acknowledge")
@click.argument("attention_id")
@command_options
@click.pass_context
def attention_acknowledge(
    ctx: click.Context,
    attention_id: str,
    api_url: str,
    as_json: bool,
    output_format: str,
) -> None:
    """Acknowledge an item as handled (POST /api/attention/<id>/acknowledge)."""
    run_command(
        ctx,
        "POST",
        f"/api/attention/{attention_id}/acknowledge",
        api_url=api_url,
        as_json=as_json,
        output_format=output_format,
        json_body={},
    )


@attention.command("snooze")
@click.argument("attention_id")
@click.argument("when")
@command_options
@click.pass_context
def attention_snooze(
    ctx: click.Context,
    attention_id: str,
    when: str,
    api_url: str,
    as_json: bool,
    output_format: str,
) -> None:
    """Snooze an item to a later window (POST /api/attention/<id>/snooze).

    WHEN is an ISO-8601 date/datetime (naive = local time) or a relative
    keyword: tomorrow, next day, next week, next month, next year.
    """
    until = _parse_snooze_when(when)
    run_command(
        ctx,
        "POST",
        f"/api/attention/{attention_id}/snooze",
        api_url=api_url,
        as_json=as_json,
        output_format=output_format,
        json_body={"until": until.isoformat()},
    )
