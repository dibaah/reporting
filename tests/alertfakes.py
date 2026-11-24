"""Helpers for alert tests: a recording notifier and builders for Outlook reports."""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from alert_service.notify import Notification, Notifier, NotifyError
from database.models import (
    FailedParseQueue, KeywordRule, RawCells, Report, ReportLine, ReportType, SourceFile,
)

UTC = dt.timezone.utc
CUTOFF = dt.datetime(2999, 1, 1, tzinfo=UTC)


class FakeNotifier(Notifier):
    def __init__(self, fail: set[str] | None = None):
        self.sent: list[Notification] = []
        self.fail = fail if fail is not None else set()  # channels that currently fail

    def send(self, n: Notification) -> None:
        if n.channel in self.fail:
            raise NotifyError(f"{n.channel} is down")
        self.sent.append(n)

    def by_channel(self, ch):
        return [n for n in self.sent if n.channel == ch]


def only_my_rules(db: Session) -> None:
    """Real keyword rules (if any) must not interfere with a test: switch them off inside the test transaction."""
    db.execute(update(KeywordRule).values(is_active=False))


def rule(db: Session, keyword, severity="warning", match="word", channel="email") -> KeywordRule:
    r = KeywordRule(keyword=keyword, match_type=match, severity=severity, channel=channel)
    db.add(r)
    db.flush()
    return r


def outlook_report(db: Session, received: dt.datetime, lines, *, rtype="12H", origin="outlook", shift="Day",
                   report_date: dt.date | None = None) -> tuple[SourceFile, Report]:
    """lines: (section, label, text). A parsed report of an Outlook (or archive) file."""
    rt = db.scalar(select(ReportType).where(ReportType.name == rtype))
    sf = SourceFile(origin=origin, file_name=f"zq_{uuid.uuid4().hex[:6]}.xlsx", sha256=uuid.uuid4().hex * 2,
                    received_at=received, status="parsed", email_id="zq-alert")
    db.add(sf)
    db.flush()
    rep = Report(report_type_id=rt.id, source_file_id=sf.id, report_date=report_date or received.date(), shift=shift, header={})
    rep.lines = [ReportLine(line_no=i, section=s, label=l, text=t) for i, (s, l, t) in enumerate(lines, 1)]
    db.add(rep)
    db.flush()
    return sf, rep


def failed_outlook_file(db: Session, received: dt.datetime, rows, *, origin="outlook", hidden_rows=None,
                        reason="no_template_match") -> SourceFile:
    """A file the parser could not read: only raw cells (and a queue entry) exist."""
    sf = SourceFile(origin=origin, file_name=f"zq_bad_{uuid.uuid4().hex[:6]}.xlsx", sha256=uuid.uuid4().hex * 2,
                    received_at=received, status="failed", email_id="zq-alert")
    db.add(sf)
    db.flush()
    db.add(RawCells(source_file_id=sf.id, sheet_name="Sheet1", sheet_index=0, is_hidden=False, grid=rows, text_dump=""))
    if hidden_rows:
        db.add(RawCells(source_file_id=sf.id, sheet_name="Lists", sheet_index=1, is_hidden=True, grid=hidden_rows, text_dump=""))
    db.add(FailedParseQueue(source_file_id=sf.id, reason=reason, error_detail="test"))
    db.flush()
    return sf
