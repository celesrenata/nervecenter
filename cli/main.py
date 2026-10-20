"""NerveCenter CLI entrypoint (spec §47).

Run with ``python -m cli.main`` (or ``python -m cli``).

Exit codes: 0 ok · 2 contract/usage error · 127 API unreachable.
"""
import click

from .commands import (
    agents,
    ask,
    attention,
    brief,
    changes,
    communication,
    commitments,
    compute,
    entity,
    goal,
    incident,
    mutations,
    pause,
    people,
    routes,
    status,
    task,
    today,
    waiting,
)


@click.group(help="NerveCenter Command Line Interface (spec §47).")
@click.version_option("0.1.0", "--version", prog_name="nervcenter-cli")
def cli() -> None:
    """NerveCenter Command Line Interface (spec §47).

    All commands support ``--json`` for the raw API body and ``--api``
    (or the ``NERVE_API`` environment variable) to target a specific server.
    """


ALL_COMMANDS = [
    status.status,
    ask.ask,
    entity.entity,
    changes.changes,
    goal.goal,
    task.task,
    agents.agents,
    compute.compute,
    routes.routes,
    incident.incident,
    pause.pause_agents,
    mutations.stop_mutations,
    # §65 personal surfaces (AC-R11).
    attention.attention,
    today.today,
    commitments.commitments,
    waiting.waiting,
    brief.brief,
    people.people,
    communication.communication,
]

for _command in ALL_COMMANDS:
    cli.add_command(_command)


if __name__ == "__main__":
    cli()
