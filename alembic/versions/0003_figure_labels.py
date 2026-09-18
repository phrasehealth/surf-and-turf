"""Figure labels become positional captions, not global identifiers.

`R-88-03` embedded the report serial, which does not exist until publish — so the
body needed placeholders resolved at render, and the agent had to carry an identifier
it could not yet know. Dropping the report serial removes that whole problem.

A figure is now captioned by its position in the report it appears in: `Figure 1`,
`Figure 2`. Durable identity is the analysis it was drawn from, which already has a
serial (`A-1042.1`) and is already returned by `record_analysis`. So the caption is
for the reader and the analysis label is for anyone who needs to find it again.

`label` therefore stops being unique — every report has a `Figure 1` — while
(report_id, serial) stays unique, which is what actually has to hold.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("figures_label_key", "figures", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint("figures_label_key", "figures", ["label"])
