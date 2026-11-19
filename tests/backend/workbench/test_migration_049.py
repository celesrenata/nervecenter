"""Migration 049_workbench: additive only, reversible, single alembic head.

Validates: Requirements 2.14
"""
from __future__ import annotations

import ast
import importlib
import re

import sqlalchemy as sa
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

MODULE = "backend.persistence.alembic.versions.049_workbench"
NEW_TABLES = {
    "workbench_projects",
    "workbench_sessions",
    "workbench_model_usage",
    "workbench_ide",
    "workbench_audit",
}
FORBIDDEN_OPS = {"add_column", "alter_column", "drop_column", "rename_table", "execute"}


def _sqlite_compat(engine: sa.Engine) -> None:
    """The migration targets PostgreSQL; make the DDL acceptable to SQLite."""

    @sa.event.listens_for(engine, "before_cursor_execute", retval=True)
    def _rewrite(conn, cursor, statement, parameters, context, executemany):
        statement = re.sub(r"slug ~ '[^']*'", "1=1", statement)
        return statement.replace("char_length(", "length("), parameters


def _run(engine: sa.Engine, fn_name: str) -> None:
    mod = importlib.import_module(MODULE)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            getattr(mod, fn_name)()


def _tables(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_single_alembic_head():
    cfg = Config()
    cfg.set_main_option("script_location", "backend/persistence/alembic")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, heads
    # 049_workbench is in the single chain, directly on top of 049_voicemails.
    assert script.get_revision("049_workbench").down_revision == "049_voicemails"
    assert "049_workbench" in {r.revision for r in script.iterate_revisions(heads[0], "base")}


def test_upgrade_then_downgrade_on_scratch_db(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'scratch.db'}")
    _sqlite_compat(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE existing_table (id INTEGER PRIMARY KEY, v TEXT)"))
        conn.execute(sa.text("INSERT INTO existing_table (v) VALUES ('keep')"))
    before = _tables(engine)

    _run(engine, "upgrade")
    assert _tables(engine) == before | NEW_TABLES

    _run(engine, "downgrade")
    assert _tables(engine) == before
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT v FROM existing_table")).scalar() == "keep"
    engine.dispose()


def _ops(fn_name: str) -> list[tuple[str, str | None]]:
    mod = importlib.import_module(MODULE)
    tree = ast.parse(open(mod.__file__).read())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    found: list[tuple[str, str | None]] = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "op"
        ):
            first = node.args[0] if node.args else None
            target = first.value if isinstance(first, ast.Constant) else None
            found.append((node.func.attr, target))
    return found


def test_upgrade_only_creates_new_tables():
    ops = _ops("upgrade")
    assert ops
    assert not {name for name, _ in ops} & FORBIDDEN_OPS
    created = {t for name, t in ops if name == "create_table"}
    assert created == NEW_TABLES
    assert {name for name, _ in ops} <= {"create_table", "create_index"}


def test_downgrade_only_drops_tables_it_created():
    ops = _ops("downgrade")
    assert not {name for name, _ in ops} & FORBIDDEN_OPS
    dropped = {t for name, t in ops if name == "drop_table"}
    assert dropped == NEW_TABLES
