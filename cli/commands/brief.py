"""`nerve brief` — composed briefing (prompt2 §30 / AC-R11).

Fetches one composed briefing (``GET /api/briefing``) with an optional
``kind`` (morning | midday | evening, default morning).
"""
from __future__ import annotations

import click

from .._helpers import command_options, run_command


@click.command("brief")
@click.option(
    "--kind",
    type=click.Choice(["morning", "midday", "evening"]),
    default=None,
    help="Briefing kind (default: morning).",
)
@command_options
@click.pass_context
def brief(
    ctx: click.Context,
    kind: str | None,
    api_url: str,
    as_json: bool,
    output_format: str,
) -> None:
    """Show one composed briefing (GET /api/briefing)."""
    params = {"kind": kind} if kind else None
    run_command(
        ctx,
        "GET",
        "/api/briefing",
        api_url=api_url,
        as_json=as_json,
        output_format=output_format,
        params=params,
    )
