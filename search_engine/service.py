"""Search service: the only place Python code queries reports for searching and viewing.

Search itself is the PostgreSQL function search_report_lines (migration 0002). The future .NET
Report plugin calls the same function, so both sides always give the same results.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from database.models import (
    FailedParseQueue, Report, ReportLine, ReportType, SourceFile,
)

from .highlight import highlight_html, query_terms

MODES = ("fulltext", "partial")
MAX_LIMIT = 200


@dataclass
class SearchFilters:
    query: str = ""
    report_type: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    equipment: str | None = None
    mode: str = "fulltext"  # fulltext: whole words and tags (K-101) | partial: substrings and typos


@dataclass
class Hit:
    line_id: int
    report_id: int
    report_date: date
    report_type: str
    shift: str | None
    line_no: int
    section: str | None
    label: str | None
    text: str | None
    numeric_value: Any
    unit: str | None
    rank: float
    section_html: str = ""
    label_html: str = ""
    text_html: str = ""  # HTML-escaped, with <mark> around matches: safe to put in a page


@dataclass
class SearchPage:
    hits: list[Hit] = field(default_factory=list)
    limit: int = 50
    offset: int = 0
    has_more: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def search(session: Session, f: SearchFilters, limit: int = 50, offset: int = 0) -> SearchPage:
    if f.mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if f.date_from and f.date_to and f.date_from > f.date_to:
        raise ValueError("date_from is after date_to")
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    rows = session.execute(
        text(
            "SELECT * FROM report.search_report_lines("
            "CAST(:q AS text), CAST(:mode AS text), CAST(:rt AS text), CAST(:df AS date), "
            "CAST(:dt AS date), CAST(:eq AS text), :lim, :off)"
        ),
        {"q": _clean(f.query), "mode": f.mode, "rt": _clean(f.report_type), "df": f.date_from,
         "dt": f.date_to, "eq": _clean(f.equipment), "lim": limit + 1, "off": offset},
    ).mappings().all()  # one extra row tells us whether another page exists
    terms = query_terms(f.query)
    page = SearchPage(limit=limit, offset=offset, has_more=len(rows) > limit)
    for r in rows[:limit]:
        h = Hit(**dict(r))
        h.section_html = highlight_html(h.section, terms)
        h.label_html = highlight_html(h.label, terms)
        h.text_html = highlight_html(h.text, terms)
        page.hits.append(h)
    return page


# ---------------------------------------------------------------- reports

def get_report(session: Session, report_id: int) -> dict | None:
    rep = session.get(Report, report_id)
    if rep is None:
        return None
    rt = session.get(ReportType, rep.report_type_id)
    sf = session.get(SourceFile, rep.source_file_id)
    header = {k: v for k, v in (rep.header or {}).items() if not k.startswith("_")}
    parser = (rep.header or {}).get("_parser", {})
    lines = session.scalars(
        select(ReportLine).where(ReportLine.report_id == report_id).order_by(ReportLine.line_no)
    ).all()
    return {
        "id": rep.id, "report_type": rt.name, "report_date": rep.report_date, "shift": rep.shift,
        "header": header, "warnings": parser.get("warnings", []),
        "source": {"file_name": sf.file_name, "origin": sf.origin, "path": sf.path, "received_at": sf.received_at},
        "lines": [
            {"id": l.id, "line_no": l.line_no, "section": l.section, "label": l.label, "text": l.text,
             "numeric_value": l.numeric_value, "unit": l.unit, "extra": l.extra}
            for l in lines
        ],
    }


def get_context(session: Session, line_id: int, n: int = 2) -> list[dict]:
    """The lines around a hit (same report), so a keyword is read in context."""
    line = session.get(ReportLine, line_id)
    if line is None:
        return []
    n = max(0, min(int(n), 20))
    rows = session.scalars(
        select(ReportLine)
        .where(ReportLine.report_id == line.report_id, ReportLine.line_no.between(line.line_no - n, line.line_no + n))
        .order_by(ReportLine.line_no)
    ).all()
    return [{"id": l.id, "line_no": l.line_no, "section": l.section, "label": l.label, "text": l.text,
             "numeric_value": l.numeric_value, "unit": l.unit, "is_hit": l.id == line_id} for l in rows]


def list_reports(session: Session, report_type: str | None = None, date_from: date | None = None,
                 date_to: date | None = None, limit: int = 50, offset: int = 0) -> dict:
    limit = max(1, min(int(limit), MAX_LIMIT))
    q = (
        select(Report.id, Report.report_date, Report.shift, Report.header,
               ReportType.name.label("report_type"), SourceFile.file_name,
               func.count(ReportLine.id).label("lines"))
        .join(ReportType, ReportType.id == Report.report_type_id)
        .join(SourceFile, SourceFile.id == Report.source_file_id)
        .join(ReportLine, ReportLine.report_id == Report.id, isouter=True)
        .group_by(Report.id, Report.header, ReportType.name, SourceFile.file_name)
        .order_by(Report.report_date.desc(), Report.id.desc())
        .limit(limit + 1).offset(max(0, int(offset)))
    )
    if _clean(report_type):
        q = q.where(ReportType.name == report_type)
    if date_from:
        q = q.where(Report.report_date >= date_from)
    if date_to:
        q = q.where(Report.report_date <= date_to)
    rows = [dict(r) for r in session.execute(q).mappings().all()]
    return {"reports": rows[:limit], "limit": limit, "offset": max(0, int(offset)), "has_more": len(rows) > limit}


# ---------------------------------------------------------------- numbers for the summary cards

def get_report_types(session: Session) -> list[dict]:
    rows = session.execute(
        select(ReportType.name, ReportType.frequency, func.count(Report.id))
        .join(Report, Report.report_type_id == ReportType.id, isouter=True)
        .where(ReportType.is_active.is_(True))
        .group_by(ReportType.id)
        .order_by(ReportType.id)
    ).all()
    return [{"name": n, "frequency": f, "reports": c} for n, f, c in rows]


def get_stats(session: Session) -> dict:
    total = session.scalar(select(func.count()).select_from(Report)) or 0
    lines = session.scalar(select(func.count()).select_from(ReportLine)) or 0
    first, last = session.execute(select(func.min(Report.report_date), func.max(Report.report_date))).one()
    failed = session.scalar(
        select(func.count()).select_from(FailedParseQueue).where(FailedParseQueue.resolved.is_(False))
    ) or 0
    last_import: datetime | None = session.scalar(select(func.max(SourceFile.ingested_at)))
    return {
        "reports": total, "lines": lines, "first_report_date": first, "last_report_date": last,
        "failed_files": failed, "last_import_at": last_import,
        "by_type": get_report_types(session),
    }
