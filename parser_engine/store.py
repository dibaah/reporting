"""Save a ParseOutcome to the database (used by the importers in Phase 3 and 5).

Idempotent per source file: saving again replaces the raw cells and the report, so a
file can be re-parsed after a template fix without duplicates.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from database.models import (
    FailedParseQueue, RawCells, Report, ReportLine, ReportMetric, ReportTemplate,
    ReportType, SourceFile,
)

from .contract import REASON_UNKNOWN_TYPE, ParseFailure, ParseOutcome
from .registry import Registry, default_registry


def _ensure_template(session: Session, report_type_id: int, name: str, registry: Registry) -> ReportTemplate:
    row = session.scalar(
        select(ReportTemplate).where(
            ReportTemplate.report_type_id == report_type_id, ReportTemplate.name == name
        )
    )
    if row is None:
        t = registry.get(name)
        row = ReportTemplate(
            report_type_id=report_type_id,
            name=name,
            era=getattr(t, "era", None),
            valid_from=date.fromisoformat(t.valid_from) if getattr(t, "valid_from", None) else None,
            valid_to=date.fromisoformat(t.valid_to) if getattr(t, "valid_to", None) else None,
            parser_name=name,
        )
        session.add(row)
        session.flush()
    return row


def _record_failure(session: Session, source_file: SourceFile, failure: ParseFailure) -> None:
    now = datetime.now(timezone.utc)
    open_row = session.scalar(
        select(FailedParseQueue).where(
            FailedParseQueue.source_file_id == source_file.id, FailedParseQueue.resolved.is_(False)
        )
    )
    if open_row is None:
        session.add(
            FailedParseQueue(
                source_file_id=source_file.id, reason=failure.reason,
                error_detail=failure.detail[:4000], retry_count=0, last_attempt_at=now,
            )
        )
    else:
        open_row.reason = failure.reason
        open_row.error_detail = failure.detail[:4000]
        open_row.retry_count += 1
        open_row.last_attempt_at = now
    source_file.status = "failed"


def save_outcome(
    session: Session, source_file: SourceFile, outcome: ParseOutcome, registry: Registry | None = None
) -> None:
    """Write raw cells always; write the report if parsing succeeded, else queue the failure.
    The caller owns the transaction (one transaction per file)."""
    registry = registry or default_registry
    if source_file.id is None:
        session.add(source_file)
        session.flush()

    session.execute(delete(RawCells).where(RawCells.source_file_id == source_file.id))
    session.execute(delete(Report).where(Report.source_file_id == source_file.id))
    for sh in outcome.raw_sheets:
        session.add(
            RawCells(
                source_file_id=source_file.id, sheet_name=sh.name, sheet_index=sh.index,
                is_hidden=sh.hidden, grid=sh.grid, text_dump=sh.text_dump,
            )
        )

    failure = outcome.failure
    if outcome.ok:
        rt = session.scalar(select(ReportType).where(ReportType.name == outcome.report.report_type))
        if rt is None:
            failure = ParseFailure(
                REASON_UNKNOWN_TYPE, f"report type {outcome.report.report_type!r} is not in report_types"
            )
    if failure is not None:
        _record_failure(session, source_file, failure)
        return

    rep = outcome.report
    tpl = _ensure_template(session, rt.id, rep.template_name, registry)
    header = dict(rep.header)
    header["_parser"] = {
        "template": rep.template_name, "confidence": outcome.confidence,
        "file_format": outcome.file_format, "warnings": rep.warnings,
    }
    report = Report(
        report_type_id=rt.id, template_id=tpl.id, source_file_id=source_file.id,
        report_date=rep.report_date, shift=rep.shift, header=header,
    )
    report.lines = [
        ReportLine(
            line_no=ln.line_no, section=ln.section, label=ln.label, text=ln.text,
            numeric_value=ln.numeric_value, unit=ln.unit, extra=ln.extra,
        )
        for ln in rep.lines
    ]
    session.add(report)
    session.flush()
    session.add_all(
        ReportMetric(
            report_id=report.id, equipment=m.equipment, metric=m.metric, value=m.value,
            unit=m.unit, measured_at=m.measured_at,
        )
        for m in rep.metrics
    )
    source_file.status = "parsed"
    for row in session.scalars(
        select(FailedParseQueue).where(
            FailedParseQueue.source_file_id == source_file.id, FailedParseQueue.resolved.is_(False)
        )
    ):
        row.resolved = True
