"""Phase 6: expected-report schedules and matching (pure functions first, then the database)."""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from alert_service import schedule as sch
from database.db import get_engine
from database.models import ExpectedReport, ReportType
from tests.alertfakes import outlook_report

UTC = dt.timezone.utc
H = lambda *a: dt.datetime(*a, tzinfo=UTC)


def test_daily_times_in_a_time_zone_including_daylight_saving():
    s = {"kind": "daily_times", "times": ["06:30", "18:30"], "tz": "Europe/Berlin"}
    slots = sch.slots_between(s, H(2026, 3, 28, 0), H(2026, 3, 31, 0), UTC)
    assert slots == [H(2026, 3, 28, 5, 30), H(2026, 3, 28, 17, 30),     # CET  = UTC+1
                     H(2026, 3, 29, 4, 30), H(2026, 3, 29, 16, 30),     # DST began that night: UTC+2
                     H(2026, 3, 30, 4, 30), H(2026, 3, 30, 16, 30)]
    t = {"kind": "daily_times", "times": ["06:30"], "tz": "Asia/Tehran"}   # UTC+3:30, no daylight saving
    assert sch.slots_between(t, H(2026, 1, 1), H(2026, 1, 2), UTC) == [H(2026, 1, 1, 3, 0)]


def test_range_is_start_exclusive_end_inclusive_and_default_tz_is_used():
    s = {"kind": "daily_times", "times": ["06:00"]}
    assert sch.slots_between(s, H(2026, 1, 1, 6), H(2026, 1, 2, 6), UTC) == [H(2026, 1, 2, 6)]
    tz = ZoneInfo("Asia/Tehran")
    assert sch.slots_between(s, H(2026, 1, 1), H(2026, 1, 2), tz) == [H(2026, 1, 1, 2, 30)]


def test_every_n_days_and_monthly():
    s = {"kind": "every_n_days", "n": 14, "anchor": "2026-01-05", "time": "08:00"}
    got = sch.slots_between(s, H(2025, 12, 1), H(2026, 2, 28), UTC)
    assert got == [H(2026, 1, 5, 8), H(2026, 1, 19, 8), H(2026, 2, 2, 8), H(2026, 2, 16, 8)]   # nothing before the anchor
    m = {"kind": "monthly", "day": 1, "time": "08:00"}
    assert [x.day for x in sch.slots_between(m, H(2026, 1, 1), H(2026, 4, 1, 9), UTC)] == [1, 1, 1, 1]  # Jan, Feb, Mar and Apr 1st at 08:00
    last = {"kind": "monthly", "day": "last", "time": "20:00"}
    assert [(x.month, x.day) for x in sch.slots_between(last, H(2027, 1, 31, 21), H(2027, 4, 1), UTC)] == [(2, 28), (3, 31)]


@pytest.mark.parametrize("raw,message", [
    ({"kind": "daily_times", "times": ["25:00"]}, "bad time"),
    ({"kind": "daily_times", "times": []}, "at least one"),
    ({"kind": "every_n_days", "n": 0, "anchor": "2026-01-01"}, "at least 1"),
    ({"kind": "monthly", "day": 31}, "1-28"),
    ({"kind": "weekly"}, "kind must be"),
    ({"kind": "daily_times", "times": ["06:00"], "tz": "Mars/Base"}, "unknown time zone"),
])
def test_invalid_schedules_are_rejected(raw, message):
    with pytest.raises(ValueError, match=message):
        sch.validate_schedule(raw)


def test_validate_normalises():
    assert sch.validate_schedule({"kind": "daily_times", "times": ["18:30", "6:30", "06:30"]})["times"] == ["06:30", "18:30"]


def test_half_window():
    assert sch.half_window({"kind": "daily_times", "times": ["06:30", "18:30"]}) == dt.timedelta(hours=6)
    assert sch.half_window({"kind": "daily_times", "times": ["06:30"]}) == dt.timedelta(hours=12)
    assert sch.half_window({"kind": "every_n_days", "n": 14}) == dt.timedelta(days=7)


def test_receipts_go_to_their_nearest_slot():
    slots = [H(2999, 1, 1, 6, 30), H(2999, 1, 1, 18, 30), H(2999, 1, 2, 6, 30)]
    half = dt.timedelta(hours=6)
    got = sch.assign_receipts(slots, [(1, H(2999, 1, 1, 6, 45)), (2, H(2999, 1, 2, 6, 40)), (3, H(2999, 1, 2, 6, 50))], half)
    assert got == {slots[0]: 1, slots[2]: 2}                     # the earlier receipt wins slot 3; slot 2 stays empty
    assert sch.assign_receipts(slots, [(9, H(2999, 1, 5))], half) == {}   # far from every slot
    assert sch.assign_receipts([], [(1, H(2999, 1, 1))], half) == {}
    late = sch.assign_receipts(slots, [(7, H(2999, 1, 1, 19, 0))], half)
    assert late == {slots[1]: 7}                                 # a late report still fills ITS slot


# ---------------------------------------------------------------- database side

@pytest.fixture()
def db():
    conn = get_engine().connect()
    trans = conn.begin()
    s = Session(bind=conn, expire_on_commit=False)
    yield s
    s.close()
    trans.rollback()
    conn.close()


@pytest.fixture()
def zqx(db):
    t = ReportType(name="ZQX", frequency="12h", expected_schedule={"kind": "daily_times", "times": ["06:30", "18:30"], "tz": "UTC"})
    db.add(t)
    db.flush()
    return t


def zqx_slots(db, t):
    return db.scalars(select(ExpectedReport).where(ExpectedReport.report_type_id == t.id).order_by(ExpectedReport.expected_at)).all()


def test_slots_start_after_the_cutoff_and_are_not_duplicated(db, zqx):
    cutoff, now = H(2999, 1, 1, 0), H(2999, 1, 2, 10)
    sch.refresh_expected(db, now, cutoff, UTC)
    assert [s.expected_at for s in zqx_slots(db, zqx)] == [H(2999, 1, 1, 6, 30), H(2999, 1, 1, 18, 30), H(2999, 1, 2, 6, 30)]
    sch.refresh_expected(db, now, cutoff, UTC)                              # again: nothing new
    sch.refresh_expected(db, H(2999, 1, 3, 10), cutoff, UTC)                # later: only the new slots
    assert len(zqx_slots(db, zqx)) == 5


def test_type_without_a_schedule_is_never_checked(db):
    t = ReportType(name="ZQY", frequency="12h")
    db.add(t)
    db.flush()
    sch.refresh_expected(db, H(2999, 1, 5), H(2999, 1, 1), UTC)
    assert zqx_slots(db, t) == []


def test_matching_and_overdue(db, zqx):
    cutoff, now = H(2999, 1, 1, 0), H(2999, 1, 2, 22)
    outlook_report(db, H(2999, 1, 1, 6, 45), [("s", "a", "b")], rtype="ZQX")
    outlook_report(db, H(2999, 1, 2, 6, 40), [("s", "a", "b")], rtype="ZQX")
    outlook_report(db, H(2999, 1, 1, 18, 40), [("s", "a", "b")], rtype="ZQX", origin="archive")   # archive never counts
    sch.refresh_expected(db, now, cutoff, UTC)
    assert sch.match_receipts(db) >= 2
    slots = zqx_slots(db, zqx)
    assert [s.received_report_id is not None for s in slots] == [True, False, True, False]
    grace = dt.timedelta(minutes=120)
    overdue = [e.expected_at for e, n in sch.overdue_slots(db, now, grace) if n == "ZQX"]
    assert overdue == [H(2999, 1, 1, 18, 30), H(2999, 1, 2, 18, 30)]       # both 18:30 slots are more than 2 h late
    assert [e.expected_at for e, n in sch.overdue_slots(db, H(2999, 1, 2, 20, 0), grace) if n == "ZQX"] == [H(2999, 1, 1, 18, 30)]


def test_a_late_report_fills_its_slot(db, zqx):
    cutoff = H(2999, 1, 1, 0)
    sch.refresh_expected(db, H(2999, 1, 1, 22), cutoff, UTC)
    sch.match_receipts(db)
    assert all(s.received_report_id is None for s in zqx_slots(db, zqx))
    outlook_report(db, H(2999, 1, 1, 21, 30), [("s", "a", "b")], rtype="ZQX")      # the 18:30 report arrives 3 h late
    sch.match_receipts(db)
    first, second = zqx_slots(db, zqx)
    assert first.received_report_id is None and second.received_report_id is not None
