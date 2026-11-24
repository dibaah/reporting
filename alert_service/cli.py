"""Manage keywords, alerts and expected-report schedules.

  python -m alert_service.cli keyword add "leak" --severity critical --channel both
  python -m alert_service.cli keyword add "leak(s|age|ing)?" --match regex --severity warning
  python -m alert_service.cli keyword list
  python -m alert_service.cli preview "leak" --severity warning --from 2024-01-01   # read-only, nothing is sent
  python -m alert_service.cli enable                  # alerts start NOW (forward-only)
  python -m alert_service.cli status
  python -m alert_service.cli alerts list --status new
  python -m alert_service.cli ack 12 13 --note "K-102 inspected, bearing replaced"
  python -m alert_service.cli schedule set 12H --times 06:30,18:30 --tz Asia/Tehran
  python -m alert_service.cli test-notify --channel email
  python -m alert_service.cli setting set notification_email "me@company.com, boss@company.com"
  python -m alert_service.cli setting list
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database.db import session_scope
from database.seed import DEFAULT_SETTINGS
from database.models import AlertEvent, KeywordRule, Report, ReportLine, ReportType, SourceFile
from ingestion_engine.settings import get_setting, set_setting

from . import schedule
from .matcher import RuleError, build_pattern, compile_rule, is_negated
from .notify import Notification, NotifyError, default_router, split_recipients

UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ------------------------------------------------------------------ keywords

def add_keyword(s: Session, keyword: str, match_type: str = "word", severity: str = "warning", channel: str = "email") -> KeywordRule:
    build_pattern(keyword, match_type)  # raises RuleError for anything unusable
    rule = KeywordRule(keyword=keyword.strip(), match_type=match_type, severity=severity, channel=channel)
    s.add(rule)
    s.flush()
    return rule


def preview_keyword(s: Session, keyword: str, match_type: str = "word", severity: str = "warning",
                    date_from: date | None = None, max_lines: int = 20000, show: int = 20) -> dict:
    """Test a keyword against the stored history. READ-ONLY: nothing is created or sent."""
    rule = compile_rule(type("R", (), dict(id=None, keyword=keyword, match_type=match_type, severity=severity, channel="email")))
    q = (select(ReportLine, Report, ReportType.name).join(Report, Report.id == ReportLine.report_id)
         .join(ReportType, ReportType.id == Report.report_type_id).order_by(Report.report_date.desc(), ReportLine.id).limit(max_lines))
    if date_from:
        q = q.where(Report.report_date >= date_from)
    if match_type != "regex":  # cheap pre-filter in SQL on the first word, exact matching in Python
        token = keyword.split()[0].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        q = q.where(or_(ReportLine.text.ilike(f"%{token}%"), ReportLine.label.ilike(f"%{token}%"), ReportLine.section.ilike(f"%{token}%")))
    scanned = matched = negated = 0
    reports: set[int] = set()
    samples = []
    for line, rep, tname in s.execute(q):
        scanned += 1
        text = " | ".join(p for p in (line.section, line.label, line.text) if p)
        hits = rule.find(text)
        if not hits:
            continue
        if severity != "critical" and is_negated(text, hits[0]):
            negated += 1
            continue
        matched += 1
        reports.add(rep.id)
        if len(samples) < show:
            samples.append({"date": str(rep.report_date), "type": tname, "report_id": rep.id, "line": text[:160]})
    return {"lines_checked": scanned, "matching_lines": matched, "reports": len(reports),
            "ignored_as_negated": negated, "samples": samples, "truncated": scanned >= max_lines}


# ------------------------------------------------------------------ alerts

def acknowledge(s: Session, ids: list[int], note: str | None) -> list[int]:
    done = []
    for ev in s.scalars(select(AlertEvent).where(AlertEvent.id.in_(ids))):
        ev.status, ev.acknowledged_at = "acknowledged", datetime.now(UTC)
        if note:
            ev.action_note = note
        done.append(ev.id)
    return done


def list_alerts(s: Session, status: str | None = None, severity: str | None = None, limit: int = 20) -> list[dict]:
    q = (select(AlertEvent, KeywordRule, SourceFile, Report).join(KeywordRule, KeywordRule.id == AlertEvent.rule_id)
         .join(SourceFile, SourceFile.id == AlertEvent.source_file_id, isouter=True)
         .join(Report, Report.id == AlertEvent.report_id, isouter=True).order_by(AlertEvent.id.desc()).limit(limit))
    if status:
        q = q.where(AlertEvent.status == status)
    if severity:
        q = q.where(KeywordRule.severity == severity)
    return [{"id": e.id, "status": e.status, "severity": r.severity, "keyword": r.keyword, "matched": e.matched_text,
             "report_date": str(rep.report_date) if rep else None, "file": sf.file_name if sf else None,
             "created": e.created_at, "sent": e.sent_at, "repeat_count": e.repeat_count, "note": e.action_note,
             "context": e.context} for e, r, sf, rep in s.execute(q)]


# ------------------------------------------------------------------ status

def status(s: Session) -> dict:
    cutoff = get_setting("alerts_active_from", "", s)
    pending = s.scalar(select(func.count()).select_from(SourceFile).where(
        SourceFile.origin == "outlook", SourceFile.alert_scanned_at.is_(None),
        *([SourceFile.received_at >= datetime.fromisoformat(cutoff)] if cutoff else [SourceFile.id < 0]))) or 0
    counts = dict(s.execute(select(AlertEvent.status, func.count()).group_by(AlertEvent.status)).all())
    return {
        "alerts_enabled": get_setting("alerts_enabled", "false", s), "alerts_active_from": cutoff or None,
        "files_waiting_for_scan": pending, "alerts_by_status": counts,
        "active_keywords": s.scalar(select(func.count()).select_from(KeywordRule).where(KeywordRule.is_active.is_(True))),
        "recipients": split_recipients(get_setting("notification_email", "", s)),
        "last_live_import": get_setting("live_import_last_ok_at", "never", s),
        "last_alert_check": get_setting("alert_check_last_ok_at", "never", s),
        "last_digest_sent_on": get_setting("last_digest_sent_on", "never", s),
        "schedules": {t.name: t.expected_schedule for t in s.scalars(select(ReportType).order_by(ReportType.id))},
    }


PROTECTED = {"alerts_enabled": "use `enable` / `disable`", "alerts_active_from": "use `enable` / `enable --restart`",
             "historical_import_control": "use `python -m ingestion_engine.historical --set-control`"}


def set_checked_setting(s: Session, key: str, value: str) -> None:
    """Change an operational setting with basic validation (typos in a key are rejected)."""
    if key in PROTECTED:
        raise ValueError(f"{key} is not set here: {PROTECTED[key]}")
    if key not in DEFAULT_SETTINGS:
        raise ValueError(f"unknown setting {key!r}; see `setting list`")
    value = value.strip()
    if key.endswith(("_minutes", "_count")) and not (value.isdigit() and int(value) >= 1):
        raise ValueError(f"{key} must be a whole number of at least 1")
    if key == "timezone" and value:
        schedule.get_tz(value, UTC)
    if key == "daily_digest_time":
        schedule._parse_time(value)
    if key == "live_import_since" and value:
        date.fromisoformat(value)
    if key == "notification_email" and value and not split_recipients(value):
        raise ValueError("no valid email address found (separate several with commas)")
    set_setting(key, value, s)


# ------------------------------------------------------------------ command line

def _print_alert(a: dict, full: bool = False) -> None:
    print(f"#{a['id']:<5} {a['status']:<12} {a['severity']:<8} '{a['keyword']}'  report {a['report_date'] or '(unparsed)'}  "
          f"{a['file'] or ''}" + (f"   repeat x{a['repeat_count']}" if a['repeat_count'] else ""))
    if full:
        print(a["context"] or "")
        if a["note"]:
            print("note:", a["note"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="alert_service.cli", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    kw = sub.add_parser("keyword").add_subparsers(dest="kcmd", required=True)
    a = kw.add_parser("add")
    a.add_argument("keyword")
    a.add_argument("--match", choices=["word", "phrase", "regex"], default="word")
    a.add_argument("--severity", choices=["critical", "warning", "info"], default="warning")
    a.add_argument("--channel", choices=["email", "teams", "both"], default="email")
    kw.add_parser("list")
    for name in ("on", "off", "remove"):
        kw.add_parser(name).add_argument("id", type=int)

    p = sub.add_parser("preview")
    p.add_argument("keyword")
    p.add_argument("--match", choices=["word", "phrase", "regex"], default="word")
    p.add_argument("--severity", choices=["critical", "warning", "info"], default="warning")
    p.add_argument("--from", dest="date_from", type=date.fromisoformat)
    p.add_argument("--max-lines", type=int, default=20000)

    e = sub.add_parser("enable")
    e.add_argument("--restart", action="store_true", help="move the start point to now (otherwise an earlier start is kept)")
    sub.add_parser("disable")
    sub.add_parser("status")
    al = sub.add_parser("alerts").add_subparsers(dest="acmd", required=True)
    l = al.add_parser("list")
    l.add_argument("--status", choices=["new", "sent", "acknowledged"])
    l.add_argument("--severity", choices=["critical", "warning", "info"])
    l.add_argument("--limit", type=int, default=20)
    al.add_parser("show").add_argument("id", type=int)
    k = sub.add_parser("ack")
    k.add_argument("ids", type=int, nargs="+")
    k.add_argument("--note")
    t = sub.add_parser("test-notify")
    t.add_argument("--channel", choices=["email", "teams"], default="email")
    st = sub.add_parser("setting").add_subparsers(dest="stcmd", required=True)
    st.add_parser("list")
    st_set = st.add_parser("set")
    st_set.add_argument("key")
    st_set.add_argument("value")
    sc = sub.add_parser("schedule").add_subparsers(dest="scmd", required=True)
    sc.add_parser("show")
    s_set = sc.add_parser("set")
    s_set.add_argument("report_type")
    s_set.add_argument("--times", help="daily arrival times, e.g. 06:30,18:30")
    s_set.add_argument("--every-days", type=int)
    s_set.add_argument("--anchor", help="first date for --every-days (YYYY-MM-DD)")
    s_set.add_argument("--monthly-day", help="1-28 or 'last'")
    s_set.add_argument("--time", default="08:00")
    s_set.add_argument("--tz")
    sc.add_parser("clear").add_argument("report_type")
    args = ap.parse_args(argv)

    try:
        with session_scope() as s:
            return _run(args, s)
    except (RuleError, ValueError) as e:
        print(f"error: {e}")
        return 2


def _run(args, s: Session) -> int:
    if args.cmd == "keyword":
        if args.kcmd == "add":
            r = add_keyword(s, args.keyword, args.match, args.severity, args.channel)
            print(f"keyword #{r.id} added: '{r.keyword}' ({r.match_type}, {r.severity}, {r.channel})")
            if r.channel in ("email", "both") and not split_recipients(get_setting("notification_email", "", s)):
                print("note: notification_email is not set yet, so emails cannot be sent")
        elif args.kcmd == "list":
            for r in s.scalars(select(KeywordRule).order_by(KeywordRule.id)):
                print(f"#{r.id:<4} {'on ' if r.is_active else 'off'} {r.severity:<8} {r.match_type:<6} {r.channel:<6} {r.keyword}")
        else:
            r = s.get(KeywordRule, args.id)
            if r is None:
                print("no such keyword")
                return 2
            if args.kcmd == "remove":
                if s.scalar(select(func.count()).select_from(AlertEvent).where(AlertEvent.rule_id == r.id)):
                    print("this keyword already has alerts; use `keyword off` instead so the history stays valid")
                    return 2
                s.delete(r)
                print("removed")
            else:
                r.is_active = args.kcmd == "on"
                print(f"keyword #{r.id} is now {args.kcmd}")
    elif args.cmd == "preview":
        res = preview_keyword(s, args.keyword, args.match, args.severity, args.date_from, args.max_lines)
        print(f"{res['matching_lines']} matching line(s) in {res['reports']} report(s); {res['lines_checked']} candidate line(s) checked; "
              f"{res['ignored_as_negated']} ignored as negated" + ("  [stopped at --max-lines]" if res["truncated"] else ""))
        for x in res["samples"]:
            print(f"  {x['date']} {x['type']:<8} #{x['report_id']}  {x['line']}")
        print("(preview only: nothing was created or sent)")
    elif args.cmd == "enable":
        prev = get_setting("alerts_active_from", "", s)
        if args.restart or not prev:
            set_setting("alerts_active_from", _now_iso(), s)
            print(f"alerts start now ({get_setting('alerts_active_from', '', s)}); older mail never creates alerts")
        else:
            waiting = s.scalar(select(func.count()).select_from(SourceFile).where(
                SourceFile.origin == "outlook", SourceFile.alert_scanned_at.is_(None),
                SourceFile.received_at >= datetime.fromisoformat(prev)))
            print(f"alerts continue from {prev}; {waiting} file(s) received since then will be checked. Use --restart to start from now instead")
        set_setting("alerts_enabled", "true", s)
    elif args.cmd == "disable":
        set_setting("alerts_enabled", "false", s)
        print("alerts disabled (nothing is scanned or sent). `enable` continues from the same start point; `enable --restart` starts from now")
    elif args.cmd == "status":
        print(json.dumps(status(s), indent=2, default=str))
    elif args.cmd == "alerts":
        if args.acmd == "list":
            rows = list_alerts(s, args.status, args.severity, args.limit)
            for a in rows:
                _print_alert(a)
            if not rows:
                print("no alerts")
        else:
            rows = [a for a in list_alerts(s, limit=100000) if a["id"] == args.id]
            if not rows:
                print("no such alert")
                return 2
            _print_alert(rows[0], full=True)
    elif args.cmd == "ack":
        done = acknowledge(s, args.ids, args.note)
        print(f"acknowledged: {done}" + ("" if len(done) == len(args.ids) else f"  (not found: {sorted(set(args.ids) - set(done))})"))
    elif args.cmd == "test-notify":
        n = Notification("[TEST] Report Intelligence alert", "This is a test message from alert_service.cli test-notify.",
                         "<p>This is a test message.</p>", "system", args.channel)
        try:
            default_router(split_recipients(get_setting("notification_email", "", s))).send(n)
            print(f"test message sent through {args.channel}")
        except NotifyError as e:
            print(f"NOT sent: {e}")
            return 1
    elif args.cmd == "setting":
        if args.stcmd == "list":
            for k in sorted(DEFAULT_SETTINGS):
                print(f"{k:<34} {get_setting(k, '', s)!r:<32} {DEFAULT_SETTINGS[k][1]}")
        else:
            set_checked_setting(s, args.key, args.value)
            print(f"{args.key} = {args.value!r}")
    elif args.cmd == "schedule":
        if args.scmd == "show":
            for t in s.scalars(select(ReportType).order_by(ReportType.id)):
                print(f"{t.name:<10} {json.dumps(t.expected_schedule) if t.expected_schedule else '(no expected schedule: not checked)'}")
        elif args.scmd == "clear":
            t = s.scalar(select(ReportType).where(ReportType.name == args.report_type))
            if t is None:
                print("no such report type")
                return 2
            t.expected_schedule = None
            print("schedule cleared")
        else:
            t = s.scalar(select(ReportType).where(ReportType.name == args.report_type))
            if t is None:
                print("no such report type")
                return 2
            if args.times:
                raw = {"kind": "daily_times", "times": [x.strip() for x in args.times.split(",")]}
            elif args.every_days:
                raw = {"kind": "every_n_days", "n": args.every_days, "anchor": args.anchor or "", "time": args.time}
            elif args.monthly_day:
                raw = {"kind": "monthly", "day": "last" if args.monthly_day == "last" else int(args.monthly_day), "time": args.time}
            else:
                print("give --times, --every-days with --anchor, or --monthly-day")
                return 2
            if args.tz:
                raw["tz"] = args.tz
            t.expected_schedule = schedule.validate_schedule(raw)
            print(f"{t.name}: {json.dumps(t.expected_schedule)}  (checked from the moment alerts were enabled)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
