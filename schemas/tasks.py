"""Task and TaskRun records (NerveCenter spec Section 10-16).

A *task* is a unit of work scheduled by the core (from a goal, decision, or
connector). A *task run* is a single execution attempt of a task, capturing
timing, status, and the produced :class:`AgentResult`.

Assumptions (spec not present in the repository):
    - ``Task.status`` reflects queue/execution lifecycle; ``TaskRun.status``
      reflects a single attempt outcome.
    - ``priority`` is 0 (lowest) .. 10 (highest), 0 default.
    - A task may schedule multiple runs (retries); ``TaskRun.attempt`` numbers
      them.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .base import Record

#: Lifecycle of a scheduled task.
TaskStatus = Literal[
    "draft",
    "pending",
    "scheduled",
    "in_progress",
    "completed",
    "failed",
    "cancelled",
]

#: Outcome of a single task execution attempt.
TaskRunStatus = Literal["running", "succeeded", "failed", "timed_out", "cancelled"]


class Task(Record):
    """A unit of work scheduled by the core."""

    title: str = Field(..., description="Short human-readable task title.")
    description: str | None = Field(
        default=None,
        description="Full description of the work to be performed.",
    )
    status: TaskStatus = Field(
        default="pending",
        description="Lifecycle state of the task.",
    )
    priority: int = Field(
        default=0,
        ge=0,
        le=10,
        description="Scheduling priority; 0 (lowest) to 10 (highest).",
    )
    goal_id: str | None = Field(
        default=None,
        description="Id of the :class:`Goal` this task contributes to, if any.",
    )
    decision_id: str | None = Field(
        default=None,
        description="Id of the :class:`Decision` that triggered this task, if any.",
    )
    assignee_agent_id: str | None = Field(
        default=None,
        description="Id of the agent/runtime that should execute this task.",
    )
    due_at: datetime | None = Field(
        default=None,
        description="Optional deadline for the task (UTC).",
    )
    scheduled_at: datetime | None = Field(
        default=None,
        description="When the task was placed on the queue (UTC).",
    )
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured inputs the executor will consume.",
    )
    parent_task_id: str | None = Field(
        default=None,
        description="Id of the parent task for hierarchical decomposition.",
    )
    dependencies: list[str] = Field(
        default_factory=list,
        description="Ids of tasks that must finish before this task may start (DAG edges).",
    )


class TaskRun(Record):
    """A single execution attempt of a :class:`Task`."""

    task_id: str = Field(..., description="Id of the task this run executes.")
    status: TaskRunStatus = Field(
        default="running",
        description="Outcome of this execution attempt.",
    )
    attempt: int = Field(
        default=1,
        ge=1,
        description="1-based attempt number for this task.",
    )
    started_at: datetime | None = Field(
        default=None,
        description="When execution began (UTC).",
    )
    finished_at: datetime | None = Field(
        default=None,
        description="When execution ended (UTC).",
    )
    agent_id: str | None = Field(
        default=None,
        description="Id of the agent/runtime that performed the run.",
    )
    model_id: str | None = Field(
        default=None,
        description="Id of the model used, when a model was involved.",
    )
    result_ref: str | None = Field(
        default=None,
        description="Id of the produced :class:`AgentResult`, if any.",
    )
    error: str | None = Field(
        default=None,
        description="Error message, when the attempt failed or timed out.",
    )
    logs_ref: str | None = Field(
        default=None,
        description="Reference to captured logs for this run.",
    )
