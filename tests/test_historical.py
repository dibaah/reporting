"""Phase 3: historical importer and Phase 0 inventory. Needs a migrated database and .env."""
import csv
import shutil
from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import delete, func, select

import ingestion_engine.historical as hist
from database import seed
from database.db import session_scope
from database.models import (
    AlertEvent, FailedParseQueue, JobRun, RawCells, Report, ReportLine, SourceFile,
)
from ingestion_engine.historical import failed_summary, folder_hint, run_import, scan_files
from ingestion_engine.inventory import run_inventory
from parser_engine.contract import ParsedLine, ParsedReport
from parser_engine.registry import Template
from tests.example_templates import make_registry

SAMPLES = Path(__file__).parent / "samples"
REG = make_registry()


@pytest.fixture(scope="module", autouse=True)
def _seed():
    seed.run()


@pytest.fixture()
def archive(tmp_path):
    """A small fake archive: year/month folders, duplicates, lock file, junk, damaged files."""
    root = tmp_path / "2013"
    jan, feb = root / "January", root / "March"
    jan.mkdir(parents=True)
    feb.mkdir()
    shutil.copy(SAMPLES / "legacy_12h_2013-01-05.xls", jan / "report_a.xls")          # 12 lines
    shutil.copy(SAMPLES / "modern_12h_2024-03-10.xlsx", jan / "report_b.xlsx")        # 6 lines (2024 in a 2013 folder)
    shutil.copy(SAMPLES / "legacy_12h_2013-01-05.xls", feb / "same_content_again.xls")  # duplicate by hash
    shutil.copy(SAMPLES / "unknown_layout.xlsx", jan / "unknown.xlsx")                # no template
    shutil.copy(SAMPLES / "corrupt.xls", feb / "corrupt.xls")                         # damaged
    (jan / "~$report_a.xls").write_bytes(b"lock")                                     # Office lock file
    (jan / "notes.txt").write_text("not excel")
    yield root
    _cleanup(root)


def _cleanup(root: Path):
    """The importer commits for real, so remove everything this test created."""
    with session_scope() as s:
        ids = s.scalars(select(SourceFile.id).where(SourceFile.path.like(str(root.parent) + "%"))).all()
        if ids:
            s.execute(delete(Report).where(Report.source_file_id.in_(ids)))
            s.execute(delete(SourceFile).where(SourceFile.id.in_(ids)))
        s.execute(delete(JobRun).where(JobRun.job_type == "historical_import",
                                       JobRun.details["root"].astext.like(str(root.parent) + "%")))


def counts(root: Path) -> dict:
    with session_scope() as s:
        like = SourceFile.path.like(str(root.parent) + "%")
        ids = select(SourceFile.id).where(like)
        return {
            "files": s.scalar(select(func.count()).select_from(SourceFile).where(like)),
            "reports": s.scalar(select(func.count()).select_from(Report).where(Report.source_file_id.in_(ids))),
            "lines": s.scalar(select(func.count()).select_from(ReportLine).join(Report).where(Report.source_file_id.in_(ids))),
            "raw": s.scalar(select(func.count()).select_from(RawCells).where(RawCells.source_file_id.in_(ids))),
            "failed": s.scalar(select(func.count()).select_from(FailedParseQueue).where(
                FailedParseQueue.source_file_id.in_(ids), FailedParseQueue.resolved.is_(False))),
            "alerts": s.scalar(select(func.count()).select_from(AlertEvent)),
        }


# ---------------------------------------------------------------- scanning

def test_scan_finds_excel_only_and_skips_lock_files(archive):
    names = sorted(p.name for p in scan_files(archive))
    assert names == ["corrupt.xls", "report_a.xls", "report_b.xlsx", "same_content_again.xls", "unknown.xlsx"]


@pytest.mark.parametrize("rel,expected", [
    ("2013/January/a.xls", (2013, 1)), ("2013/02 February/a.xls", (2013, 2)), ("2014/Sep/x/a.xls", (2014, 9)),
    ("misc/a.xls", (None, None)),
])
def test_folder_hint(tmp_path, rel, expected):
    assert folder_hint(tmp_path / rel, tmp_path) == expected


def test_folder_hint_uses_root_name_when_root_is_the_year_folder(tmp_path):
    root = tmp_path / "2013"
    assert folder_hint(root / "March" / "a.xls", root) == (2013, 3)


# ---------------------------------------------------------------- the plan's "done when"

def test_dry_run_writes_nothing_and_reports_what_would_happen(archive):
    before = counts(archive)
    sm = run_import(archive, dry_run=True, registry=REG, collect_rows=True)
    assert counts(archive) == before == {**before, "files": 0, "reports": 0}
    assert (sm.imported, sm.queued_failed, sm.duplicates_in_run) == (2, 2, 1)
    assert sm.by_reason == {"no_template_match": 1, "unreadable_file": 1}
    outcomes = {Path(r["path"]).name: r["outcome"] for r in sm.rows}
    # identical content in two folders: one copy is imported, the other is reported as a duplicate
    assert sorted([outcomes["report_a.xls"], outcomes["same_content_again.xls"]]) == ["duplicate_in_run", "would_import"]


def test_import_twice_creates_no_duplicates_and_damaged_file_is_queued(archive):
    alerts_before = counts(archive)["alerts"]
    first = run_import(archive, registry=REG)
    c1 = counts(archive)
    assert (first.imported, first.queued_failed, first.duplicates_in_run, first.errors) == (2, 2, 1, 0)
    assert c1["files"] == 4 and c1["reports"] == 2 and c1["lines"] == 12 + 6 and c1["failed"] == 2
    assert c1["raw"] >= 4  # every readable file keeps its raw cells (modern has a hidden sheet too)

    second = run_import(archive, registry=REG)
    assert counts(archive) == c1
    assert (second.imported, second.queued_failed, second.errors) == (0, 0, 0)
    assert second.skipped_known == 4 and second.duplicates_in_run == 1

    reasons = {r["reason"]: r["count"] for r in failed_summary() if r["count"]}
    assert reasons.get("no_template_match", 0) >= 1 and reasons.get("unreadable_file", 0) >= 1
    assert c1["alerts"] == alerts_before  # historical import never creates alerts


def test_source_files_record_origin_path_and_hash(archive):
    run_import(archive, registry=REG)
    with session_scope() as s:
        sf = s.scalar(select(SourceFile).where(SourceFile.path.like("%report_b.xlsx")))
        assert sf.origin == "archive" and sf.received_at is None and len(sf.sha256) == 64
        assert sf.status == "parsed" and sf.size_bytes > 0


def test_folder_mismatch_is_counted_but_never_blocks(archive):
    sm = run_import(archive, registry=REG)
    assert sm.folder_mismatch == 1  # the 2024 report inside the 2013 folder
    assert sm.imported == 2


def test_job_run_is_recorded(archive):
    run_import(archive, registry=REG)
    with session_scope() as s:
        job = s.scalar(select(JobRun).where(JobRun.job_type == "historical_import",
                                            JobRun.details["root"].astext == str(archive)))
        assert job.status == "success" and job.finished_at is not None
        assert job.details["imported"] == 2 and job.details["queued_failed"] == 2


# ---------------------------------------------------------------- resume, stop, limit, errors

def test_limit_then_resume_completes_without_duplicates(archive):
    first = run_import(archive, registry=REG, limit=2)
    assert first.imported + first.queued_failed == 2
    second = run_import(archive, registry=REG)
    c = counts(archive)
    assert c["files"] == 4 and c["reports"] == 2 and c["lines"] == 18
    assert first.imported + second.imported == 2


def test_stop_control_ends_cleanly_and_a_later_run_finishes(archive):
    calls = {"n": 0}

    def control():
        calls["n"] += 1
        return "stop" if calls["n"] > 2 else "run"

    sm = run_import(archive, registry=REG, control=control)
    assert sm.stopped and sm.processed == 2
    with session_scope() as s:
        job = s.scalar(select(JobRun).where(JobRun.details["root"].astext == str(archive)))
        assert job.status == "success" and job.details["stopped"] is True
    run_import(archive, registry=REG)
    assert counts(archive)["files"] == 4


def test_pause_waits_then_continues(archive):
    states = iter(["pause", "pause"] + ["run"] * 50)
    sm = run_import(archive, registry=REG, control=lambda: next(states), pause_poll_seconds=0)
    assert not sm.stopped and sm.imported == 2


def test_one_bad_file_does_not_stop_the_run(archive, monkeypatch):
    real = hist.save_outcome

    def flaky(session, sf, outcome, registry=None):
        if sf.file_name == "report_b.xlsx":
            raise RuntimeError("disk full (simulated)")
        return real(session, sf, outcome, registry)

    monkeypatch.setattr(hist, "save_outcome", flaky)
    sm = run_import(archive, registry=REG)
    assert sm.errors == 1 and sm.imported == 1 and "disk full" in sm.error_samples[0]
    assert counts(archive)["reports"] == 1
    monkeypatch.setattr(hist, "save_outcome", real)
    again = run_import(archive, registry=REG)  # the failed file is retried automatically
    assert again.imported == 1 and counts(archive)["reports"] == 2


def test_repeated_errors_abort_the_run_and_mark_the_job_failed(archive, monkeypatch):
    monkeypatch.setattr(hist, "MAX_CONSECUTIVE_ERRORS", 2)
    monkeypatch.setattr(hist, "save_outcome", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    sm = run_import(archive, registry=REG)
    assert sm.aborted and sm.errors == 2
    with session_scope() as s:
        job = s.scalar(select(JobRun).where(JobRun.details["root"].astext == str(archive)))
        assert job.status == "failed" and "consecutive" in job.error


def test_keyboard_interrupt_marks_job_failed_and_propagates(archive):
    def control():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_import(archive, registry=REG, control=control)
    with session_scope() as s:
        job = s.scalar(select(JobRun).where(JobRun.details["root"].astext == str(archive)))
        assert job.status == "failed" and "interrupted" in job.error


def test_missing_or_empty_root_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        run_import(tmp_path / "nope", registry=REG)


# ---------------------------------------------------------------- after adding a template

class MaintenancePlan(Template):
    name = "example_maintenance"
    report_type = "12H"

    def detect(self, wb):
        return 1.0 if wb.get_sheet("Maintenance plan") else 0.0

    def parse(self, wb):
        import datetime as dt
        rep = ParsedReport(report_type="12H", template_name=self.name, report_date=dt.date(2013, 1, 9))
        rep.lines = [ParsedLine(line_no=1, label="Task", text="Inspect valve V-9")]
        return rep


def test_reparse_failed_after_a_new_template_is_added(archive):
    run_import(archive, registry=REG)
    reg2 = make_registry()
    reg2.add(MaintenancePlan())

    plain = run_import(archive, registry=reg2)                    # without the flag nothing is retried
    assert plain.reparsed == 0 and plain.skipped_known == 4

    sm = run_import(archive, registry=reg2, reparse_failed=True)
    assert sm.reparsed == 1 and sm.queued_failed == 1             # unknown.xlsx fixed, corrupt.xls still bad
    c = counts(archive)
    assert c["reports"] == 3 and c["failed"] == 1 and c["files"] == 4


# ---------------------------------------------------------------- Phase 0 inventory

def test_inventory_groups_layouts_and_lists_unreadable_files(archive, tmp_path):
    # a second file of the SAME layout with a different date: must land in the same group
    wb = openpyxl.load_workbook(SAMPLES / "modern_12h_2024-03-10.xlsx")
    wb["12H Report"]["B2"] = "2013-02-11"
    wb["12H Report"]["B4"] = "Someone Else"
    wb.save(archive / "March" / "report_c.xlsx")
    shutil.copy(SAMPLES / "misnamed_modern.xls", archive / "March" / "misnamed.xls")

    res = run_inventory(archive, tmp_path / "out")
    assert res["files"] == 7 and res["unreadable"] == 1           # only corrupt.xls (notes.txt is not Excel)
    assert res["wrong_extension"] == 1                            # the misnamed .xls holding xlsx content
    rows = list(csv.DictReader(open(tmp_path / "out" / "inventory.csv", encoding="utf-8-sig")))
    by_name = {Path(r["path"]).name: r for r in rows}
    assert by_name["report_b.xlsx"]["layout_key"] == by_name["report_c.xlsx"]["layout_key"] == by_name["misnamed.xls"]["layout_key"]
    assert by_name["report_a.xls"]["layout_key"] != by_name["report_b.xlsx"]["layout_key"]
    assert by_name["report_b.xlsx"]["hidden_sheets"] == "Lists"
    assert "unreadable" in by_name["corrupt.xls"]["error"]
    lay = list(csv.DictReader(open(tmp_path / "out" / "layouts.csv", encoding="utf-8-sig")))
    top = lay[0]
    assert int(top["files"]) == 3 and top["years"] == "2013"      # modern layout, three files
