"""Shared plumbing for CLI commands (spec §47).

Every command gets the standard options through :func:`command_options`:
``--api`` (base URL, default ``$NERVE_API`` or ``http://localhost:8000``),
``--json`` (raw API JSON body) and ``--output-format`` (text | table).

Exit codes:
    0    success
    127  API unreachable
    2    contract / usage error (bad URL, HTTP >= 400)
"""
from __future__ import annotations

from typing import Any, Callable

import click

from .client import ApiError, UnreachableError, resolve_base_url
from .client import request as http_request
from .output import OutputFormatter

EXIT_OK = 0
EXIT_UNREACHABLE = 127
EXIT_CONTRACT = 2


def command_options(func):
    """Attach the standard NerveCenter CLI options to a command or group."""
    func = click.option(
        "--api",
        "api_url",
        default=None,
        help="API base URL (default: $NERVE_API or http://localhost:8000)",
    )(func)
    func = click.option(
        "--json",
        "as_json",
        is_flag=True,
        default=False,
        help="Emit the raw API JSON body instead of the default rendering",
    )(func)
    func = click.option(
        "--output-format",
        type=click.Choice(["text", "table"]),
        default="table",
        help="Rendering style for non-JSON output",
    )(func)
    return func


def validate_api_url(url: str) -> None:
    """Fail with exit code 2 on a malformed base URL."""
    if "://" not in url or url.split("://", 1)[0] not in ("http", "https"):
        raise click.UsageError(f"invalid --api URL (expected http(s)://...): {url}")


def unwrap_envelope(payload):
    """Unwrap the §46 ListResponse envelope (``{"data": [...], "meta": {...}}``)."""
    if isinstance(payload, dict) and "data" in payload and "meta" in payload:
        return payload["data"]
    return payload


def _fail(ctx: click.Context, exc: Exception) -> None:
    if isinstance(exc, UnreachableError):
        click.echo(f"error: {exc}", err=True)
        ctx.exit(EXIT_UNREACHABLE)
    elif isinstance(exc, ApiError):
        click.echo(f"error: {exc}", err=True)
        ctx.exit(EXIT_CONTRACT)
    else:  # pragma: no cover - unexpected
        click.echo(f"error: unexpected failure: {exc}", err=True)
        ctx.exit(EXIT_CONTRACT)


def run_command(
    ctx: click.Context,
    method: str,
    path: str,
    *,
    api_url: str,
    as_json: bool,
    output_format: str,
    params: dict | None = None,
    json_body: dict | None = None,
    render: Callable[[Any, str], str] | None = None,
) -> None:
    """Execute one API call, render the result, and map failures to exit codes.

    ``render`` (optional) overrides the default :class:`OutputFormatter`
    rendering for non-JSON output — used by e.g. the ranked ``attention``
    table, which must show the ``why`` explainability column.
    """
    base_url = resolve_base_url(api_url)
    validate_api_url(base_url)
    try:
        response = http_request(method, base_url, path, params=params, json_body=json_body)
    except (UnreachableError, ApiError) as exc:
        _fail(ctx, exc)
        return
    if as_json:
        click.echo(OutputFormatter.format_json(response.data))
    else:
        data = unwrap_envelope(response.data)
        click.echo(
            render(data, output_format)
            if render is not None
            else OutputFormatter.render(data, output_format)
        )
    ctx.exit(EXIT_OK)
