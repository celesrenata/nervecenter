"""`nerve waiting` — waiting-on requests (prompt2 §19 / AC-R11).

Lists outgoing requests that went unanswered, most-waiting first
(``GET /api/waiting-on``). Optionally filtered by status.
"""
from __future__ import annotations

import click

from .._helpers import command_options, run_command


@click.command("waiting")
@click.option(
    "--status",
    type=click.Choice(["open", "resolved", "cancelled", "all"]),
    default=None,
    help="Filter by request status (default: open).",
)
@command_options
@click.pass_context
def waiting(
    ctx: click.Context,
    status: str | None,
    api_url: str,
    as_json: bool,
    output_format: str,
) -> None:
    """List waiting-on requests (GET /api/waiting-on)."""
    params = {"status": status} if status else None
    run_command(
        ctx,
        "GET",
        "/api/waiting-on",
        api_url=api_url,
        as_json=as_json,
        output_format=output_format,
        params=params,
    )
