"""Phase 2: saving parse outcomes to the database. Needs a migrated database and .env."""
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import seed
from database.db import get_engine
from database.models import (
    FailedParseQueue, RawCells, Report, ReportLine, ReportMetric, ReportTemplate, SourceFile,
)
from parser_engine.pipeline import parse_file
from parser_engine.store import save_outcome
from tests.example_templates import make_registry

SAMPLES = Path(__file__).parent / "samples"
REG = make_registry()


@pytest.fixture(scope="module", autouse=True)
def _seed():
    seed.run()


@pytest.fixture()
def session():
    """Everything a test writes is rolled back."""
    conn = get_engine().connect()
    trans = conn.begin()
    s = Session(bind=conn, expire_on_commit=False)
    yield s
    s.close()
    trans.rollback()
    conn.close()


def source_file(name: str) -> SourceFile:
    data = (SAMPLES / name).read_bytes()
    return SourceFile(origin="archive", file_name=name, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))


def count(s, model, *where) -> int:
    return s.scalar(select(func.count()).select_from(model).where(*where))


def test_successful_parse_saves_report_lines_metrics_and_raw_cells(session):
    sf = source_file("legacy_12h_2013-01-05.xls")
    save_outcome(session, sf, parse_file(SAMPLES / sf.file_name, REG), REG)
    rep = session.scalar(select(Report).where(Report.source_file_id == sf.id))
    assert sf.status == "parsed"
    assert str(rep.report_date) == "2013-01-05" and rep.shift == "Day"
    assert rep.header["_parser"]["template"] == "example_12h_legacy"
    lines = session.scalars(select(ReportLine).where(ReportLine.report_id == rep.id).order_by(ReportLine.line_no)).all()
    assert len(lines) == 12 and lines[0].label == "K-101"
    assert count(session, ReportMetric, ReportMetric.report_id == rep.id) == 7
    assert count(session, RawCells, RawCells.source_file_id == sf.id) == 1
    assert count(session, FailedParseQueue, FailedParseQueue.source_file_id == sf.id) == 0
    assert session.scalar(select(ReportTemplate.parser_name).where(ReportTemplate.id == rep.template_id)) == "example_12h_legacy"


def test_search_vector_is_filled_for_saved_lines(session):
    sf = source_file("legacy_12h_2013-01-05.xls")
    save_outcome(session, sf, parse_file(SAMPLES / sf.file_name, REG), REG)
    session.flush()
    hits = session.execute(
        select(ReportLine.label).where(ReportLine.search_vector.op("@@")(func.plainto_tsquery("simple", "k-102")))
    ).scalars().all()
    assert "K-102" in hits


def test_unknown_layout_is_queued_and_raw_cells_are_kept(session):
    sf = source_file("unknown_layout.xlsx")
    save_outcome(session, sf, parse_file(SAMPLES / sf.file_name, REG), REG)
    q = session.scalars(select(FailedParseQueue).where(FailedParseQueue.source_file_id == sf.id)).all()
    assert [r.reason for r in q] == ["no_template_match"]
    assert sf.status == "failed"
    raw = session.scalar(select(RawCells).where(RawCells.source_file_id == sf.id))
    assert "Inspect valve V-9" in raw.text_dump
    assert count(session, Report, Report.source_file_id == sf.id) == 0


def test_saving_twice_replaces_instead_of_duplicating(session):
    sf = source_file("modern_12h_2024-03-10.xlsx")
    for _ in range(2):
        save_outcome(session, sf, parse_file(SAMPLES / sf.file_name, REG), REG)
    assert count(session, Report, Report.source_file_id == sf.id) == 1
    assert count(session, ReportLine) == 6
    assert count(session, RawCells, RawCells.source_file_id == sf.id) == 2  # visible + hidden sheet
    hidden = session.scalar(select(RawCells.is_hidden).where(RawCells.source_file_id == sf.id, RawCells.sheet_name == "Lists"))
    assert hidden is True


def test_repeated_failure_increments_retry_and_success_resolves_it(session):
    sf = source_file("unknown_layout.xlsx")
    out = parse_file(SAMPLES / sf.file_name, REG)
    save_outcome(session, sf, out, REG)
    save_outcome(session, sf, out, REG)
    row = session.scalar(select(FailedParseQueue).where(FailedParseQueue.source_file_id == sf.id))
    assert count(session, FailedParseQueue, FailedParseQueue.source_file_id == sf.id) == 1
    assert row.retry_count == 1 and row.resolved is False

    # a template for this layout is added later: re-parsing succeeds and closes the queue entry
    good = parse_file(SAMPLES / "modern_12h_2024-03-10.xlsx", REG)
    save_outcome(session, sf, good, REG)
    session.flush()
    assert row.resolved is True and sf.status == "parsed"


def test_report_type_missing_from_database_is_queued(session):
    sf = source_file("modern_12h_2024-03-10.xlsx")
    out = parse_file(SAMPLES / sf.file_name, REG)
    out.report.report_type = "Weekly"  # not in report_types
    save_outcome(session, sf, out, REG)
    reason = session.scalar(select(FailedParseQueue.reason).where(FailedParseQueue.source_file_id == sf.id))
    assert reason == "unknown_report_type"
    assert count(session, Report, Report.source_file_id == sf.id) == 0


def test_validation_failure_keeps_raw_cells(session):
    sf = source_file("legacy_missing_date.xls")
    save_outcome(session, sf, parse_file(SAMPLES / sf.file_name, REG), REG)
    assert session.scalar(select(FailedParseQueue.reason).where(FailedParseQueue.source_file_id == sf.id)) == "validation_failed"
    assert count(session, RawCells, RawCells.source_file_id == sf.id) == 1
