"""Phase 5: the live importer (Outlook folder -> database)."""
import datetime as dt
import logging
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

import ingestion_engine.live as live
from database import seed
from database.db import get_engine, session_scope
from database.models import (
    AlertEvent, AppSetting, FailedParseQueue, JobRun, RawCells, Report, ReportLine, SourceFile,
)
from ingestion_engine.live import run_cycle, run_forever, run_once
from ingestion_engine.mail.base import MailConfigError, MailSourceError
from ingestion_engine.settings import get_setting, set_setting
from tests.example_templates import make_registry
from tests.mailfakes import FakeSource, line, make_xlsx, msg, savepoint_factory

UTC = dt.timezone.utc
REG = make_registry()
D1, D2, D3 = (dt.datetime(2999, 1, d, 6, 0, tzinfo=UTC) for d in (1, 2, 3))


@pytest.fixture(scope="module", autouse=True)
def _seed():
    seed.run()


@pytest.fixture()
def db():
    conn = get_engine().connect()
    trans = conn.begin()
    s = Session(bind=conn, expire_on_commit=False)
    yield s
    s.close()
    trans.rollback()
    conn.close()


def cycle(db, source, tmp_path, **kw):
    return run_cycle(source, registry=REG, session_factory=savepoint_factory(db), raw_store=tmp_path / "raw", **kw)


def report_file(day, equipment="ZQX-101", text="Vibration normal"):
    return make_xlsx(dt.date(2999, 1, day), [line(equipment, text, 4.2, "mm/s"), line("Flare", "Pilot lit", hour=9)])


def rows(db, email_prefix="zq-"):
    return db.scalars(select(SourceFile).where(SourceFile.email_id.like(email_prefix + "%")).order_by(SourceFile.received_at, SourceFile.id)).all()


# ---------------------------------------------------------------- the plan's "done when"

def test_new_mail_appears_in_the_database_with_raw_file_and_metadata(db, tmp_path):
    sent_bytes = report_file(1)
    src = FakeSource([
        msg("zq-1", D1, ("report_0101.xlsx", sent_bytes), ("logo.png", b"PNG"), ("notes.pdf", b"%PDF")),
        msg("zq-2", D2, ("report_0102.xlsx", report_file(2))),
    ])
    s = cycle(db, src, tmp_path)
    assert (s.messages, s.attachments, s.ignored, s.imported, s.failed, s.errors) == (2, 2, 2, 2, 0, 0)
    files = rows(db)
    assert [f.file_name for f in files] == ["report_0101.xlsx", "report_0102.xlsx"]
    first = files[0]
    assert first.origin == "outlook" and first.email_id == "zq-1" and first.received_at == D1 and first.status == "parsed"
    stored = Path(first.path)
    assert stored.is_file() and stored.read_bytes() == sent_bytes                  # original kept, outside the repo
    assert stored.parent == tmp_path / "raw" / "outlook" / "2999" / "01"
    rep = db.scalar(select(Report).where(Report.source_file_id == first.id))
    assert str(rep.report_date) == "2999-01-01"
    assert db.scalar(select(func.count()).select_from(ReportLine).where(ReportLine.report_id == rep.id)) == 2
    assert db.scalar(select(func.count()).select_from(RawCells).where(RawCells.source_file_id == first.id)) == 1


def test_sending_the_same_mail_twice_creates_one_report(db, tmp_path):
    data = report_file(1)
    src = FakeSource([msg("zq-1", D1, ("r.xlsx", data))])
    cycle(db, src, tmp_path)
    again = cycle(db, src, tmp_path)
    assert (again.imported, again.skipped_known) == (0, 1)
    att = src.all[0].attachments[0]
    assert att.reads == 1                                                               # known mail+name: not even downloaded

    forwarded = FakeSource([msg("zq-9", D3, ("fwd_report.xlsx", data), subject="FW: Daily report")])
    fwd = cycle(db, forwarded, tmp_path)
    assert (fwd.imported, fwd.duplicates) == (0, 1)                                     # same content, different mail
    assert len(rows(db)) == 1 and db.scalar(select(func.count()).select_from(Report).where(
        Report.source_file_id == rows(db)[0].id)) == 1


def test_lookback_window_and_start_date_setting(db, tmp_path):
    src = FakeSource([msg("zq-1", D2, ("r.xlsx", report_file(2)))])
    cycle(db, src, tmp_path)
    cycle(db, src, tmp_path)
    assert src.last_since == D2 - live.DEFAULT_OVERLAP                                  # re-lists the last 2 days
    set_setting("live_import_since", "2999-01-02", db)
    first = FakeSource([])
    cycle(db, first, tmp_path)
    assert first.last_since >= dt.datetime(2999, 1, 2, tzinfo=UTC)                      # never reads older mail than the setting
    set_setting("live_import_since", "not a date", db)
    cycle(db, FakeSource([]), tmp_path)                                                 # bad value is ignored, not fatal


# ---------------------------------------------------------------- failures

def test_unparseable_and_damaged_attachments_are_queued_not_lost(db, tmp_path):
    src = FakeSource([msg("zq-1", D1, ("unknown.xlsx", _unknown_xlsx()), ("broken.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 200))])
    s = cycle(db, src, tmp_path)
    assert (s.imported, s.failed, s.errors) == (0, 2, 0)
    unknown, broken = rows(db)
    assert unknown.status == broken.status == "failed"
    reasons = {f.file_name: db.scalar(select(FailedParseQueue.reason).where(FailedParseQueue.source_file_id == f.id)) for f in (unknown, broken)}
    assert reasons == {"unknown.xlsx": "no_template_match", "broken.xls": "unreadable_file"}
    assert Path(unknown.path).is_file()                                                  # original still stored
    assert db.scalar(select(func.count()).select_from(RawCells).where(RawCells.source_file_id == unknown.id)) == 1


def _unknown_xlsx() -> bytes:
    import io, openpyxl
    wb = openpyxl.Workbook()
    wb.active.title = "Something else"
    wb.active["A1"] = "leak"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_one_bad_attachment_does_not_stop_the_cycle(db, tmp_path):
    src = FakeSource([msg("zq-1", D1, ("bad.xlsx", RuntimeError("download failed")), ("good.xlsx", report_file(1)))])
    s = cycle(db, src, tmp_path)
    assert (s.imported, s.errors) == (1, 1) and "download failed" in s.error_samples[0]
    assert [f.file_name for f in rows(db)] == ["good.xlsx"]


def test_an_error_while_saving_rolls_back_only_that_attachment(db, tmp_path, monkeypatch):
    real = live.save_outcome

    def flaky(session, sf, outcome, registry=None):
        if sf.file_name == "second.xlsx":
            session.add(sf)
            session.flush()
            raise RuntimeError("database hiccup")
        return real(session, sf, outcome, registry)

    monkeypatch.setattr(live, "save_outcome", flaky)
    src = FakeSource([msg("zq-1", D1, ("first.xlsx", report_file(1)), ("second.xlsx", report_file(2)), ("third.xlsx", report_file(3)))])
    s = cycle(db, src, tmp_path)
    assert (s.imported, s.errors) == (2, 1)
    assert [f.file_name for f in rows(db)] == ["first.xlsx", "third.xlsx"]              # the half-saved one was rolled back
    monkeypatch.setattr(live, "save_outcome", real)
    assert cycle(db, src, tmp_path).imported == 1                                       # next cycle retries it


def test_mailbox_trouble_stops_the_cycle_but_keeps_what_was_imported(db, tmp_path):
    src = FakeSource([msg("zq-1", D1, ("a.xlsx", report_file(1))), msg("zq-2", D2, ("b.xlsx", report_file(2)))],
                     fail_after=1, error=MailSourceError("connection reset"))
    with pytest.raises(MailSourceError):
        cycle(db, src, tmp_path)
    assert [f.file_name for f in rows(db)] == ["a.xlsx"]


def test_same_file_name_twice_in_one_mail_keeps_both(db, tmp_path):
    src = FakeSource([msg("zq-1", D1, ("report.xlsx", report_file(1)), ("report.xlsx", report_file(2)))])
    s = cycle(db, src, tmp_path)
    assert s.imported == 2 and sorted(f.file_name for f in rows(db)) == ["report.xlsx", "report_2.xlsx"]
    assert cycle(db, src, tmp_path).skipped_known == 2


def test_importer_never_creates_alerts(db, tmp_path):
    before = db.scalar(select(func.count()).select_from(AlertEvent))
    cycle(db, FakeSource([msg("zq-1", D1, ("a.xlsx", report_file(1, text="Leak detected")))]), tmp_path)
    assert db.scalar(select(func.count()).select_from(AlertEvent)) == before


# ---------------------------------------------------------------- end to end with .eml files

def test_eml_folder_end_to_end(db, tmp_path):
    from tests.test_mail_sources import write_eml
    from ingestion_engine.mail.eml import EmlFolderSource

    mails = tmp_path / "mails"
    mails.mkdir()
    write_eml(mails, "1.eml", D1, [("r1.xlsx", report_file(1)), ("sig.png", b"x")], message_id="<zq-e1@x>")
    write_eml(mails, "2.eml", D2, [("r2.xlsx", report_file(2))], message_id="<zq-e2@x>")
    s = cycle(db, EmlFolderSource(mails), tmp_path)
    assert (s.messages, s.imported, s.ignored) == (2, 2, 1)
    assert cycle(db, EmlFolderSource(mails), tmp_path).imported == 0


# ---------------------------------------------------------------- bookkeeping with real commits

@pytest.fixture()
def committed_cleanup():
    with session_scope() as s:
        before_setting = s.get(AppSetting, "live_import_last_ok_at")
        before_value = before_setting.value if before_setting else None
        max_job = s.scalar(select(func.max(JobRun.id))) or 0
    yield
    with session_scope() as s:
        ids = s.scalars(select(SourceFile.id).where(SourceFile.email_id.like("zq-live-%"))).all()
        if ids:
            s.execute(delete(Report).where(Report.source_file_id.in_(ids)))
            s.execute(delete(SourceFile).where(SourceFile.id.in_(ids)))
        s.execute(delete(JobRun).where(JobRun.job_type == "live_import", JobRun.id > max_job))
        if before_value is None:
            s.execute(delete(AppSetting).where(AppSetting.key == "live_import_last_ok_at"))
        else:
            s.get(AppSetting, "live_import_last_ok_at").value = before_value


def test_run_once_updates_heartbeat_and_logs_only_activity(committed_cleanup, tmp_path, monkeypatch):
    monkeypatch.setenv("RAW_STORE_PATH", str(tmp_path / "raw"))
    with session_scope() as s:
        jobs_before = s.scalar(select(func.count()).select_from(JobRun).where(JobRun.job_type == "live_import"))
    src = FakeSource([msg("zq-live-1", D1, ("a.xlsx", report_file(1)))])
    s1 = run_once(src, REG)
    assert s1.imported == 1
    quiet = run_once(src, REG)                                                          # nothing new
    assert not quiet.active
    with session_scope() as s:
        jobs = s.scalars(select(JobRun).where(JobRun.job_type == "live_import").order_by(JobRun.id)).all()
        assert len(jobs) == jobs_before + 1 and jobs[-1].status == "success" and jobs[-1].details["imported"] == 1
        stamp = dt.datetime.fromisoformat(get_setting("live_import_last_ok_at", "", s))
        assert (dt.datetime.now(UTC) - stamp).total_seconds() < 60


def test_run_once_records_a_failed_job_when_the_mailbox_is_down(committed_cleanup):
    src = FakeSource([msg("zq-live-1", D1, ("a.xlsx", b"x"))], fail_after=0, error=MailSourceError("mailbox down"))
    with pytest.raises(MailSourceError):
        run_once(src, REG)
    with session_scope() as s:
        job = s.scalar(select(JobRun).where(JobRun.job_type == "live_import").order_by(JobRun.id.desc()))
        assert job.status == "failed" and "mailbox down" in job.error


def test_loop_survives_every_kind_of_error_in_once_mode(committed_cleanup, caplog):
    caplog.set_level(logging.ERROR)
    for exc in (MailConfigError("folder not found"), MailSourceError("down"), RuntimeError("bug")):
        def boom(exc=exc):
            raise exc
        assert run_forever(once=True, source_factory=boom) == 0
    assert "configuration problem" in caplog.text and "not reachable" in caplog.text and "unexpected error" in caplog.text
