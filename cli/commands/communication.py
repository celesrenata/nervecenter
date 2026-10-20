"""`nerve communication` — normalized communications (prompt2 §7 / AC-R11).

Subcommands:

- ``communication list``        GET /api/communications
- ``communication search <q>``  GET /api/communications/search?q=<query>
                                (cross-channel search, R110)

Bare ``nerve communication`` lists all communications.
"""
from __future__ import annotations

import click

from .._helpers import command_options, run_command


def _list_communications(
    ctx: click.Context,
    api_url: str,
    as_json: bool,
    output_format: str,
) -> None:
    run_command(
        ctx,
        "GET",
        "/api/communications",
        api_url=api_url,
        as_json=as_json,
        output_format=output_format,
    )


@click.group("communication", invoke_without_command=True)
@command_options
@click.pass_context
def communication(
    ctx: click.Context, api_url: str, as_json: bool, output_format: str
) -> None:
    """Communications — normalized cross-channel records (GET /api/communications)."""
    if ctx.invoked_subcommand is None:
        _list_communications(ctx, api_url, as_json, output_format)


@communication.command("list")
@command_options
@click.pass_context
def communication_list(
    ctx: click.Context, api_url: str, as_json: bool, output_format: str
) -> None:
    """List normalized communications (GET /api/communications)."""
    _list_communications(ctx, api_url, as_json, output_format)


@communication.command("search")
@click.argument("query")
@command_options
@click.pass_context
def communication_search(
    ctx: click.Context,
    query: str,
    api_url: str,
    as_json: bool,
    output_format: str,
) -> None:
    """Cross-channel search (GET /api/communications/search?q=<query>).

    QUERY is the free-text query for communications, people, and conversations
    across all channels.
    """
    run_command(
        ctx,
        "GET",
        "/api/communications/search",
        api_url=api_url,
        as_json=as_json,
        output_format=output_format,
        params={"q": query},
    )
