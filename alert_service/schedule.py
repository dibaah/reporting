"""Missing-report detection.

A report type gets an expected schedule (report_types.expected_schedule, JSON):

  {"kind": "daily_times", "times": ["06:30", "18:30"], "tz": "Asia/Tehran"}   # 12H reports
  {"kind": "every_n_days", "n": 14, "anchor": "2026-01-05", "time": "08:00"}  # biweekly
  {"kind": "monthly", "day": 1, "time": "08:00"}                              # monthly (day 1-28 or "last")

`tz` is optional (default: the timezone app setting, else this computer's time zone). Times are
the moments the report should have ARRIVED by; the setting missing_report_grace_minutes is added.
Each Outlook report is matched to the nearest expected slot; a slot with no report after the
grace period is "missing" and you are warned once. Forward-only: nothing before alerts_active_from.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from database.models import ExpectedReport, Report, ReportType, SourceFile

UTC = timezone.utc
MAX_SLOTS_PER_RUN = 2000


def get_tz(name: str | None, default: tzinfo) -> tzinfo:
    if not name:
        return default
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {name!r} (use a name like Asia/Tehran; on Windows run: pip install tzdata)")


def _parse_time(value: str) -> time:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"bad time {value!r}, use HH:MM")
    return time(int(m.group(1)), int(m.group(2)))


def validate_schedule(raw: dict) -> dict:
    kind = raw.get("kind")
    out: dict = {"kind": kind}
    if raw.get("tz"):
        get_tz(raw["tz"], UTC)
        out["tz"] = raw["tz"]
    if kind == "daily_times":
        times = [_parse_time(t) for t in raw.get("times") or []]
        if not times:
            raise ValueError("daily_times needs at least one time")
        out["times"] = sorted({t.strftime("%H:%M") for t in times})
    elif kind == "every_n_days":
        n = int(raw.get("n", 0))
        if n < 1:
            raise ValueError("n must be at least 1")
        out.update(n=n, anchor=date.fromisoformat(raw["anchor"]).isoformat(), time=_parse_time(raw.get("time", "08:00")).strftime("%H:%M"))
    elif kind == "monthly":
        day = raw.get("day")
        if day != "last" and not (isinstance(day, int) and 1 <= day <= 28):
            raise ValueError('day must be 1-28 or "last"')
        out.update(day=day, time=_parse_time(raw.get("time", "08:00")).strftime("%H:%M"))
    else:
        raise ValueError("kind must be daily_times, every_n_days or monthly")
    return out


def _local(d: date, t: time, tz: tzinfo) -> datetime:
    return datetime.combine(d, t, tzinfo=tz).astimezone(UTC)


def slots_between(schedule: dict, start: datetime, end: datetime, default_tz: tzinfo) -> list[datetime]:
    """Expected arrival moments (UTC) with start < slot <= end."""
    tz = get_tz(schedule.get("tz"), default_tz)
    first, last = (start.astimezone(tz).date() - timedelta(days=1)), (end.astimezone(tz).date() + timedelta(days=1))
    kind, found = schedule["kind"], []
    day = first
    while day <= last:
        if kind == "daily_times":
            found += [_local(day, _parse_time(t), tz) for t in schedule["times"]]
        elif kind == "every_n_days":
            if (day - date.fromisoformat(schedule["anchor"])).days % int(schedule["n"]) == 0 and day >= date.fromisoformat(schedule["anchor"]):
                found.append(_local(day, _parse_time(schedule["time"]), tz))
        elif kind == "monthly":
            target = calendar.monthrange(day.year, day.month)[1] if schedule["day"] == "last" else schedule["day"]
            if day.day == target:
                found.append(_local(day, _parse_time(schedule["time"]), tz))
        day += timedelta(days=1)
    return sorted(s for s in set(found) if start < s <= end)


def half_window(schedule: dict) -> timedelta:
    """A receipt belongs to a slot when it is nearer to it than half the spacing between slots."""
    kind = schedule["kind"]
    if kind == "daily_times":
        mins = sorted(_parse_time(t).hour * 60 + _parse_time(t).minute for t in schedule["times"])
        gaps = [b - a for a, b in zip(mins, mins[1:])] + [mins[0] + 1440 - mins[-1]]
        return timedelta(minutes=min(gaps) / 2)
    if kind == "every_n_days":
        return timedelta(days=int(schedule["n"]) / 2)
    return timedelta(days=14)


def assign_receipts(slots: list[datetime], receipts: list[tuple[int, datetime]], half: timedelta) -> dict[datetime, int]:
    """slot -> report id. Every receipt goes to its NEAREST slot (if within `half`); the earliest receipt wins a slot."""
    assigned: dict[datetime, tuple[datetime, int]] = {}
    if not slots:
        return {}
    for rid, at in sorted(receipts, key=lambda r: r[1]):
        nearest = min(slots, key=lambda s: abs(s - at))
        if abs(nearest - at) <= half and nearest not in assigned:
            assigned[nearest] = (at, rid)
    return {s: rid for s, (_, rid) in assigned.items()}


# ------------------------------------------------------------------ database side

def _types_with_schedule(session: Session) -> list[ReportType]:
    return [t for t in session.scalars(select(ReportType).where(ReportType.is_active.is_(True))) if t.expected_schedule]


def refresh_expected(session: Session, now: datetime, cutoff: datetime, default_tz: tzinfo) -> int:
    """Create expected_reports rows for slots that have passed since the alerts cutoff. Returns new rows."""
    created = 0
    for t in _types_with_schedule(session):
        last = session.scalar(select(func.max(ExpectedReport.expected_at)).where(ExpectedReport.report_type_id == t.id))
        start = max(cutoff, last) if last else cutoff
        for slot in slots_between(t.expected_schedule, start, now, default_tz)[:MAX_SLOTS_PER_RUN]:
            res = session.execute(
                insert(ExpectedReport).values(report_type_id=t.id, expected_at=slot)
                .on_conflict_do_nothing(index_elements=["report_type_id", "expected_at"]).returning(ExpectedReport.id))
            created += 1 if res.first() is not None else 0
    return created


def match_receipts(session: Session) -> int:
    """Link unmatched slots to the reports that arrived for them. Returns slots newly matched."""
    matched = 0
    for t in _types_with_schedule(session):
        slots = session.scalars(select(ExpectedReport).where(ExpectedReport.report_type_id == t.id)
                                .order_by(ExpectedReport.expected_at).limit(MAX_SLOTS_PER_RUN * 2)).all()
        open_slots = [s for s in slots if s.received_report_id is None]
        if not open_slots:
            continue
        half = half_window(t.expected_schedule)
        lo, hi = slots[0].expected_at - half, slots[-1].expected_at + half
        receipts = [(rid, at) for rid, at in session.execute(
            select(Report.id, SourceFile.received_at).join(SourceFile, SourceFile.id == Report.source_file_id)
            .where(Report.report_type_id == t.id, SourceFile.origin == "outlook", SourceFile.received_at >= lo,
                   SourceFile.received_at <= hi))]
        mapping = assign_receipts([s.expected_at for s in slots], receipts, half)
        for s in open_slots:
            if s.expected_at in mapping:
                s.received_report_id = mapping[s.expected_at]
                matched += 1
    return matched


def overdue_slots(session: Session, now: datetime, grace: timedelta) -> list[tuple[ExpectedReport, str]]:
    """Slots with no report after the grace period that have not been warned about yet."""
    rows = session.execute(
        select(ExpectedReport, ReportType.name).join(ReportType, ReportType.id == ExpectedReport.report_type_id)
        .where(ExpectedReport.received_report_id.is_(None), ExpectedReport.warned_at.is_(None),
               ExpectedReport.expected_at + grace < now).order_by(ExpectedReport.expected_at)).all()
    return [(e, n) for e, n in rows]
