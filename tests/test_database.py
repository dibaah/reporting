"""Phase 1 checks. Needs a migrated database (alembic upgrade head) and a .env."""
import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError

from database import seed
from database.db import get_engine, session_scope
from database.models import (
    AlertEvent, AppSetting, Base, KeywordRule, Report, ReportLine, ReportType, SourceFile,
)

EXPECTED_TABLES = {
    "report_types", "report_templates", "source_files", "reports", "report_lines",
    "report_metrics", "raw_cells", "failed_parse_queue", "keyword_rules", "alert_events",
    "expected_reports", "app_settings", "job_runs",
}


def test_all_tables_exist():
    existing = set(inspect(get_engine()).get_table_names())
    assert EXPECTED_TABLES <= existing
    assert EXPECTED_TABLES == set(Base.metadata.tables)


def test_seed_is_idempotent():
    seed.run()
    second = seed.run()
    assert second == {"report_types": 0, "settings": 0}
    with session_scope() as s:
        names = set(s.scalars(select(ReportType.name)))
        assert {"12H", "Biweekly", "Monthly"} <= names
        assert s.get(AppSetting, "live_import_interval_minutes").value == "5"


def test_report_lines_full_text_and_trigram():
    with session_scope() as s:
        rt = s.scalars(select(ReportType)).first()
        sf = SourceFile(origin="archive", file_name="t.xlsx", sha256="a" * 64)
        s.add(sf)
        s.flush()
        rep = Report(report_type_id=rt.id, source_file_id=sf.id, report_date="2013-01-01")
        rep.lines = [
            ReportLine(line_no=1, label="K-101", text="Vibration high on compressor K-101"),
            ReportLine(line_no=2, label="Note", text="No leak detected"),
        ]
        s.add(rep)
        s.flush()
        # tag stays intact with the 'simple' configuration
        hits = s.scalars(
            select(ReportLine.line_no).where(
                ReportLine.search_vector.op("@@")(text("plainto_tsquery('simple','vibration')"))
            )
        ).all()
        assert hits == [1]
        # trigram similarity works (extension installed)
        sim = s.execute(text("select similarity('compresor','compressor')")).scalar()
        assert sim > 0.4
        s.rollback()  # leave the database clean


def test_duplicate_file_hash_rejected():
    with pytest.raises(DBAPIError):
        with session_scope() as s:
            s.add(SourceFile(origin="archive", file_name="a", sha256="b" * 64))
            s.add(SourceFile(origin="archive", file_name="b", sha256="b" * 64))


def test_check_constraints():
    with pytest.raises(DBAPIError):
        with session_scope() as s:
            s.add(KeywordRule(keyword="x", severity="urgent"))


def test_runtime_user_cannot_change_schema():
    with pytest.raises(DBAPIError):
        with get_engine().begin() as c:
            c.execute(text("create table should_fail (id int)"))
