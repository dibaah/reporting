"""The CTEP Shift Report template: synthetic samples (always) and real files (only if present locally)."""
import datetime as dt
import json
import os
import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alert_service import engine
from database import seed
from database.db import get_engine
from database.models import RawCells, Report, ReportLine
from ingestion_engine.historical import folder_hint, run_import
from ingestion_engine.live import run_cycle
from parser_engine.helpers import parse_date
from parser_engine.pipeline import outcome_to_dict, parse_file
from parser_engine.registry import Registry
from parser_engine.store import save_outcome
from parser_engine.templates.ctep_shift_report import (
    CtepShiftReport, measure, split_items, value_with_unit,
)
from parser_engine.workbook import load_workbook
from search_engine.service import SearchFilters, search
from tests.alertfakes import FakeNotifier, only_my_rules, rule
from tests.mailfakes import FakeSource, msg, savepoint_factory
from tests.example_templates import make_registry
from database.models import SourceFile

SAMPLES = Path(__file__).parent / "samples"
REAL = Path(__file__).parent / "real_samples"
GOLDEN = Path(__file__).parent / "golden"
UTC = dt.timezone.utc


def reg() -> Registry:
    r = Registry()
    r.add(CtepShiftReport())
    return r


def parsed(name, directory=SAMPLES):
    out = parse_file(directory / name, reg())
    assert out.ok, out.failure
    return out


def check_golden(name, data):
    path = GOLDEN / f"{name}.json"
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"{path.name} missing: run with UPDATE_GOLDEN=1 and review it"
    assert path.read_text(encoding="utf-8") == text


SYNTHETIC = ["CWRS-20990105D", "CSR-20990106N", "CSR-20990107D", "CSR-20990108D"]


@pytest.mark.parametrize("name", SYNTHETIC)
def test_synthetic_files_match_golden(name):
    check_golden(f"ctep_{name}", outcome_to_dict(parsed(f"{name}.xlsx"))["report"])


# ---------------------------------------------------------------- what the parser understands

def lines_of(rep, **kw):
    return [l for l in rep.lines if all(getattr(l, k, None) == v or (l.extra or {}).get(k) == v for k, v in kw.items())]


def test_era_b_header_and_identity():
    rep = parsed("CWRS-20990105D.xlsx").report
    assert (rep.report_type, rep.report_date, rep.shift, rep.warnings) == ("12H", dt.date(2099, 1, 5), "Day", [])
    assert rep.header["doc_no"] == "CWRS-20990105D" and rep.header["crew"] == "A" and rep.header["supervisor"] == "T.Supervisor"
    assert rep.header["ccr_members"] == "A.One / B.Two / C.Three" and rep.header["section"] == "CTEP Operation"


def test_era_a_night_shift_and_duty_line():
    out = parsed("CSR-20990106N.xlsx")
    assert out.report.shift == "Night" and out.report.header["duty"] == "A. Tester" and out.report.report_date == dt.date(2099, 1, 6)


def test_wrong_year_in_the_header_is_corrected_and_explained():
    rep = parsed("CSR-20990107D.xlsx").report
    assert rep.report_date == dt.date(2099, 1, 7) and rep.header["date_text"] == "07.Jan.2098"
    assert len(rep.warnings) == 1 and "2098-01-07" in rep.warnings[0] and "2099-01-07 was used" in rep.warnings[0]


def test_readings_become_numbers_and_metrics():
    rep = parsed("CWRS-20990105D.xlsx").report
    hc = lines_of(rep, label="HC No")[0]
    assert (hc.section, hc.numeric_value) == ("Wells & MFs", 25)
    wd = lines_of(rep, label="WD")[0]
    assert wd.numeric_value == 2 and "Injection Press : 128 bar" in wd.text and "Inj Flow rate: 22.5 ton /h" in wd.text
    flare = [l for l in rep.lines if (l.label or "").startswith("HP flare stack drain volume@ 06:00")][0]
    assert (flare.numeric_value, flare.unit) == (200, "lits")
    metrics = {(m.metric, m.equipment): (m.value, m.unit) for m in rep.metrics}
    assert metrics[("Injection Press", "Wells & MFs")] == (128, "bar")
    assert metrics[("S.C", "LC A")] == (26.5, "bar") and metrics[("S.C", "HC")] == (8.45, "bar")
    assert metrics[("tank level", "F W 901A")] == (98.23, None)
    assert metrics[("LC Export Rate", None)] == (70000, "bbl/day") and metrics[("HC Export Rate", None)] == (30000, "bbl/day")


def test_empty_form_labels_do_not_become_lines():
    labels = [l.label or "" for l in parsed("CWRS-20990105D.xlsx").report.lines]
    assert not any("18:00 P.M." in x for x in labels)           # fixed label, nothing reported
    assert not any(x in ("Substations & UPS", "ICSS & HMI issue") for x in labels)


def test_activity_report_is_split_into_items_with_their_areas():
    rep = parsed("CWRS-20990105D.xlsx").report
    items = [l for l in rep.lines if l.extra.get("kind") == "activity"]
    gas = [l for l in items if l.section == "Gas Treatment"]
    assert [l.extra["item_no"] for l in gas] == list(range(1, 9))
    assert "leakage of PRV" in gas[2].text and gas[2].extra["covers"] == ["Gas Treatment", "Utility Systems"]
    first = [l for l in items if l.section == "Wells & MFs"][0]
    assert first.extra["covers"] == ["Wells & MFs", "Oil Treatment"] and first.extra["item_no"] is None
    assert [l.section for l in items].count("GTG & Power Plant") == 1


def test_wr_wo_entries_are_attached_to_the_right_items():
    era_b = parsed("CWRS-20990105D.xlsx").report                 # "3. WO Request / 8. WO Request" sits in another block
    marked = {l.extra["item_no"]: l.extra for l in era_b.lines if l.extra.get("wr_wo")}
    assert set(marked) == {3, 8} and marked[3]["wr_wo_matched_by"] == "item number across the report"
    assert "PRV" in next(l for l in era_b.lines if l.extra.get("item_no") == 3 and l.section == "Gas Treatment").text
    era_a = parsed("CSR-20990106N.xlsx").report                  # "1 - 11503" next to the block's own items
    assert [(l.extra["item_no"], l.extra["wr_wo"]) for l in era_a.lines if l.extra.get("wr_wo")] == [(1, "11503"), (2, "11504")]
    assert not [l for l in era_a.lines if l.label == "WR/WO"]     # nothing left unmatched


def test_gtg_pairs_storage_tanks_and_shutdown():
    rep = parsed("CSR-20990108D.xlsx").report
    gtg = [l for l in rep.lines if l.section == "GTG & Power Plant" and l.label != "Activity"]
    assert [(l.label, l.text) for l in gtg] == [("GTG units in service (fuel type)", "Units 1 , 2 , 3 with FG"), ("Available stand-by units", "Unit 5")]
    tanks = [l for l in rep.lines if l.section == "Storage Tank Level"]
    assert len(tanks) == 11 and {l.extra["group"] for l in tanks} == {"Oil Tanks", "Water Tanks", "Diesel Storage"}
    assert [l.numeric_value for l in tanks if l.label == "OFF Spec"] == [0]
    sd = [l for l in rep.lines if l.extra.get("kind") == "shutdown"]
    assert len(sd) == 1 and sd[0].section == "Shut down" and "Root cause: High vibration" in sd[0].text and "Initiated time: 03:15" in sd[0].text
    assert not [l for l in parsed("CWRS-20990105D.xlsx").report.lines if l.section == "Shut down"]   # empty block: no line


def test_crew_remark_and_line_numbers():
    rep = parsed("CWRS-20990105D.xlsx").report
    assert [l.line_no for l in rep.lines] == list(range(1, len(rep.lines) + 1))
    assert [l.label for l in rep.lines if l.section == "Crew"] == ["CCR members", "Site members"]
    assert rep.lines[-1].section == "Remark" and "Export Rate" in rep.lines[-1].text


def test_detection_is_exclusive():
    t = CtepShiftReport()
    for name in SYNTHETIC:
        assert t.detect(load_workbook(SAMPLES / f"{name}.xlsx")) == 1.0
    for other in ("legacy_12h_2013-01-05.xls", "modern_12h_2024-03-10.xlsx", "unknown_layout.xlsx"):
        assert t.detect(load_workbook(SAMPLES / other)) < 0.6
    assert not parse_file(SAMPLES / "modern_12h_2024-03-10.xlsx", reg()).ok          # example layouts are not mistaken for it


def test_helpers():
    assert measure("HC  No : 25") == ("HC No", 25, None) and measure("S.C: \n26.5 bar") == ("S.C", 26.5, "bar")
    assert measure("Inj Flow rate:  22.5  ton /h") == ("Inj Flow rate", 22.5, "ton /h")
    assert measure("HP flare stack drain volume@ 06:00 A.M.") is None and measure("In Service") is None
    assert value_with_unit("200 lits") == (200, "lits") and value_with_unit(0) == (0, None) and value_with_unit("In Service") is None
    assert split_items("1. a\ncontinued\n2- b\n\n3) c") == [(1, "a continued"), (2, "b"), (3, "c")]
    assert split_items("just text") == [(None, "just text")] and split_items("") == []
    assert parse_date("19.Jan.2019") == dt.date(2019, 1, 19) and parse_date("2019-May-04") == dt.date(2019, 5, 4)
    assert folder_hint(Path("/r/2019/2019-08/x.xlsx"), Path("/r")) == (2019, 8)


# ---------------------------------------------------------------- real files (only on a machine that has them)

REAL_FILES = sorted(REAL.glob("*.xlsx")) if REAL.exists() else []


@pytest.mark.skipif(not REAL_FILES, reason="no real files in tests/real_samples (they are never committed)")
@pytest.mark.parametrize("path", REAL_FILES, ids=[p.name for p in REAL_FILES])
def test_real_files_parse(path):
    out = parse_file(path, reg())
    assert out.ok, out.failure
    rep = out.report
    m = __import__("re").search(r"-(\d{8})([DN])", path.name)
    assert rep.report_date == dt.datetime.strptime(m.group(1), "%Y%m%d").date()       # identity from the name always wins
    assert rep.shift == {"D": "Day", "N": "Night"}[m.group(2)]
    assert 25 <= len(rep.lines) <= 60 and len(rep.metrics) >= 15
    sections = {l.section for l in rep.lines}
    assert {"Wells & MFs", "Oil Treatment", "Gas Treatment", "Utility Systems", "Storage Tank Level", "Remark"} <= sections
    assert any(m.metric == "LC Export Rate" and m.value > 1000 for m in rep.metrics)


# ---------------------------------------------------------------- database, search, import and alerts

@pytest.fixture(scope="module", autouse=True)
def _seed():
    seed.run()


@pytest.fixture()
def db():
    conn = get_engine().connect()
    trans = conn.begin()
    s = Session(bind=conn, expire_on_commit=False)
    only_my_rules(s)
    yield s
    s.close()
    trans.rollback()
    conn.close()


def test_saved_report_is_searchable_line_by_line(db):
    out = parsed("CWRS-20990105D.xlsx")
    sf = SourceFile(origin="archive", file_name="CWRS-20990105D.xlsx", sha256="c" * 64)
    save_outcome(db, sf, out, reg())
    rep = db.scalar(select(Report).where(Report.source_file_id == sf.id))
    assert db.scalar(select(func.count()).select_from(ReportLine).where(ReportLine.report_id == rep.id)) == len(out.report.lines)
    page = search(db, SearchFilters("leakage PRV", date_from=dt.date(2099, 1, 1)))
    hit = next(h for h in page.hits if h.report_id == rep.id)
    assert hit.section == "Gas Treatment" and hit.label == "Activity" and "<mark>leakage</mark>" in hit.text_html
    assert [h.report_id for h in search(db, SearchFilters("S.C", date_from=dt.date(2099, 1, 1), mode="partial")).hits]
    assert rep.header["_parser"]["template"] == "ctep_shift_report" and rep.header["supervisor"] == "T.Supervisor"


def test_historical_dry_run_over_a_year_month_tree(tmp_path):
    root = tmp_path / "Shift Report (CWRS)" / "2099" / "2099-01"
    root.mkdir(parents=True)
    for n in SYNTHETIC:
        shutil.copy(SAMPLES / f"{n}.xlsx", root / f"{n}.xlsx")
    sm = run_import(tmp_path / "Shift Report (CWRS)", dry_run=True, registry=reg(), collect_rows=True)
    assert (sm.total_files, sm.imported, sm.queued_failed, sm.folder_mismatch) == (4, 4, 0, 0)
    assert {r["folder_year"] for r in sm.rows} == {2099} and {r["folder_month"] for r in sm.rows} == {1}


def test_mail_to_alert_chain(db, tmp_path):
    """A shift report arrives by email -> imported -> the keyword in its activity text raises an alert."""
    from alert_service.engine import alert_cutoff
    from ingestion_engine.settings import set_setting

    for k, v in {"alerts_enabled": "true", "alerts_active_from": "2099-01-01T00:00:00+00:00", "timezone": "UTC",
                 "notification_email": "me@example.com"}.items():
        set_setting(k, v, db)
    rule(db, "leakage", "critical")
    data = (SAMPLES / "CWRS-20990105D.xlsx").read_bytes()
    received = dt.datetime(2099, 1, 5, 6, 40, tzinfo=UTC)
    run_cycle(FakeSource([msg("zq-ctep", received, ("CWRS-20990105D.xlsx", data))]), registry=reg(),
              session_factory=savepoint_factory(db), raw_store=tmp_path / "raw")
    sf = db.scalar(select(SourceFile).where(SourceFile.email_id == "zq-ctep"))
    assert sf.status == "parsed"
    n = FakeNotifier()
    out = engine.run_cycle(n, session_factory=savepoint_factory(db), now=dt.datetime(2099, 1, 5, 7, tzinfo=UTC), only_sources=[sf.id])
    assert out.critical_sent == 1
    assert n.sent[0].subject == "[CRITICAL] 12H 2099-01-05 Day: 1 keyword alert"
    assert "due to leakage of PRV" in n.sent[0].text and "> Activity: Suction and discharge" in n.sent[0].text
