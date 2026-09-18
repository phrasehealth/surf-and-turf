"""analysis_parameters.operator — how a value is compared.

A parameter is a filter someone might later want to swap, and the test is the shape
of the comparison: set membership (`IN`) and ranges (`BETWEEN`, `<`, `>`) are
parameters; a scalar equality usually is not. Recording the operator makes that
judgement auditable, and tells a picker whether a binding is one value or many.

`kind` (what the values are — diagnosis, medication, orderset …) stays free text: the
list grows with the warehouse and a migration per domain buys nothing. `operator` is
constrained, because that set is SQL's and does not move.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

OPERATORS = ("in", "between", "gt", "gte", "lt", "lte", "eq")


def upgrade() -> None:
    # No rows exist yet, but a server_default keeps the NOT NULL honest if that
    # changes before this ships anywhere.
    op.add_column("analysis_parameters",
                  sa.Column("operator", sa.Text, nullable=False,
                            server_default=sa.text("'in'")))
    op.create_check_constraint(
        "analysis_parameters_operator_check", "analysis_parameters",
        sa.column("operator").in_(OPERATORS))
    op.alter_column("analysis_parameters", "operator", server_default=None)


def downgrade() -> None:
    op.drop_constraint("analysis_parameters_operator_check", "analysis_parameters")
    op.drop_column("analysis_parameters", "operator")
