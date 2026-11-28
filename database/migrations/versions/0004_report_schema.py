"""Move Report Intelligence objects into the report schema.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TABLES = (
    "app_settings",
    "job_runs",
    "keyword_rules",
    "report_types",
    "source_files",
    "failed_parse_queue",
    "raw_cells",
    "report_templates",
    "reports",
    "expected_reports",
    "report_lines",
    "report_metrics",
    "alert_events",
)
SEARCH_FUNCTION = "search_report_lines(text, text, text, date, date, text, integer, integer)"


def _move_tables(source: str, target: str) -> None:
    bind = op.get_bind()
    for table in TABLES:
        exists = bind.scalar(
            sa.text("SELECT to_regclass(:qualified_name) IS NOT NULL"),
            {"qualified_name": f'{source}."{table}"'},
        )
        if exists:
            op.execute(sa.text(f'ALTER TABLE "{source}"."{table}" SET SCHEMA "{target}"'))


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS report AUTHORIZATION CURRENT_USER")
    _move_tables("public", "report")

    op.execute(f"ALTER FUNCTION public.{SEARCH_FUNCTION} SET SCHEMA report")
    op.execute(f"ALTER FUNCTION report.{SEARCH_FUNCTION} SET search_path = report, public")
    op.execute(f"REVOKE ALL ON FUNCTION report.{SEARCH_FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION report.{SEARCH_FUNCTION} TO report_app")

    op.execute("GRANT USAGE ON SCHEMA report TO report_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA report TO report_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA report TO report_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE report_owner IN SCHEMA report "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO report_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE report_owner IN SCHEMA report "
        "GRANT USAGE, SELECT ON SEQUENCES TO report_app"
    )


def downgrade() -> None:
    op.execute(f"ALTER FUNCTION report.{SEARCH_FUNCTION} SET SCHEMA public")
    op.execute(f"ALTER FUNCTION public.{SEARCH_FUNCTION} SET search_path = public")
    _move_tables("report", "public")
    op.execute("REVOKE USAGE ON SCHEMA report FROM report_app")
