"""Phase 2 tests for the parser engine (no database needed)."""
import datetime as dt
import json
import os
import shutil
from pathlib import Path

import pytest

from parser_engine.contract import (
    REASON_NO_TEMPLATE, REASON_PARSER_ERROR, REASON_PASSWORD, REASON_UNREADABLE,
    REASON_UNSUPPORTED, REASON_VALIDATION,
)
from parser_engine.helpers import parse_date, parse_date_ex, to_number
from parser_engine.pipeline import outcome_to_dict, parse_file
from parser_engine.registry import Registry
from parser_engine.workbook import WorkbookError, _classify_error, detect_format, load_workbook
from tests.example_templates import Legacy12H, Modern12H, make_registry

SAMPLES = Path(__file__).parent / "samples"
GOLDEN = Path(__file__).parent / "golden"
REG = make_registry()


def check_golden(name: str, data: dict) -> None:
    path = GOLDEN / f"{name}.json"
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"golden file {path.name} missing: run with UPDATE_GOLDEN=1 and review it"
    assert path.read_text(encoding="utf-8") == text


# ---------------------------------------------------------------- golden: one per era

def test_legacy_xls_matches_golden():
    out = parse_file(SAMPLES / "legacy_12h_2013-01-05.xls", REG)
    assert out.ok, out.failure
    check_golden("legacy_12h_2013-01-05", outcome_to_dict(out)["report"])


def test_modern_xlsx_matches_golden():
    out = parse_file(SAMPLES / "modern_12h_2024-03-10.xlsx", REG)
    assert out.ok, out.failure
    check_golden("modern_12h_2024-03-10", outcome_to_dict(out)["report"])


# ---------------------------------------------------------------- what the plan promises

def test_twelve_line_report_gives_twelve_lines_with_merged_sections_filled():
    rep = parse_file(SAMPLES / "legacy_12h_2013-01-05.xls", REG).report
    assert len(rep.lines) == 12
    assert [ln.line_no for ln in rep.lines] == list(range(1, 13))
    assert all(ln.section for ln in rep.lines)  # merged section cells were filled down
    assert rep.lines[3].extra == {"reading": "STANDBY"}  # non-numeric reading is kept, not lost


def test_date_comes_from_content_and_ambiguity_is_flagged():
    rep = parse_file(SAMPLES / "legacy_12h_2013-01-05.xls", REG).report
    assert rep.report_date == dt.date(2013, 1, 5)
    assert any("ambiguous" in w for w in rep.warnings)


def test_wrong_extension_is_read_by_content(monkeypatch):
    # must work WITHOUT LibreOffice: it is optional on the server
    monkeypatch.setattr("parser_engine.workbook._convert_with_libreoffice", lambda p: None)
    path = SAMPLES / "misnamed_modern.xls"
    assert detect_format(path) == "xlsx"
    out = parse_file(path, REG)
    assert out.ok and out.template_name == "example_12h_modern"
    assert any("does not match content" in w for w in out.warnings)
    assert not any("LibreOffice" in w for w in out.warnings)


def test_hidden_sheet_is_kept_in_raw_but_ignored_by_template():
    out = parse_file(SAMPLES / "modern_12h_2024-03-10.xlsx", REG)
    hidden = [s for s in out.raw_sheets if s.hidden]
    assert [s.name for s in hidden] == ["Lists"]
    assert "internal lookup list" in hidden[0].text_dump
    assert len(out.report.lines) == 6


def test_registry_picks_best_template():
    wb = load_workbook(SAMPLES / "modern_12h_2024-03-10.xlsx")
    res = REG.resolve(wb)
    assert res.template.name == "example_12h_modern" and res.confidence == 1.0
    assert REG.resolve(load_workbook(SAMPLES / "legacy_12h_2013-01-05.xls")).template.name == "example_12h_legacy"


def test_ambiguous_match_is_reported():
    reg = Registry()
    reg.add(Legacy12H())

    class Twin(Legacy12H):
        name = "twin"

    reg.add(Twin())
    res = reg.resolve(load_workbook(SAMPLES / "legacy_12h_2013-01-05.xls"))
    assert res.ambiguous


# ---------------------------------------------------------------- failures fail cleanly

def test_unknown_layout_fails_but_keeps_raw_cells():
    out = parse_file(SAMPLES / "unknown_layout.xlsx", REG)
    assert not out.ok and out.failure.reason == REASON_NO_TEMPLATE
    assert out.raw_sheets and "Inspect valve V-9" in out.raw_sheets[0].text_dump


def test_no_templates_registered_fails_cleanly():
    out = parse_file(SAMPLES / "modern_12h_2024-03-10.xlsx", Registry())
    assert out.failure.reason == REASON_NO_TEMPLATE and out.raw_sheets


def test_missing_mandatory_field_is_validation_failure_with_raw_cells():
    out = parse_file(SAMPLES / "legacy_missing_date.xls", REG)
    assert out.failure.reason == REASON_VALIDATION
    assert "report_date" in out.failure.detail
    assert out.raw_sheets


def test_parser_exception_becomes_failure_not_crash():
    out = parse_file(SAMPLES / "exploding_layout.xlsx", REG)
    assert out.failure.reason == REASON_PARSER_ERROR
    assert "deliberate failure" in out.failure.detail
    assert out.raw_sheets


@pytest.mark.parametrize("name,reasons", [
    ("corrupt.xls", {REASON_UNREADABLE}),
    ("truncated.xlsx", {REASON_UNREADABLE}),
])
def test_damaged_files_fail_cleanly(name, reasons):
    out = parse_file(SAMPLES / name, REG)
    assert not out.ok and out.failure.reason in reasons
    assert out.raw_sheets == []


def test_missing_file_and_text_file(tmp_path):
    assert parse_file(tmp_path / "nope.xls", REG).failure.reason == REASON_UNREADABLE
    txt = tmp_path / "notes.xls"
    txt.write_text("plain text, not a spreadsheet")
    assert parse_file(txt, REG).failure.reason in {REASON_UNSUPPORTED, REASON_UNREADABLE}


def test_empty_file(tmp_path):
    p = tmp_path / "empty.xlsx"
    p.write_bytes(b"")
    assert parse_file(p, REG).failure.reason == REASON_UNREADABLE


def test_encrypted_message_maps_to_password_protected():
    err = _classify_error(Exception("Workbook is encrypted"))
    assert err.reason == REASON_PASSWORD


@pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice not installed")
def test_libreoffice_fallback_reads_html_saved_as_xls(tmp_path):
    p = tmp_path / "weird.xls"
    p.write_text("<html><body><table><tr><td>Hello</td><td>42</td></tr></table></body></html>")
    wb = load_workbook(p)
    assert any("LibreOffice" in w for w in wb.warnings)
    assert wb.sheets[0].rows[0][:2] == ["Hello", 42]


# ---------------------------------------------------------------- helpers

@pytest.mark.parametrize("value,expected", [
    (dt.datetime(2013, 1, 5, 6, 0), dt.date(2013, 1, 5)),
    ("2013-01-05", dt.date(2013, 1, 5)),
    ("5-Jan-13", dt.date(2013, 1, 5)),
    ("05 January 2013", dt.date(2013, 1, 5)),
    ("January 5, 2013", dt.date(2013, 1, 5)),
    ("25/12/2013", dt.date(2013, 12, 25)),
    ("12/25/2013", dt.date(2013, 12, 25)),   # only valid month-first
    ("05.01.2013", dt.date(2013, 1, 5)),
    ("2013-01-05 06:00:00", dt.date(2013, 1, 5)),
    (41279, dt.date(2013, 1, 5)),             # Excel serial (as in .xlsb)
    ("not a date", None),
    (None, None),
    (12, None),
])
def test_parse_date(value, expected):
    assert parse_date(value) == expected


def test_parse_date_ambiguity_and_order():
    assert parse_date_ex("03/04/2013").ambiguous
    assert parse_date_ex("03/04/2013").value == dt.date(2013, 4, 3)
    assert parse_date_ex("03/04/2013", dayfirst=False).value == dt.date(2013, 3, 4)
    assert not parse_date_ex("25/04/2013").ambiguous


@pytest.mark.parametrize("value,expected", [
    (5, 5), (4.2, 4.2), ("1,234.5", 1234.5), ("12,5", 12.5), ("45 %", 45), ("n/a", None),
    (None, None), (True, None), ("", None),
])
def test_to_number(value, expected):
    assert to_number(value) == expected
