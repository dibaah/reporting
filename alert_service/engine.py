"""Alert engine (Phase 6).

Rules that never change (Master Plan section 5):
- FORWARD-ONLY: only Outlook files received at or after alerts_active_from are scanned. Historical
  imports never create alerts, and mail that arrived before alerts were switched on never does either.
- NOTHING DEPENDS ON THE PARSER: if a live report could not be parsed, its raw cells are scanned instead.
- Critical = sent at once (one message per report). Warning = daily digest. Info = recorded only.
- A finding that repeats in later reports is still sent, annotated "seen N times before".
"""
from __future__ import annotations

import hashlib
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Callable, ContextManager, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.db import session_scope
from database.models import (
    AlertEvent, ExpectedReport, FailedParseQueue, JobRun, KeywordRule, RawCells, Report, ReportLine,
    ReportType, SourceFile,
)
from ingestion_engine.settings import get_setting, set_setting

from . import schedule
from .matcher import CompiledRule, RuleError, compile_rule, is_negated
from .notify import (
    AlertView, Notification, Notifier, NotifyConfigError, NotifyError, format_digest, format_report_alerts,
    format_system, split_recipients,
)

log = logging.getLogger("alerts")
UTC = timezone.utc
MAX_ALERTS_PER_FILE = 200
REPEAT_WINDOW = timedelta(days=7)

SessionFactory = Callable[[], ContextManager[Session]]


# ------------------------------------------------------------------ settings

def alert_cutoff(session: Session) -> datetime | None:
    """The forward-only cutoff, or None when alerts are switched off / were never switched on."""
    if get_setting("alerts_enabled", "false", session).strip().lower() != "true":
        return None
    raw = get_setting("alerts_active_from", "", session)
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def local_tz(session: Session) -> tzinfo:
    return schedule.get_tz(get_setting("timezone", "", session), datetime.now().astimezone().tzinfo)


def recipients(session: Session) -> list[str]:
    return split_recipients(get_setting("notification_email", "", session))


# ------------------------------------------------------------------ scanning

@dataclass
class ScanResult:
    files: int = 0
    alerts: int = 0
    negated: int = 0
    capped: int = 0
    bad_rules: list[str] = field(default_factory=list)


@dataclass
class _Line:
    text: str  # what keywords are matched against
    display: str  # what a human reads in the alert
    line_id: int | None
    raw_ref: dict | None


def load_rules(session: Session) -> tuple[list[CompiledRule], list[str]]:
    rules, bad = [], []
    for r in session.scalars(select(KeywordRule).where(KeywordRule.is_active.is_(True)).order_by(KeywordRule.id)):
        try:
            rules.append(compile_rule(r))
        except RuleError as e:
            bad.append(f"rule #{r.id} {r.keyword!r}: {e}")
            log.warning("skipping keyword rule #%s: %s", r.id, e)
    return rules, bad


def _lines_for(session: Session, sf: SourceFile) -> tuple[list[_Line], int | None]:
    report_id = session.scalar(select(Report.id).where(Report.source_file_id == sf.id))
    out: list[_Line] = []
    if report_id is not None:
        for l in session.scalars(select(ReportLine).where(ReportLine.report_id == report_id).order_by(ReportLine.line_no)):
            parts = [p for p in (l.section, l.label, l.text) if p]
            parts += [str(v) for v in (l.extra or {}).values() if isinstance(v, str) and v]
            disp = f"{l.label}: {l.text}" if l.label and l.text else (l.text or l.label or l.section or "")
            out.append(_Line(" | ".join(parts), disp, l.id, None))
        return out, report_id
    # the parser could not read it: scan the raw cells of every visible sheet instead
    for sheet in session.scalars(select(RawCells).where(RawCells.source_file_id == sf.id, RawCells.is_hidden.is_(False))
                                 .order_by(RawCells.sheet_index)):
        for r, row in enumerate(sheet.grid or []):
            cells = [str(v) for v in row if v is not None]
            if cells:
                txt = " | ".join(cells)
                out.append(_Line(txt, txt, None, {"sheet": sheet.sheet_name, "row": r + 1}))
    return out, None


def _norm(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text.lower())).strip()


def _scan_file(session: Session, sf: SourceFile, rules: list[CompiledRule], now: datetime, res: ScanResult) -> None:
    lines, report_id = _lines_for(session, sf)
    created = 0
    for rule in rules:
        for pos, ln in enumerate(lines):
            hits = rule.find(ln.text)
            if not hits:
                continue
            m = hits[0]
            if rule.severity != "critical" and is_negated(ln.text[:2000], m):
                res.negated += 1
                continue
            if created >= MAX_ALERTS_PER_FILE:
                res.capped += 1
                continue
            ref = f"L{ln.line_id}" if ln.line_id else f"R{ln.raw_ref['sheet']}:{ln.raw_ref['row']}"
            key = hashlib.sha1(f"{rule.id}:{sf.id}:{ref}".encode()).hexdigest()
            if session.scalar(select(AlertEvent.id).where(AlertEvent.dedupe_key == key)) is not None:
                continue
            ctx = []
            for j in range(max(0, pos - 1), min(len(lines), pos + 2)):
                ctx.append(("> " if j == pos else "  ") + lines[j].display[:300])
            rkey = hashlib.sha1(f"{rule.id}|{_norm(ln.text)}".encode()).hexdigest()
            repeats = session.scalar(select(func.count()).select_from(AlertEvent).where(
                AlertEvent.rule_id == rule.id, AlertEvent.repeat_key == rkey, AlertEvent.created_at >= now - REPEAT_WINDOW)) or 0
            session.add(AlertEvent(rule_id=rule.id, report_id=report_id, report_line_id=ln.line_id, source_file_id=sf.id,
                                   raw_ref=ln.raw_ref, matched_text=m.text, context="\n".join(ctx), status="new",
                                   dedupe_key=key, repeat_key=rkey, repeat_count=repeats, created_at=now))
            session.flush()
            created += 1
    res.alerts += created
    sf.alert_scanned_at = now


def scan_pending(session: Session, cutoff: datetime, now: datetime | None = None, *,
                 only: Iterable[int] | None = None, max_files: int = 500) -> ScanResult:
    """Scan Outlook files received at/after `cutoff` that have not been scanned yet."""
    now = now or datetime.now(UTC)
    res = ScanResult()
    rules, res.bad_rules = load_rules(session)
    q = (select(SourceFile).where(SourceFile.origin == "outlook", SourceFile.received_at >= cutoff,
                                  SourceFile.alert_scanned_at.is_(None), SourceFile.status.in_(("parsed", "failed")))
         .order_by(SourceFile.received_at).limit(max_files))
    if only is not None:
        q = q.where(SourceFile.id.in_(list(only)))
    for sf in session.scalars(q).all():
        with session.begin_nested():  # one file = one atomic step: events and the scanned flag together
            _scan_file(session, sf, rules, now, res)
        res.files += 1
    if res.capped:
        log.warning("alert cap of %s per file reached; %s more matches were not recorded", MAX_ALERTS_PER_FILE, res.capped)
    return res


# ------------------------------------------------------------------ delivery

def _views(session: Session, severity: str, only_sources: Iterable[int] | None = None) -> list[tuple[AlertEvent, AlertView]]:
    q = (select(AlertEvent, KeywordRule, SourceFile, Report, ReportType)
         .join(KeywordRule, KeywordRule.id == AlertEvent.rule_id)
         .join(SourceFile, SourceFile.id == AlertEvent.source_file_id, isouter=True)
         .join(Report, Report.id == AlertEvent.report_id, isouter=True)
         .join(ReportType, ReportType.id == Report.report_type_id, isouter=True)
         .where(KeywordRule.severity == severity, AlertEvent.status == "new", AlertEvent.sent_at.is_(None))
         .order_by(AlertEvent.source_file_id, AlertEvent.id))
    if only_sources is not None:
        q = q.where(AlertEvent.source_file_id.in_(list(only_sources)))
    out = []
    for ev, rule, sf, rep, rtype in session.execute(q):
        label = (f"{rtype.name} {rep.report_date}" + (f" {rep.shift}" if rep.shift else "")) if rep else f"unparsed file {sf.file_name if sf else '?'}"
        out.append((ev, AlertView(ev.id, rule.severity, rule.keyword, ev.matched_text, ev.context or "", ev.repeat_count,
                                  label, sf.file_name if sf else "", sf.received_at if sf else None, ev.report_id, rule.channel)))
    return out


def _send(notifier: Notifier, n: Notification) -> str | None:
    try:
        notifier.send(n)
        return None
    except NotifyError as e:
        return str(e)


@dataclass
class DispatchResult:
    sent: int = 0
    messages: int = 0
    errors: list[str] = field(default_factory=list)


def dispatch_critical(session: Session, notifier: Notifier, now: datetime | None = None, *,
                      only_sources: Iterable[int] | None = None) -> DispatchResult:
    """Send every unsent Critical alert at once: one message per report and channel.
    An alert counts as delivered when at least one of its channels worked; if all failed it is retried next cycle."""
    now = now or datetime.now(UTC)
    base = get_setting("report_ui_base_url", "", session)
    res = DispatchResult()
    groups: dict[int | None, list[tuple[AlertEvent, AlertView]]] = {}
    for ev, view in _views(session, "critical", only_sources):
        groups.setdefault(ev.source_file_id, []).append((ev, view))
    for items in groups.values():
        delivered: set[int] = set()
        for channel in ("email", "teams"):
            wanted = [(e, v) for e, v in items if v.channel in (channel, "both")]
            if not wanted:
                continue
            n = format_report_alerts([v for _, v in wanted], base)
            n.channel = channel
            err = _send(notifier, n)
            if err:
                res.errors.append(f"{n.subject}: {err}")
                log.error("critical alert not delivered by %s: %s", channel, err)
            else:
                res.messages += 1
                delivered |= {e.id for e, _ in wanted}
        for ev, _ in items:
            if ev.id in delivered:
                ev.status, ev.sent_at = "sent", now
                res.sent += 1
    return res


def send_digest_if_due(session: Session, notifier: Notifier, now: datetime | None = None, *,
                       only_sources: Iterable[int] | None = None) -> DispatchResult:
    now = now or datetime.now(UTC)
    tz = local_tz(session)
    local_now = now.astimezone(tz)
    try:
        h, m = (int(x) for x in get_setting("daily_digest_time", "08:00", session).split(":"))
    except ValueError:
        h, m = 8, 0
    today = local_now.date().isoformat()
    res = DispatchResult()
    if local_now.time() < time(h, m) or get_setting("last_digest_sent_on", "", session) == today:
        return res
    items = _views(session, "warning", only_sources)
    if items:
        n = format_digest([v for _, v in items], today, get_setting("report_ui_base_url", "", session))
        n.channel = "email"
        err = _send(notifier, n)
        if err:
            res.errors.append(f"digest: {err}")
            log.error("digest not sent: %s", err)
            return res  # try again next cycle
        for ev, _ in items:
            ev.status, ev.sent_at = "sent", now
        res.sent, res.messages = len(items), 1
    set_setting("last_digest_sent_on", today, session)  # also on empty days: no empty emails
    return res


def notify_failed_files(session: Session, notifier: Notifier, cutoff: datetime, now: datetime | None = None, *,
                        only_sources: Iterable[int] | None = None) -> DispatchResult:
    """One warning for each live attachment that could not be parsed (they are still keyword-scanned)."""
    now = now or datetime.now(UTC)
    q = (select(FailedParseQueue, SourceFile).join(SourceFile, SourceFile.id == FailedParseQueue.source_file_id)
         .where(FailedParseQueue.resolved.is_(False), FailedParseQueue.notified_at.is_(None),
                SourceFile.origin == "outlook", SourceFile.received_at >= cutoff).order_by(SourceFile.received_at))
    if only_sources is not None:
        q = q.where(SourceFile.id.in_(list(only_sources)))
    rows = session.execute(q).all()
    res = DispatchResult()
    if not rows:
        return res
    lines = [f"{len(rows)} report file(s) arrived by email but could not be read into the database.",
             "Their text is still searched for keywords, but they are not in the report tables.", ""]
    for fq, sf in rows[:30]:
        lines.append(f"- {sf.file_name}  received {sf.received_at:%Y-%m-%d %H:%M} UTC  reason: {fq.reason}")
    if len(rows) > 30:
        lines.append(f"... and {len(rows) - 30} more")
    lines += ["", "See: python -m ingestion_engine.historical --list-failed  (or the Failed files page later)."]
    n = format_system(f"{len(rows)} report file(s) could not be parsed", lines)
    n.channel = "email"
    err = _send(notifier, n)
    if err:
        res.errors.append(f"failed-files warning: {err}")
        return res
    for fq, _ in rows:
        fq.notified_at = now
    res.sent, res.messages = len(rows), 1
    return res


def notify_missing_reports(session: Session, notifier: Notifier, cutoff: datetime, now: datetime | None = None) -> tuple[int, DispatchResult]:
    """Maintain expected slots, match arrivals, and warn once about slots that are overdue."""
    now = now or datetime.now(UTC)
    tz = local_tz(session)
    schedule.refresh_expected(session, now, cutoff, tz)
    schedule.match_receipts(session)
    try:
        grace = timedelta(minutes=int(get_setting("missing_report_grace_minutes", "120", session)))
    except ValueError:
        grace = timedelta(minutes=120)
    overdue = schedule.overdue_slots(session, now, grace)
    res = DispatchResult()
    if not overdue:
        return 0, res
    lines = [f"{len(overdue)} expected report(s) have not arrived:", ""]
    for e, name in overdue[:40]:
        late = now - e.expected_at
        lines.append(f"- {name} expected {e.expected_at.astimezone(tz):%Y-%m-%d %H:%M} ({late.total_seconds() / 3600:.1f} h overdue)")
    lines += ["", "If it arrives later it is imported normally. Expected times are set with: python -m alert_service.cli schedule"]
    n = format_system(f"{len(overdue)} expected report(s) missing", lines)
    n.channel = "email"
    err = _send(notifier, n)
    if err:
        res.errors.append(f"missing-report warning: {err}")
        return 0, res
    for e, _ in overdue:
        e.warned_at = now
    res.sent, res.messages = len(overdue), 1
    return len(overdue), res


# ------------------------------------------------------------------ one full cycle

@dataclass
class CycleResult:
    enabled: bool = False
    scanned_files: int = 0
    new_alerts: int = 0
    critical_sent: int = 0
    digest_sent: int = 0
    failed_files_warned: int = 0
    missing_warned: int = 0
    negated: int = 0
    bad_rules: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.new_alerts or self.critical_sent or self.digest_sent or self.failed_files_warned
                    or self.missing_warned or self.errors)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def run_cycle(notifier: Notifier, *, session_factory: SessionFactory = session_scope, now: datetime | None = None,
              only_sources: Iterable[int] | None = None) -> CycleResult:
    """Scan new mail, send what is due, check for missing reports. Does nothing until alerts are enabled."""
    now = now or datetime.now(UTC)
    out = CycleResult()
    with session_factory() as s:
        cutoff = alert_cutoff(s)
    if cutoff is None:
        return out
    out.enabled = True
    steps = [
        ("scan", lambda s: scan_pending(s, cutoff, now, only=only_sources)),
        ("critical", lambda s: dispatch_critical(s, notifier, now, only_sources=only_sources)),
        ("failed files", lambda s: notify_failed_files(s, notifier, cutoff, now, only_sources=only_sources)),
        ("missing reports", lambda s: notify_missing_reports(s, notifier, cutoff, now)),
        ("digest", lambda s: send_digest_if_due(s, notifier, now, only_sources=only_sources)),
    ]
    for name, step in steps:
        try:
            with session_factory() as s:
                r = step(s)
        except Exception as e:  # one failing step must not block the others
            log.exception("alert step %r failed", name)
            out.errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
        if name == "scan":
            out.scanned_files, out.new_alerts, out.negated, out.bad_rules = r.files, r.alerts, r.negated, r.bad_rules
        elif name == "critical":
            out.critical_sent = r.sent
            out.errors += r.errors
        elif name == "failed files":
            out.failed_files_warned = r.sent
            out.errors += r.errors
        elif name == "missing reports":
            out.missing_warned = r[0]
            out.errors += r[1].errors
        else:
            out.digest_sent = r.sent
            out.errors += r.errors
    return out
