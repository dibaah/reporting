"""Test helpers for mail: a fake mail source, Excel attachments in the synthetic 12H layout,
and a session factory that joins the test's rolled-back transaction."""
from __future__ import annotations

import datetime as dt
import io
from contextlib import contextmanager

import openpyxl

from ingestion_engine.mail.base import MailAttachment, MailMessage, MailSource

UTC = dt.timezone.utc


def make_xlsx(report_date: dt.date, lines, shift: str = "Day") -> bytes:
    """Workbook in the layout of tests.example_templates.Modern12H. lines = (time, equipment, description, value, unit)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "12H Report"
    ws["A2"], ws["B2"] = "Report date", dt.datetime.combine(report_date, dt.time())
    ws["A3"], ws["B3"] = "Shift", shift
    ws["A4"], ws["B4"] = "Duty superintendent", "Test Person"
    for c, h in enumerate(["Time", "Equipment", "Description", "Value", "Unit"], start=1):
        ws.cell(row=6, column=c, value=h)
    for r, row in enumerate(lines, start=7):
        for c, v in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def line(equipment, description, value=None, unit=None, hour=6):
    return (dt.time(hour, 0), equipment, description, value, unit)


class FakeAttachment(MailAttachment):
    def __init__(self, name, data: bytes | Exception):
        self.reads = 0
        self._data = data
        super().__init__(name, len(data) if isinstance(data, bytes) else None, self._read)

    def _read(self):
        self.reads += 1
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


def msg(mid, received: dt.datetime, *attachments, subject="Daily report"):
    return MailMessage(mid, subject, received, "ops@example.com",
                       [FakeAttachment(n, d) for n, d in attachments])


class FakeSource(MailSource):
    def __init__(self, messages, fail_after: int | None = None, error: Exception | None = None):
        self.all = list(messages)
        self.last_since = "never called"
        self.fail_after, self.error = fail_after, error

    def messages(self, since):
        self.last_since = since
        for i, m in enumerate(sorted(self.all, key=lambda m: m.received_at)):
            if self.fail_after is not None and i >= self.fail_after:
                raise self.error
            if since is None or m.received_at >= since:
                yield m


def savepoint_factory(session):
    """One 'transaction' per call, as in production, but inside the test's outer transaction."""
    @contextmanager
    def factory():
        with session.begin_nested():
            yield session
    return factory
