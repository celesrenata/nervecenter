"""Server defaults for auth timestamps the ORM assumed the database had.

The early migration created ``auth_sessions.issued_at``,
``auth_devices.created_at`` and ``pairing_challenges.created_at`` as NOT NULL
without a server default, while the ORM declares ``server_default=now()`` and
so omits them on insert. Once sessions started being stored (NS-ADR-123),
login failed with a NOT NULL violation. This only sets column defaults; no
data changes.

Reversibility: ``downgrade()`` drops the three defaults again.
"""
import sqlalchemy as sa
from alembic import op

revision = "038_auth_time_defaults"
down_revision = "037_sessions_pat"
branch_labels = None
depends_on = None

COLUMNS = (
    ("auth_sessions", "issued_at"),
    ("auth_devices", "created_at"),
    ("pairing_challenges", "created_at"),
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite test databases are built from the ORM, which has the default
    for table, column in COLUMNS:
        op.alter_column(table, column, server_default=sa.text("now()"))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, column in COLUMNS:
        op.alter_column(table, column, server_default=None)
