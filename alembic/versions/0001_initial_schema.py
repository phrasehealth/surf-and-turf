"""Initial schema: conversations, transcript, analyses, reports.

The whole schema from docs/persistence-schema.md lands in one migration because
none of it has shipped yet; later changes get their own. Tables whose writers do
not exist yet (analyses, runs, figures) are created regardless — they are DDL, and
`record_analysis` can land against them rather than alongside them.

Revision ID: 0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UUID = pg.UUID(as_uuid=True)
JSONB = pg.JSONB
TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
NEW_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ---------------------------------------------------------- conversations
    op.create_table(
        "conversations",
        sa.Column("id", UUID, primary_key=True, server_default=NEW_UUID),
        sa.Column("serial", sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True),
        sa.Column("user_id", sa.Text),
        sa.Column("sdk_session_id", sa.Text),
        sa.Column("started_at", TS, nullable=False, server_default=NOW),
        sa.Column("last_used_at", TS, nullable=False, server_default=NOW),
        sa.Column("closed_at", TS),
        # Deleting a conversation must not destroy the record of a PDF that still
        # exists, so conversations are soft-deleted and children RESTRICT.
        sa.Column("deleted_at", TS),
        sa.Column("total_cost_usd", sa.Numeric(10, 4)),
        sa.Column("total_turns", sa.Integer),
    )
    op.create_index("ix_conversations_user", "conversations", ["user_id", sa.text("started_at DESC")])

    # ------------------------------------------------------- turns and events
    op.create_table(
        "turns",
        sa.Column("id", UUID, primary_key=True, server_default=NEW_UUID),
        sa.Column("conversation_id", UUID,
                  sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("started_at", TS, nullable=False, server_default=NOW),
        sa.Column("ended_at", TS),
        sa.Column("duration_ms", sa.Integer),
        # A running conversation total, not this turn's cost: subtract to get a delta.
        sa.Column("cost_usd_running", sa.Numeric(10, 4)),
        sa.Column("is_error", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_turns_conversation_seq"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("conversation_id", UUID,
                  sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("turn_id", UUID, sa.ForeignKey("turns.id", ondelete="CASCADE")),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("at", TS, nullable=False, server_default=NOW),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("tool_use_id", sa.Text),
        sa.Column("payload", JSONB, nullable=False),
        sa.UniqueConstraint("turn_id", "seq", name="uq_events_turn_seq"),
    )
    op.create_index("ix_events_conversation", "events", ["conversation_id", "id"])
    op.create_index("ix_events_tool_use", "events", ["tool_use_id"],
                    postgresql_where=sa.text("tool_use_id IS NOT NULL"))

    # --------------------------------------------------------------- analyses
    op.create_table(
        "analyses",
        sa.Column("id", UUID, primary_key=True, server_default=NEW_UUID),
        sa.Column("serial", sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True),
        sa.Column("lineage_id", UUID, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("supersedes_id", UUID, sa.ForeignKey("analyses.id", ondelete="SET NULL")),
        sa.Column("derived_from_id", UUID, sa.ForeignKey("analyses.id", ondelete="SET NULL")),
        sa.Column("origin_conversation_id", UUID,
                  sa.ForeignKey("conversations.id", ondelete="SET NULL")),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("subtitle", sa.Text),
        sa.Column("note_template", sa.Text),
        sa.Column("chart_type", sa.Text),
        sa.Column("chart_spec", JSONB),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("created_by", sa.Text),
        sa.Column("archived_at", TS),
        sa.UniqueConstraint("lineage_id", "version", name="uq_analyses_lineage_version"),
    )
    op.create_index("ix_analyses_creator", "analyses", ["created_by", sa.text("created_at DESC")])
    op.create_index("ix_analyses_lineage", "analyses", ["lineage_id", sa.text("version DESC")])
    op.create_index("ix_analyses_derived_from", "analyses", ["derived_from_id"])

    op.create_table(
        "analysis_queries",
        sa.Column("id", UUID, primary_key=True, server_default=NEW_UUID),
        sa.Column("analysis_id", UUID,
                  sa.ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("purpose", sa.Text),
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("sql_template", sa.Text, nullable=False),
        sa.UniqueConstraint("analysis_id", "seq", name="uq_analysis_queries_seq"),
    )
    # Exactly one primary query per analysis.
    op.create_index("uq_analysis_queries_primary", "analysis_queries", ["analysis_id"],
                    unique=True, postgresql_where=sa.text("is_primary"))

    op.create_table(
        "analysis_parameters",
        sa.Column("analysis_id", UUID,
                  sa.ForeignKey("analyses.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("expression", sa.Text),
        sa.Column("label", sa.Text),
    )
    op.create_table(
        "analysis_relations",
        sa.Column("analysis_id", UUID,
                  sa.ForeignKey("analyses.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("schema_name", sa.Text, primary_key=True),
        sa.Column("relation_name", sa.Text, primary_key=True),
    )
    op.create_table(
        "analysis_joins",
        sa.Column("analysis_id", UUID,
                  sa.ForeignKey("analyses.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("left_ref", sa.Text, primary_key=True),
        sa.Column("right_ref", sa.Text, primary_key=True),
        sa.Column("verified", sa.Boolean, nullable=False),
        sa.Column("match_pct", sa.Numeric(5, 2)),
    )

    op.create_table(
        "analysis_runs",
        sa.Column("id", UUID, primary_key=True, server_default=NEW_UUID),
        sa.Column("analysis_id", UUID,
                  sa.ForeignKey("analyses.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("ran_at", TS, nullable=False, server_default=NOW),
        sa.Column("ran_by", sa.Text),
        # 'adopted' = bound to results run_sql already produced; 'executed' = we ran it.
        sa.Column("origin", sa.Text, nullable=False, server_default=sa.text("'adopted'")),
        sa.Column("bound_params", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("database_name", sa.Text, nullable=False),
        sa.Column("role_name", sa.Text),
        sa.Column("error", sa.Text),
        sa.Column("qcp_built_at", TS),
        sa.Column("qcp_conformance", sa.Text),
        sa.Column("freshness", JSONB),
        sa.Column("resolved_note", sa.Text),
    )
    op.create_index("ix_analysis_runs_analysis", "analysis_runs",
                    ["analysis_id", sa.text("ran_at DESC")])

    op.create_table(
        "analysis_run_queries",
        sa.Column("run_id", UUID,
                  sa.ForeignKey("analysis_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("analysis_query_id", UUID,
                  sa.ForeignKey("analysis_queries.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("tool_use_id", sa.Text),
        sa.Column("resolved_sql", sa.Text, nullable=False),
        # NULL until checked; FALSE is allowed, not an error (templates may approximate).
        sa.Column("template_verified", sa.Boolean),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("row_count", sa.Integer),
        sa.Column("error", sa.Text),
        sa.Column("observed_date_from", sa.Date),
        sa.Column("observed_date_to", sa.Date),
        sa.Column("result_digest", sa.Text),
    )
    op.create_index("ix_arq_tool_use", "analysis_run_queries", ["tool_use_id"],
                    postgresql_where=sa.text("tool_use_id IS NOT NULL"))

    # ---------------------------------------------------------------- reports
    op.create_table(
        "reports",
        sa.Column("id", UUID, primary_key=True, server_default=NEW_UUID),
        sa.Column("serial", sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True),
        # Nullable: an assembled report belongs to no single conversation.
        sa.Column("origin_conversation_id", UUID,
                  sa.ForeignKey("conversations.id", ondelete="RESTRICT")),
        sa.Column("refresh_of_id", UUID, sa.ForeignKey("reports.id", ondelete="SET NULL")),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("subtitle", sa.Text),
        sa.Column("storage_backend", sa.Text, nullable=False),
        sa.Column("storage_key", sa.Text, nullable=False),
        sa.Column("report_uid", sa.Text, nullable=False, unique=True),
        sa.Column("size_bytes", sa.Integer),
        sa.Column("published_at", TS, nullable=False, server_default=NOW),
        sa.Column("published_by", sa.Text),
        sa.Column("handling_marking", sa.Text),
        sa.Column("qcp_built_at", TS),
        sa.Column("body_markdown", sa.Text),
    )
    op.create_index("ix_reports_published", "reports", [sa.text("published_at DESC")])
    op.create_index("ix_reports_refresh_of", "reports", ["refresh_of_id"])

    op.create_table(
        "report_contents",
        sa.Column("report_id", UUID,
                  sa.ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("analysis_run_id", UUID,
                  sa.ForeignKey("analysis_runs.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("position", sa.Integer, nullable=False),
        sa.UniqueConstraint("report_id", "position", name="uq_report_contents_position"),
    )
    op.create_table(
        "figures",
        sa.Column("id", UUID, primary_key=True, server_default=NEW_UUID),
        sa.Column("report_id", UUID,
                  sa.ForeignKey("reports.id", ondelete="CASCADE"), nullable=False),
        sa.Column("analysis_run_id", UUID,
                  sa.ForeignKey("analysis_runs.id", ondelete="SET NULL")),
        sa.Column("serial", sa.Integer, nullable=False),
        # Denormalised: the label is printed in a PDF someone may already hold.
        sa.Column("label", sa.Text, nullable=False, unique=True),
        sa.Column("chart_type", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("subtitle", sa.Text),
        sa.Column("note", sa.Text),
        sa.Column("svg", sa.Text),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("report_id", "serial", name="uq_figures_report_serial"),
    )

    # ------------------------------------------- SDK transcript (resume, §6)
    op.create_table(
        "sdk_transcript_entries",
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("project_key", sa.Text, nullable=False),
        sa.Column("entry_uuid", UUID),
        sa.Column("entry", JSONB, nullable=False),
        sa.Column("written_at", TS, nullable=False, server_default=NOW),
    )
    # The SDK asks adapters to treat `uuid` as an idempotency key where present.
    op.create_index("uq_sdk_transcript_entry", "sdk_transcript_entries",
                    ["session_id", "entry_uuid"], unique=True,
                    postgresql_where=sa.text("entry_uuid IS NOT NULL"))
    op.create_index("ix_sdk_transcript_session", "sdk_transcript_entries",
                    ["session_id", "seq"])


def downgrade() -> None:
    for t in ("sdk_transcript_entries", "figures", "report_contents", "reports",
              "analysis_run_queries", "analysis_runs", "analysis_joins",
              "analysis_relations", "analysis_parameters", "analysis_queries",
              "analyses", "events", "turns", "conversations"):
        op.drop_table(t)
