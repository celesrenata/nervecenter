"""`nerve people [query]` — canonical persons (prompt2 §6 / AC-R11).

Lists canonical persons (``GET /api/people``); an optional ``query`` filters
client-side against the display name, person id, and identity values
(case-insensitive substring). The route returns the full set — the API has no
server-side query param — so the filter is applied locally to the fetched
records (no demo data, just projection of the real response).
"""
from __future__ import annotations

from typing import Any

import click

from .._helpers import command_options, run_command
from ..output import OutputFormatter


def _filter_people(data: Any, query: str) -> Any:
    """Client-side case-insensitive substring filter over canonical persons."""
    if not isinstance(data, list):
        return data
    needle = query.casefold()
    kept: list[Any] = []
    for person in data:
        if not isinstance(person, dict):
            continue
        haystacks = [
            str(person.get("display_name") or ""),
            str(person.get("person_id") or ""),
        ]
        for identity in person.get("identities") or []:
            if isinstance(identity, dict):
                haystacks.append(str(identity.get("value") or ""))
                haystacks.append(str(identity.get("raw_value") or ""))
        if any(needle in h.casefold() for h in haystacks):
            kept.append(person)
    return kept


def _render_people(data: Any, output_format: str, query: str) -> str:
    filtered = _filter_people(data, query) if query else data
    return OutputFormatter.render(filtered, output_format)


@click.command("people")
@click.argument("query", required=False, default="")
@command_options
@click.pass_context
def people(
    ctx: click.Context,
    query: str,
    api_url: str,
    as_json: bool,
    output_format: str,
) -> None:
    """List canonical persons, optionally filtered (GET /api/people).

    QUERY is a case-insensitive substring matched against the display name,
    person id, and identity values.
    """
    run_command(
        ctx,
        "GET",
        "/api/people",
        api_url=api_url,
        as_json=as_json,
        output_format=output_format,
        render=lambda data, fmt: _render_people(data, fmt, query),
    )
