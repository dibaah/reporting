"""columns needed by the Outlook live importer and the alert service

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # alert service: which Outlook files have already been scanned for keywords
    op.add_column("source_files", sa.Column("alert_scanned_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_source_files_alert_pending", "source_files", ["received_at"],
        postgresql_where=sa.text("origin = 'outlook' AND alert_scanned_at IS NULL"),
    )
    # one warning per failed live file
    op.add_column("failed_parse_queue", sa.Column("notified_at", sa.DateTime(timezone=True)))
    # repeats of the same finding in later reports are annotated, not silenced
    op.add_column("alert_events", sa.Column("repeat_key", sa.String(64)))
    op.add_column("alert_events", sa.Column("repeat_count", sa.Integer(), server_default="0", nullable=False))
    op.create_index("ix_alert_events_repeat", "alert_events", ["rule_id", "repeat_key", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_alert_events_repeat", table_name="alert_events")
    op.drop_column("alert_events", "repeat_count")
    op.drop_column("alert_events", "repeat_key")
    op.drop_column("failed_parse_queue", "notified_at")
    op.drop_index("ix_source_files_alert_pending", table_name="source_files")
    op.drop_column("source_files", "alert_scanned_at")
