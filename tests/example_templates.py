"""SYNTHETIC templates used only by the tests.

They show how a template is written and prove the engine works. They do NOT describe your
real reports: real templates are written after Phase 0 (see docs/adding_a_template.md).
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from parser_engine.contract import ParsedLine, ParsedMetric, ParsedReport
from parser_engine.helpers import (
    find_cell, label_value, parse_date_ex, read_table, text_of, to_number,
)
from parser_engine.registry import Registry, Template
from parser_engine.workbook import Workbook


def _lines_from_table(rows, mapping) -> tuple[list[ParsedLine], list[tuple[int, dict]]]:
    lines = []
    for idx, (_, rec) in enumerate(rows, start=1):
        extra: dict[str, Any] = {}
        num = to_number(rec.get(mapping["value"]))
        raw_val = rec.get(mapping["value"])
        if num is None and raw_val is not None:
            extra["reading"] = text_of(raw_val)
        if rec.get("_other"):
            extra["other_cells"] = {str(c): text_of(v) for c, v in rec["_other"].items()}
        lines.append(
            ParsedLine(
                line_no=idx,
                section=text_of(rec.get(mapping["section"])) or None,
                label=text_of(rec.get(mapping["label"])) or None,
                text=text_of(rec.get(mapping["text"])) or None,
                numeric_value=num,
                unit=text_of(rec.get(mapping["unit"])) or None,
                extra=extra or None,
            )
        )
    return lines, rows


class Legacy12H(Template):
    """Era 1 (synthetic): labels in column A, table Section|Item|Reading|Unit|Remarks."""

    name = "example_12h_legacy"
    report_type = "12H"
    era = "legacy"
    COLS = {"section": r"^section", "item": r"^item", "reading": r"^reading", "unit": r"^unit", "remarks": r"^remarks"}

    def detect(self, wb: Workbook) -> float:
        sheet = wb.get_sheet("Daily Report")
        score = 0.0
        if sheet is None:
            return 0.0
        score += 0.4
        if find_cell(sheet, r"12 HOUR OPERATIONS REPORT"):
            score += 0.3
        rows, _ = read_table(sheet, self.COLS)
        if rows:
            score += 0.3
        return score

    def parse(self, wb: Workbook) -> ParsedReport:
        sheet = wb.get_sheet("Daily Report")
        rep = ParsedReport(report_type=self.report_type, template_name=self.name)
        d = parse_date_ex(label_value(sheet, "date"), dayfirst=True)
        rep.report_date = d.value
        if d.ambiguous:
            rep.warnings.append("report date is ambiguous (day/month order assumed day-first)")
        rep.shift = text_of(label_value(sheet, "shift")) or None
        rep.header = {"superintendent": text_of(label_value(sheet, "superintendent")) or None}
        rows, _ = read_table(sheet.filled(), self.COLS)  # filled(): merged section cells
        rep.lines, _ = _lines_from_table(
            rows, {"section": "section", "label": "item", "text": "remarks", "value": "reading", "unit": "unit"}
        )
        for ln in rep.lines:
            if ln.numeric_value is not None and ln.unit:
                rep.metrics.append(ParsedMetric("reading", ln.numeric_value, ln.label, ln.unit))
        return rep


class Modern12H(Template):
    """Era 2 (synthetic): sheet '12H Report', table Time|Equipment|Description|Value|Unit."""

    name = "example_12h_modern"
    report_type = "12H"
    era = "modern"
    COLS = {"time": r"^time", "equipment": r"^equipment", "description": r"^description", "value": r"^value", "unit": r"^unit"}

    def detect(self, wb: Workbook) -> float:
        score = 0.0
        sheet = wb.get_sheet("12H Report")
        if sheet is None:
            return 0.0
        score += 0.5
        rows, _ = read_table(sheet, self.COLS)
        if rows:
            score += 0.5
        return score

    def parse(self, wb: Workbook) -> ParsedReport:
        sheet = wb.get_sheet("12H Report")
        rep = ParsedReport(report_type=self.report_type, template_name=self.name)
        rep.report_date = parse_date_ex(label_value(sheet, "report date")).value
        rep.shift = text_of(label_value(sheet, "shift")) or None
        rep.header = {"superintendent": text_of(label_value(sheet, "duty superintendent")) or None}
        rows, _ = read_table(sheet.filled(), self.COLS)
        rep.lines, _ = _lines_from_table(
            rows, {"section": "time", "label": "equipment", "text": "description", "value": "value", "unit": "unit"}
        )
        for (_, rec), ln in zip(rows, rep.lines):
            if ln.numeric_value is not None:
                t = rec.get("time")
                at = dt.datetime.combine(rep.report_date, t) if isinstance(t, dt.time) and rep.report_date else None
                rep.metrics.append(ParsedMetric(ln.text or "value", ln.numeric_value, ln.label, ln.unit, at))
        return rep


class Exploding(Template):
    """Detects everything, then fails: used to test parser_error handling."""

    name = "example_exploding"
    report_type = "12H"

    def detect(self, wb: Workbook) -> float:
        return 0.9 if wb.get_sheet("Boom") else 0.0

    def parse(self, wb: Workbook) -> ParsedReport:
        raise RuntimeError("deliberate failure")


def make_registry() -> Registry:
    reg = Registry()
    for t in (Legacy12H(), Modern12H(), Exploding()):
        reg.add(t)
    return reg
