"""parse_file(): open a workbook, pick a template, parse, validate. Never raises.

Raw cells are returned whenever the workbook could be opened, even if no template matched
or the parser failed, so keyword scans and re-parsing never depend on a parser.
"""
from __future__ import annotations

import json
import traceback
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contract import (
    REASON_NO_TEMPLATE, REASON_PARSER_ERROR, REASON_VALIDATION,
    ParseFailure, ParseOutcome, ParsedReport, RawSheet,
)
from .registry import Registry, default_registry
from .workbook import Workbook, WorkbookError, load_workbook


def _jsonable(v: Any) -> Any:
    """Make a cell value safe for JSONB (dates, times, durations, decimals)."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    return str(v)


def build_raw_sheets(wb: Workbook) -> list[RawSheet]:
    out: list[RawSheet] = []
    for s in wb.sheets:
        grid = [[_jsonable(v) for v in row] for row in s.rows]
        lines = []
        for row in grid:
            cells = [str(v) for v in row if v is not None]
            if cells:
                lines.append(" | ".join(cells))
        out.append(RawSheet(s.index, s.name, s.hidden, grid, "\n".join(lines)))
    return out


def validate(report: ParsedReport) -> str | None:
    """Return an error message if mandatory fields are missing, else None."""
    problems = []
    if not report.report_type:
        problems.append("report_type is empty")
    if report.report_date is None:
        problems.append("report_date could not be read from the report")
    if not report.lines:
        problems.append("no lines were extracted")
    seen: set[int] = set()
    for ln in report.lines:
        if ln.line_no in seen:
            problems.append(f"duplicate line_no {ln.line_no}")
            break
        seen.add(ln.line_no)
    return "; ".join(problems) or None


def parse_file(path: str | Path, registry: Registry | None = None) -> ParseOutcome:
    registry = registry or default_registry
    outcome = ParseOutcome()
    try:
        wb = load_workbook(path)
    except WorkbookError as e:
        outcome.failure = ParseFailure(e.reason, e.detail)
        return outcome
    except Exception as e:  # last-resort guard: a bad file must never stop an import run
        outcome.failure = ParseFailure("unreadable_file", f"{type(e).__name__}: {e}")
        return outcome

    outcome.file_format = wb.file_format
    outcome.warnings.extend(wb.warnings)
    outcome.raw_sheets = build_raw_sheets(wb)
    if any(s.truncated for s in wb.sheets):
        outcome.warnings.append("a sheet exceeded the cell limit and its raw grid was truncated")

    res = registry.resolve(wb)
    outcome.confidence = res.confidence
    if res.template is None:
        detail = ", ".join(f"{k}={v:.2f}" for k, v in sorted(res.scores.items(), key=lambda kv: -kv[1])[:5])
        outcome.failure = ParseFailure(
            REASON_NO_TEMPLATE,
            f"no template reached {registry.min_confidence:.2f}; scores: {detail or 'no templates registered'}; "
            f"sheets: {wb.sheet_names()}",
        )
        return outcome
    outcome.template_name = res.template.name
    if res.ambiguous:
        outcome.warnings.append("more than one template matched with similar confidence")

    try:
        report = res.template.parse(wb)
    except Exception as e:
        outcome.failure = ParseFailure(
            REASON_PARSER_ERROR,
            f"{res.template.name}: {type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}",
        )
        return outcome

    problem = validate(report)
    report.warnings = list(report.warnings) + outcome.warnings
    if problem:
        outcome.failure = ParseFailure(REASON_VALIDATION, f"{res.template.name}: {problem}")
        outcome.report = None
        return outcome
    outcome.report = report
    return outcome


def outcome_to_dict(o: ParseOutcome) -> dict:
    return {
        "ok": o.ok,
        "file_format": o.file_format,
        "template": o.template_name,
        "confidence": o.confidence,
        "failure": None if o.failure is None else {"reason": o.failure.reason, "detail": o.failure.detail},
        "warnings": o.warnings,
        "sheets": [{"name": s.name, "hidden": s.hidden, "rows": len(s.grid)} for s in o.raw_sheets],
        "report": o.report.to_dict() if o.report else None,
    }


def outcome_json(o: ParseOutcome) -> str:
    return json.dumps(outcome_to_dict(o), indent=2, ensure_ascii=False, default=str)
