"""`nerve commitments` — open commitments (spec §52 / AC-R11).

Targets the canonical commitments route ``GET /api/commitments`` — the
deterministic commitment findings ("what did I promise people?"). Commitment
detection is owned by the communications plane (R103); when that backend
route is absent the command reports an API contract error (exit 2).
"""
from __future__ import annotations

import click

from .._helpers import command_options, run_command


@click.command("commitments")
@command_options
@click.pass_context
def commitments(
    ctx: click.Context, api_url: str, as_json: bool, output_format: str
) -> None:
    """List open commitments (GET /api/commitments)."""
    run_command(
        ctx,
        "GET",
        "/api/commitments",
        api_url=api_url,
        as_json=as_json,
        output_format=output_format,
    )
