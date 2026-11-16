"""Prompts and parsing for the researcher (NS-ADR-103).

The planner asks the combo for a short ordered list of research goals, each
step gets one bounded prompt, and the synthesizer turns the step results into
a report. Parsing is tolerant: JSON first, numbered lines as a fallback.
"""

from __future__ import annotations

import json
import re

MAX_STEPS = 12
MAX_GOAL_CHARS = 300
MAX_NOTES_CHARS = 6_000
EXCERPT_CHARS = 400
RESULT_CHARS = 8_000


def plan_prompt(title: str, question: str, budget_hours: float) -> str:
    return (
        "You are a long-term research planner. Break the research question into "
        f"at most {MAX_STEPS} ordered steps that can each be answered with one "
        f"focused piece of analysis. The whole project has about {budget_hours:g} hours.\n\n"
        f"Project: {title}\nQuestion: {question}\n\n"
        'Reply with only a JSON array of strings, one goal per step, e.g. ["goal one", "goal two"].'
    )


def parse_plan(text: str) -> list[str]:
    """Return step goals from the planner reply; empty when none can be found."""
    goals: list[str] = []
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
        except ValueError:
            data = None
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    item = item.get("goal") or item.get("title") or ""
                if isinstance(item, str) and item.strip():
                    goals.append(item.strip())
    if not goals:
        for line in text.splitlines():
            m = re.match(r"\s*(?:\d+[.)]|[-*])\s+(.+)", line)
            if m and m.group(1).strip():
                goals.append(m.group(1).strip())
    return [g[:MAX_GOAL_CHARS] for g in goals[:MAX_STEPS]]


def step_prompt(
    *,
    question: str,
    plan: list[str],
    ordinal: int,
    goal: str,
    notes: str,
    skills: str,
) -> str:
    outline = "\n".join(f"{i + 1}. {g}" for i, g in enumerate(plan))
    parts = [
        "You are carrying out one step of a long-running research project.",
        f"Research question: {question}",
        f"Plan:\n{outline}",
    ]
    if notes:
        parts.append(f"Working notes from earlier steps:\n{notes}")
    if skills:
        parts.append(skills)
    parts.append(
        f"Now do step {ordinal + 1}: {goal}\n"
        "Give findings, the reasoning behind them, and what remains uncertain."
    )
    return "\n\n".join(parts)


def append_notes(notes: str, ordinal: int, goal: str, result: str) -> str:
    """Working notes: one short excerpt per finished step, newest kept."""
    excerpt = " ".join(result.split())[:EXCERPT_CHARS]
    updated = f"{notes}\nStep {ordinal + 1} ({goal[:80]}): {excerpt}".strip()
    return updated[-MAX_NOTES_CHARS:]


def report_prompt(title: str, question: str, results: list[tuple[str, str]], partial: bool) -> str:
    body = "\n\n".join(f"Step {i + 1}: {g}\n{r[:3000]}" for i, (g, r) in enumerate(results))
    note = " Some planned steps were not run because the time budget ended." if partial else ""
    return (
        f"Write the final research report for the project '{title}'.{note}\n"
        f"Question: {question}\n\nStep results:\n{body}\n\n"
        "Produce a clear report with an answer, the evidence behind it, and open questions."
    )
