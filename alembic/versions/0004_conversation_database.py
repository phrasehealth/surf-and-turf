"""conversations.database — the database a conversation is bound to.

A conversation names its database when it starts and cannot change it: every
statement is checked against it, because a fully-qualified name would otherwise
reach straight out of the database the user picked. Persisted so a revived
conversation keeps querying what it was bound to rather than re-choosing.

Nullable because rows written before this existed have no answer, and inventing one
would be worse than admitting it. New conversations always set it.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("database", sa.Text))
    op.create_index("ix_conversations_database", "conversations", ["database"])


def downgrade() -> None:
    op.drop_index("ix_conversations_database", "conversations")
    op.drop_column("conversations", "database")
