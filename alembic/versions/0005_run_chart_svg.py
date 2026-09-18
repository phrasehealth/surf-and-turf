"""analysis_runs.chart_svg — the chart as drawn from that run's numbers.

A chart is a rendering of one run's results, so it belongs to the run: the server
has the rows at record time and does not keep them afterwards, so the drawing has to
be made while they are in hand. `figures` then holds the *placement* of that chart in
a report — its position, its caption — and copies the SVG so a published PDF stays
true to itself even if the analysis is later corrected.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analysis_runs", sa.Column("chart_svg", sa.Text))
    # Why a chart could not be drawn, when one was asked for. Kept so the failure is
    # visible in the record rather than only in a log line nobody reads.
    op.add_column("analysis_runs", sa.Column("chart_error", sa.Text))


def downgrade() -> None:
    op.drop_column("analysis_runs", "chart_error")
    op.drop_column("analysis_runs", "chart_svg")
