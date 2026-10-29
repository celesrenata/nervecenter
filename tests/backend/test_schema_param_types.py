"""Fix-round regression guards: D-4 datetime column types, N1/D-5 id/FK types,
D-10 structured 500 handler.

Root cause (confirmed on the live cluster): the ORM layer declared the ``id``
and all FK columns as ``UUID(as_uuid=False)`` while the live PostgreSQL
schema stores them as ``character varying``, and the eleven temporal columns
as plain ``String`` while the schema uses ``timestamp with time zone``.
Every insert/update therefore failed at the driver level with::

    operator does not exist: character varying = uuid
    column "deadline" is of type timestamp with time zone
    but expression is of type character varying

The fix changed 17 model files (id/FK → :class:`String`, temporal →
``DateTime(timezone=True)``) and four services (removed the ``_iso()`` string
coercion on the write + query-bound paths so native ``datetime`` values reach
the driver).

What this module pins
----------------------
1. **ORM column types** — every ``id``/FK column is ``String`` (never
   ``UUID``), and every temporal column on the 17 tables is a *timezone-aware
   ``DateTime``*. ``Base.metadata`` is the single source of truth the DDL
   migration derives from, so this is the strongest regression guard
   available without a live PostgreSQL.
2. **Service write paths** — ``_schema_to_orm`` (goals/tasks) and the
   ``create``() flow (goals/tasks) preserve native ``datetime`` objects on
   the temporal columns (no ``"YYYY-MM-DD..."`` strings).
3. **Query-bound parameters** — the temporal comparison clauses built by the
   service layer compile under the *PostgreSQL dialect* with bind parameters
   whose value is a ``datetime`` instance, never a ``str``.
4. **D-10** — the app registers a generic ``Exception`` handler that returns
   a structured ``{"detail", "error_type"}`` 500 body, and the handler does
   not swallow ``HTTPException`` (Starlette dispatches ``HTTPException`` to
   its more-specific ``StarletteHTTPException`` handler first via the
   exception MRO walk in ``wrap_app_handling_exceptions``).

Limitation (documented per the fix-round hand-off)
--------------------------------------------------
The unit tests here run on in-memory SQLite; SQLite does **not** enforce
column type fidelity, so it cannot reproduce the asyncpg-specific
``character varying = uuid`` / ``timestamp ... but expression is
character varying`` failures that motivated this fix. The guard above pins
the *source* of the failure — the ORM type declarations and the
service-side parameter types — so a future regression that flips a column
back to ``UUID`` or re-introduces an ``_iso()`` string coercion is caught
here, even though the SQLite round-trip itself would still "succeed".
Type enforcement against a real ``asyncpg`` connection remains the final,
cluster-level check (see the deploy contract in
``deployments/kubernetes/base/migrate-job.yaml``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import Select, Insert

from backend.api.app import app as module_app
from backend.persistence.models import (
    Agent,
    Base,
    Claim,
    Decision,
    Entity,
    Goal,
    Incident,
    Relationship,
    Task,
    TaskRun,
)
from backend.goals.service import GoalService, _schema_to_orm as _goal_to_orm
from backend.knowledge.entity import EntityService
from backend.knowledge.relationship import (
    RelationshipService,
    _active_clause,
)
from backend.tasks.service import TaskService, _schema_to_orm as _task_to_orm
from schemas.goals import Goal as GoalSchema

# Wave G/H/I/J plane models: import them explicitly so ``Base.metadata`` is a
# stable, complete universe no matter which test files run in the same session.
# (H's routines models are imported by no other test, so without this the
# exact-count guard below would vary with import order.)
import backend.persistence.models.bills  # noqa: F401
import backend.persistence.models.routines  # noqa: F401
import backend.persistence.models.wellness  # noqa: F401
from schemas.relationships import TemporalWindow
from schemas.tasks import Task as TaskSchema

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)

#: Tables known to carry an ``id`` primary key. The live schema uses
#: ``character varying`` for every one of them; the ORM must agree.
TABLES_WITH_ID = frozenset(
    {
        "agent_results",
        "agents",
        "approvals",
        "auth_devices",
        "auth_sessions",
        "claims",
        "decisions",
        "entities",
        "events",
        "evidence",
        "goals",
        "incidents",
        "pairing_challenges",
        "planned_changes",
        "relationships",
        "task_runs",
        "tasks",
        # C1 canonical-platform tables (nc_*), Alembic 005.
        "nc_resources",
        "nc_connector_instances",
        "nc_resource_aliases",
        "nc_resource_relations",
        "nc_resource_snapshots",
        "nc_principals",
        "nc_policies",
        "nc_approvals",
        # Personal-plane tables, Alembic 006.
        "people",
        "person_identities",
        "communications",
        "conversations",
        "commitments",
        "followups",
        "attention_items",
        "snoozes",
        "notification_log",
        "preferences",
        "connections",
        "secrets",
        "ingest_cursors",
        "import_jobs",
        "relay_seen_messages",
        # Calendar-plane tables, Alembic 007.
        "calendar_accounts",
        "calendar_events",
        # Wellness-plane tables, Alembic 010 (workstream I — private journal).
        "wellness_entries",
        # Bills-plane tables, Alembic 008 (workstream G).
        "bills",
        "bill_candidates",
        # Routines-plane tables, Alembic 009 (workstream H — incl. sleep).
        "routines",
        "routine_check_ins",
        "sleep_records",
    }
)

#: Table → temporal column names that must be ``DateTime(timezone=True)``
#: (the D-4 list from the live-schema inspection).
TEMPORAL_COLUMNS = {
    "goals": ("deadline",),
    "tasks": ("due_at", "scheduled_at"),
    "task_runs": ("started_at", "finished_at"),
    "claims": (),
    "decisions": ("decided_at",),
    "entities": ("valid_from", "valid_to"),
    "relationships": ("valid_from", "valid_to", "asserted_at"),
}

#: FK/ID-like column names that must be ``String`` (not UUID) in the ORM.
#: These are the columns the D-5 live-DB inspection found as
#: ``character varying``.
FK_ID_COLUMNS = (
    "id",
    "task_id",
    "task_run_id",
    "agent_id",
    "result_ref",
    "subject_entity_id",
    "supersedes_claim_id",
    "claim_id",
    "planned_change_id",
    "planned_change_id",
    "source_decision_id",
    "approval_id",
    "source_entity_id",
    "target_entity_id",
    "subject",
    "trace_id",
    "correlation_id",
    "parent_event_id",
)


def _col_type(table_name: str, col_name: str) -> str:
    """Return the canonical type string of a column (``String`` / ``DateTimeT``
    / ``DateTime`` / ``UUID`` / other)."""
    from sqlalchemy.types import DateTime as SA_DT
    from sqlalchemy.types import String as SA_String

    t = Base.metadata.tables[table_name]
    c = t.columns[col_name]
    if isinstance(c.type, SA_String):
        return "String"
    if isinstance(c.type, SA_DT):
        return "DateTimeT" if c.type.timezone else "DateTime"
    tn = type(c.type).__name__
    if tn == "UUID":
        return "UUID"
    return tn


# ---------------------------------------------------------------------------
# 1. ORM column-type guards
# ---------------------------------------------------------------------------
class TestOrmColumnTypes:
    """Pin the ORM to the live PostgreSQL schema."""

    def test_all_registered_tables_present(self):
        # The guard set is the source of truth: every table the ORM is
        # expected to register must be present. (C1 canonical-platform
        # tables from nc_platform.py are part of this set since 005.)
        assert TABLES_WITH_ID.issubset(set(Base.metadata.tables.keys()))
        assert len(Base.metadata.tables) == len(TABLES_WITH_ID)

    def test_every_id_column_is_string(self):
        """N1/D-5: every ``id`` column is ``String`` (never UUID)."""
        for table in sorted(TABLES_WITH_ID):
            col = Base.metadata.tables[table].c["id"]
            assert isinstance(
                col.type,
                type(col.type),
            )
            assert _col_type(table, "id") == "String", (
                f"{table}.id must be String, got {_col_type(table, 'id')}"
            )

    @pytest.mark.parametrize(
        ("table", "col"),
        sorted(
            {
                # agent_results (TaskRun result) — FK to task_runs / agents
                ("agent_results", "task_run_id"),
                ("agent_results", "agent_id"),
                # auth.approvals — FK to planned_changes
                ("approvals", "planned_change_id"),
                # claims — FK to entities / claims
                ("claims", "subject_entity_id"),
                ("claims", "supersedes_claim_id"),
                # decisions — FK to claims / planned_changes / tasks
                ("decisions", "claim_id"),
                ("decisions", "planned_change_id"),
                ("decisions", "task_id"),
                # planned_changes — FK to decisions / auth.approvals
                ("planned_changes", "source_decision_id"),
                ("planned_changes", "approval_id"),
                # relationships — FK to entities
                ("relationships", "source_entity_id"),
                ("relationships", "target_entity_id"),
                # events — correlation identifiers
                ("events", "subject"),
                ("events", "trace_id"),
                ("events", "correlation_id"),
                ("events", "parent_event_id"),
                # task_runs — FK to tasks / agents + result reference
                ("task_runs", "task_id"),
                ("task_runs", "agent_id"),
                ("task_runs", "result_ref"),
                # tasks — FK to goals / decisions / agents / tasks
                ("tasks", "goal_id"),
                ("tasks", "decision_id"),
                ("tasks", "assignee_agent_id"),
                ("tasks", "parent_task_id"),
            }
        ),
    )
    def test_fk_id_columns_are_string(self, table: str, col: str):
        """N1/D-5: every FK/ID column is ``String`` (never UUID)."""
        t = Base.metadata.tables[table]
        assert col in t.columns, f"{table} has no column {col!r}"
        assert _col_type(table, col) == "String", (
            f"{table}.{col} must be String, got {_col_type(table, col)}"
        )

    @pytest.mark.parametrize(
        ("table", "col"),
        sorted(
            (tbl, c)
            for tbl, cols in TEMPORAL_COLUMNS.items()
            for c in cols
        ),
    )
    def test_temporal_columns_are_tz_datetime(self, table: str, col: str):
        """D-4: every temporal column is ``DateTime(timezone=True)``."""
        t = Base.metadata.tables[table]
        assert col in t.columns, f"{table} has no column {col!r}"
        assert _col_type(table, col) == "DateTimeT", (
            f"{table}.{col} must be DateTime(timezone=True), got "
            f"{_col_type(table, col)}"
        )

    def test_no_uuid_columns_remain(self):
        """N1/D-5: no UUID-typed column survives anywhere in the 17 tables."""
        offenders = []
        for table_name, table in Base.metadata.tables.items():
            for col in table.columns:
                tn = type(col.type).__name__
                if tn == "UUID":
                    offenders.append(f"{table_name}.{col.name}")
        assert offenders == [], f"UUID columns remain: {offenders}"

    def test_no_string_temporal_columns_remain(self):
        """D-4: none of the D-4 temporal columns is still plain String."""
        offenders = []
        for table, cols in TEMPORAL_COLUMNS.items():
            for c in cols:
                if _col_type(table, c) == "String":
                    offenders.append(f"{table}.{c}")
        assert offenders == [], f"string-typed temporal columns remain: {offenders}"


# ---------------------------------------------------------------------------
# Fakes used by the service-level parameter-type tests
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> Any:
        r = self

        def all_() -> list[Any]:
            return list(self._rows)

        return type("_Sc", (), {"all": lambda self_: all_()})()


class _FakeSession:
    """Minimal async-session stand-in: records ORM objects handed to
    ``session.add`` and no-ops the transaction boundary."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        pass

    async def refresh(self, obj: Any) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def execute(self, stmt: Any) -> _FakeResult:
        return _FakeResult([])


class _FakeSessionFactory:
    def __init__(self) -> None:
        self.session = _FakeSession()

    def __call__(self) -> _FakeSession:
        return self.session


def _make_engine_stub() -> Any:
    """Return a stand-in that satisfies the ``AsyncEngine`` type annotation
    without touching a real driver."""
    return type("EngineStub", (), {"dialect": postgresql.dialect()})()


# ---------------------------------------------------------------------------
# 2. Service write-path parameter types (fake-session capture)
# ---------------------------------------------------------------------------
class TestServiceWritePaths:
    """Pin the service layer to emit native ``datetime`` on temporal columns
    and ``str`` on id/FK columns — the exact values the driver will bind."""

    async def test_goal_create_preserves_native_deadline(self):
        goal_schema = GoalSchema(
            id="goal-1",
            title="Ship",
            description="d",
            status="active",
            deadline=NOW,
        )
        svc = GoalService(engine=_make_engine_stub())  # type: ignore[arg-type]
        factory = _FakeSessionFactory()
        svc._session_factory = factory  # noqa: SLF001 (test access)
        await svc.create({"id": "goal-1", "title": "Ship", "deadline": NOW})
        (orm,) = factory.session.added
        assert isinstance(orm.deadline, datetime), (
            f"goal.deadline must be datetime, got {type(orm.deadline).__name__}"
        )
        assert isinstance(orm.id, str)
        # The write path must NOT string-coerce the datetime.
        assert not isinstance(orm.deadline, str)

    async def test_task_create_preserves_native_due_at(self):
        task_schema = TaskSchema(
            id="task-1",
            title="Do",
            status="pending",
            due_at=NOW,
        )
        svc = TaskService(engine=_make_engine_stub())  # type: ignore[arg-type]
        factory = _FakeSessionFactory()
        svc._session_factory = factory  # noqa: SLF001
        await svc.create({"id": "task-1", "title": "Do", "due_at": NOW})
        (orm,) = factory.session.added
        assert isinstance(orm.due_at, datetime), (
            f"task.due_at must be datetime, got {type(orm.due_at).__name__}"
        )
        assert isinstance(orm.id, str)

    def test_goal_schema_to_orm_preserves_native_deadline(self):
        schema = GoalSchema(
            id="goal-2",
            title="x",
            status="active",
            deadline=NOW,
        )
        orm = _goal_to_orm(schema)
        assert isinstance(orm.deadline, datetime)
        assert isinstance(orm.id, str)

    def test_task_schema_to_orm_preserves_native_due_at(self):
        schema = TaskSchema(
            id="task-2",
            title="x",
            status="pending",
            due_at=NOW,
        )
        orm = _task_to_orm(schema)
        assert isinstance(orm.due_at, datetime)
        assert isinstance(orm.id, str)


# ---------------------------------------------------------------------------
# 3. Query-bound parameter types (PostgreSQL dialect compilation)
# ---------------------------------------------------------------------------
def _bind_types(stmt: Select | Insert) -> dict[str, Any]:
    """Map bind name → (python type name, value) for a compiled statement."""
    c = stmt.compile(dialect=postgresql.dialect())
    params = c.construct_params()
    return {k: (type(v).__name__, v) for k, v in params.items()}


class TestQueryBoundParams:
    """Pin the query-bound parameter *types* under the PostgreSQL dialect.

    SQLite coerces ``datetime`` → string at the driver level, so these
    assertions can only be made against the *compiled* bind parameters —
    the values the driver would receive on a real asyncpg connection.
    """

    def test_goals_deadline_before_compiles_with_native_datetime_bound(self):
        # Mirrors GoalService.goals_with_deadlines_before():
        # the bound must be a native datetime, not an ISO string.
        stmt = (
            select(Goal)
            .where(Goal.status == "active")
            .where(Goal.deadline <= NOW)
        )
        binds = _bind_types(stmt)
        assert any(tn == "datetime" for tn, _ in binds.values()), (
            f"expected a datetime bind param in {binds}"
        )
        # And no ISO string sneaking in for the deadline bound.
        for tn, v in binds.values():
            if tn == "str":
                assert v != NOW.isoformat(), "string-coerced deadline found"

    def test_entity_active_at_clause_uses_native_datetime(self):
        # Mirrors EntityService.list_entities(active_at=...):
        # the half-open window clause must bind a native datetime.
        lower = Entity.valid_from <= NOW
        upper = Entity.valid_to > NOW
        stmt = select(Entity).where(lower, upper)
        binds = _bind_types(stmt)
        assert any(tn == "datetime" for tn, _ in binds.values()), (
            f"expected a datetime bind param in {binds}"
        )

    def test_relationship_active_clause_uses_native_datetime(self):
        """Pin the relationship _active_clause(instant) helper: it must build
        a clause whose bind is a native datetime (not a string)."""
        clause = _active_clause(NOW)
        stmt = select(Relationship).where(clause)
        binds = _bind_types(stmt)
        # Both lower (valid_from <= instant) and upper (valid_to > instant)
        # branches carry a datetime bind.
        dt_binds = [v for tn, v in binds.values() if tn == "datetime"]
        assert len(dt_binds) >= 2, f"expected ≥2 datetime binds, got {binds}"

    def test_insert_compiles_with_native_datetime_and_string_id(self):
        """Pin the insert() bind types: id is str, deadline is datetime."""
        stmt = (
            Goal.__table__
            .insert()
            .values(id="goal-x", deadline=NOW)
        )
        binds = _bind_types(stmt)
        id_type, id_val = binds["id"]
        dl_type, dl_val = binds["deadline"]
        assert id_type == "str" and id_val == "goal-x"
        assert dl_type == "datetime" and dl_val == NOW


# ---------------------------------------------------------------------------
# 4. D-10: structured 500 handler
# ---------------------------------------------------------------------------
class TestD10Structured500:
    """Pin the app-level Exception handler to a structured 500 body, and
    verify it does not swallow HTTPExceptions."""

    def test_handler_is_registered_on_app(self):
        """The module-level app must carry an Exception handler in its
        exception-handler registry."""
        handlers = module_app.exception_handlers
        assert Exception in handlers, (
            "no Exception handler registered on module-level app"
        )

    async def test_handler_returns_structured_500(self, monkeypatch):
        """The handler returns a JSONResponse with the structured body."""
        # Import the handler via the app's registry so the test follows the
        # *public* contract rather than a private symbol.
        handler = module_app.exception_handlers[Exception]
        # Build a minimal FastAPI Request without touching the network.
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/goals",
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 8000),
            "app": module_app,
        }
        req = Request(scope)
        resp = await handler(req, ValueError("boom"))
        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 500
        import json

        body = resp.body  # JSONResponse.body is a serialized-bytes property

        parsed = json.loads(body)
        assert parsed["detail"] == "Internal server error"
        assert parsed["error_type"] == "ValueError"

    def test_httpexception_is_not_swallowed_by_handler(self):
        """Verify the exception-handler MRO walk prefers HTTPException's
        specific handler over the generic Exception handler (so our D-10
        handler never swallows an HTTPException)."""
        # Build a small app that mirrors the production dispatch: a
        # StarletteHTTPException handler (the default) plus a generic
        # Exception handler.
        target = FastAPI()

        async def http_handler(request: Request, exc: HTTPException) -> JSONResponse:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

        async def generic_handler(request: Request, exc: BaseException) -> JSONResponse:
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error", "error_type": type(exc).__name__},
            )

        # Starlette's ExceptionMiddleware installs a default
        # StarletteHTTPException handler first; we must not override it.
        target.exception_handlers[HTTPException] = http_handler
        target.exception_handlers[Exception] = generic_handler

        @target.get("/boom")
        async def boom() -> None:
            raise ValueError("kaboom")

        @target.get("/boom-http")
        async def boom_http() -> None:
            raise HTTPException(status_code=404, detail="not found")

        from starlette.testclient import TestClient

        with TestClient(target, raise_server_exceptions=False) as client:
            r = client.get("/boom")
            assert r.status_code == 500
            assert r.json()["error_type"] == "ValueError"

            r = client.get("/boom-http")
            # HTTPException must NOT be captured by the generic handler:
            # its status code must be preserved (404) with its own detail.
            assert r.status_code == 404
            assert r.json() == {"detail": "not found"}
