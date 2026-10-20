"""`nerve today` — Daily Operational State rollup (spec §52 / AC-R11, AC-R17).

Targets the canonical operational-state route ``GET /api/briefing/operational``
(the §52 rollup: open commitments, incoming requests, waiting-ons, upcoming
deadlines, pending approvals, today's calendar, active incidents, blocked
goals). The rollup is owned by the briefing plane (R109); when that backend
route is absent the command reports an API contract error (exit 2).
"""
from __future__ import annotations

import click

from .._helpers import command_options, run_command


@click.command("today")
@command_options
@click.pass_context
def today(
    ctx: click.Context, api_url: str, as_json: bool, output_format: str
) -> None:
    """Today's operational state (GET /api/briefing/operational).

    A read-only daily rollup across the personal planes.
    """
    run_command(
        ctx,
        "GET",
        "/api/briefing/operational",
        api_url=api_url,
        as_json=as_json,
        output_format=output_format,
    )
