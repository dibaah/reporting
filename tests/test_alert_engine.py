"""Phase 6: scanning, sending, digest, failed-file and missing-report warnings, and the management CLI."""
import datetime as dt
import json
from contextlib import contextmanager

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alert_service import cli, engine, service
from alert_service.matcher import RuleError
from database import seed
from database.db import get_engine
from database.models import (
    AlertEvent, ExpectedReport, FailedParseQueue, JobRun, KeywordRule, ReportType,
)
from ingestion_engine.settings import get_setting, set_setting
from tests.alertfakes import (
    CUTOFF, FakeNotifier, failed_outlook_file, only_my_rules, outlook_report, rule,
)
from tests.mailfakes import savepoint_factory

UTC = dt.timezone.utc
H = lambda *a: dt.datetime(*a, tzinfo=UTC)
EARLY = H(2999, 1, 5, 7, 0)    # before the 08:00 digest time
LATE = H(2999, 1, 5, 9, 0)


@pytest.fixture(scope="module", autouse=True)
def _seed():
    seed.run()


@pytest.fixture()
def db():
    conn = get_engine().connect()
    trans = conn.begin()
    s = Session(bind=conn, expire_on_commit=False)
    only_my_rules(s)
    for k, v in {"alerts_enabled": "true", "alerts_active_from": CUTOFF.isoformat(), "timezone": "UTC",
                 "notification_email": "me@example.com", "daily_digest_time": "08:00", "last_digest_sent_on": "",
                 "missing_report_grace_minutes": "120", "report_ui_base_url": ""}.items():
        set_setting(k, v, s)
    yield s
    s.close()
    trans.rollback()
    conn.close()


def cycle(db, notifier, now=EARLY, files=()):
    return engine.run_cycle(notifier, session_factory=savepoint_factory(db), now=now, only_sources=[f.id for f in files])


def events(db, *files):
    return db.scalars(select(AlertEvent).where(AlertEvent.source_file_id.in_([f.id for f in files])).order_by(AlertEvent.id)).all()


# ---------------------------------------------------------------- forward-only

def test_only_mail_received_after_the_cutoff_is_scanned(db):
    rule(db, "leak", "critical")
    old, _ = outlook_report(db, H(2998, 12, 31, 23), [("s", "a", "Leak detected")])
    archive, _ = outlook_report(db, H(2999, 1, 2), [("s", "a", "Leak detected")], origin="archive")
    new, _ = outlook_report(db, H(2999, 1, 2), [("s", "a", "Leak detected")])
    res = engine.scan_pending(db, CUTOFF, EARLY, only=[old.id, archive.id, new.id])
    assert (res.files, res.alerts) == (1, 1)
    assert old.alert_scanned_at is None and archive.alert_scanned_at is None and new.alert_scanned_at == EARLY
    assert engine.scan_pending(db, CUTOFF, EARLY, only=[old.id, archive.id, new.id]).files == 0   # never twice
    assert len(events(db, old, archive, new)) == 1


def test_nothing_happens_until_alerts_are_switched_on(db):
    rule(db, "leak", "critical")
    sf, _ = outlook_report(db, H(2999, 1, 2), [("s", "a", "Leak detected")])
    set_setting("alerts_enabled", "false", db)
    assert engine.alert_cutoff(db) is None
    out = cycle(db, n := FakeNotifier(), files=[sf])
    assert not out.enabled and sf.alert_scanned_at is None and n.sent == []
    set_setting("alerts_enabled", "true", db)
    set_setting("alerts_active_from", "", db)
    assert engine.alert_cutoff(db) is None                          # enabled but never stamped
    set_setting("alerts_active_from", "2999-01-01T00:00:00", db)    # naive timestamps are read as UTC
    assert engine.alert_cutoff(db) == CUTOFF


# ---------------------------------------------------------------- severity routing

def test_critical_now_warning_in_the_digest_info_only_recorded(db):
    rule(db, "leak", "critical")
    rule(db, "vibration", "warning")
    rule(db, "standby", "info")
    sf, _ = outlook_report(db, H(2999, 1, 5, 6, 40), [("Safety", "Flare", "Leak detected at flare header"),
                                                      ("Compressors", "K-101", "Vibration high"),
                                                      ("Remarks", "Note", "Standby unit started")])
    n = FakeNotifier()
    out = cycle(db, n, EARLY, [sf])
    assert (out.new_alerts, out.critical_sent, out.digest_sent) == (3, 1, 0)
    assert [x.subject for x in n.sent] == ["[CRITICAL] 12H 2999-01-05 Day: 1 keyword alert"]
    by_kw = {db.get(KeywordRule, e.rule_id).keyword: e for e in events(db, sf)}
    assert by_kw["leak"].status == "sent" and by_kw["leak"].sent_at == EARLY
    assert by_kw["vibration"].status == "new" and by_kw["standby"].status == "new"

    out = cycle(db, n, LATE, [sf])                                   # after 08:00: the digest goes out
    assert out.digest_sent == 1 and n.sent[-1].subject.startswith("[WARNING digest] 1 keyword alert")
    assert "keyword 'vibration'" in n.sent[-1].text and "keyword 'standby'" not in n.sent[-1].text   # info is never mailed
    assert get_setting("last_digest_sent_on", "", db) == "2999-01-05"
    by_kw = {db.get(KeywordRule, e.rule_id).keyword: e for e in events(db, sf)}
    assert by_kw["vibration"].status == "sent" and by_kw["standby"].status == "new" and by_kw["standby"].sent_at is None

    count = len(n.sent)
    cycle(db, n, LATE + dt.timedelta(hours=1), [sf])
    assert len(n.sent) == count                                      # one digest per day

    sf2, _ = outlook_report(db, H(2999, 1, 6, 6, 40), [("Compressors", "K-102", "Vibration high")])
    cycle(db, n, H(2999, 1, 6, 9), [sf, sf2])
    assert len(n.sent) == count + 1                                   # next day, next digest


def test_digest_waits_for_the_configured_time_and_never_sends_empty_mail(db):
    rule(db, "vibration", "warning")
    sf, _ = outlook_report(db, H(2999, 1, 5, 6), [("s", "K-101", "Vibration high")])
    n = FakeNotifier()
    cycle(db, n, H(2999, 1, 5, 7, 59), [sf])
    assert n.sent == [] and get_setting("last_digest_sent_on", "", db) == ""
    set_setting("daily_digest_time", "07:30", db)
    cycle(db, n, H(2999, 1, 5, 7, 59), [sf])
    assert len(n.sent) == 1
    cycle(db, n, H(2999, 1, 6, 9), [sf])                              # a quiet day: no mail, but marked done
    assert len(n.sent) == 1 and get_setting("last_digest_sent_on", "", db) == "2999-01-06"


def test_digest_time_zone(db):
    rule(db, "vibration", "warning")
    sf, _ = outlook_report(db, H(2999, 1, 5, 3), [("s", "K-101", "Vibration high")])
    set_setting("timezone", "Asia/Tehran", db)                         # 08:00 Tehran = 04:30 UTC
    n = FakeNotifier()
    cycle(db, n, H(2999, 1, 5, 4, 0), [sf])
    assert n.sent == []
    cycle(db, n, H(2999, 1, 5, 4, 45), [sf])
    assert len(n.sent) == 1


def test_a_failed_digest_is_retried(db):
    rule(db, "vibration", "warning")
    sf, _ = outlook_report(db, H(2999, 1, 5, 6), [("s", "K-101", "Vibration high")])
    n = FakeNotifier(fail={"email"})
    out = cycle(db, n, LATE, [sf])
    assert out.errors and get_setting("last_digest_sent_on", "", db) == "" and events(db, sf)[0].status == "new"
    n.fail.clear()
    assert cycle(db, n, LATE, [sf]).digest_sent == 1


# ---------------------------------------------------------------- channels, failures, grouping

def test_channels_and_partial_failure(db):
    rule(db, "alpha", "critical", channel="teams")
    rule(db, "beta", "critical", channel="both")
    rule(db, "gamma", "critical", channel="email")
    sf, _ = outlook_report(db, H(2999, 1, 5, 6), [("s", "a", "alpha here"), ("s", "b", "beta here"), ("s", "c", "gamma here")])
    n = FakeNotifier()
    out = cycle(db, n, EARLY, [sf])
    assert out.critical_sent == 3 and len(n.by_channel("email")) == 1 and len(n.by_channel("teams")) == 1
    teams, email = n.by_channel("teams")[0].text, n.by_channel("email")[0].text
    assert "keyword 'alpha'" in teams and "keyword 'beta'" in teams and "keyword 'gamma'" not in teams
    assert "keyword 'gamma'" in email and "keyword 'beta'" in email and "keyword 'alpha'" not in email
    assert {e.status for e in events(db, sf)} == {"sent"}


def test_alert_stays_unsent_until_some_channel_works(db):
    rule(db, "alpha", "critical", channel="teams")
    rule(db, "beta", "critical", channel="both")
    sf, _ = outlook_report(db, H(2999, 1, 5, 6), [("s", "a", "alpha here"), ("s", "b", "beta here")])
    n = FakeNotifier(fail={"teams"})
    out = cycle(db, n, EARLY, [sf])
    st = {db.get(KeywordRule, e.rule_id).keyword: e.status for e in events(db, sf)}
    assert st == {"alpha": "new", "beta": "sent"}                    # beta reached email, alpha only had Teams
    assert out.errors and "teams is down" in out.errors[0]
    n.fail.clear()
    out = cycle(db, n, EARLY + dt.timedelta(minutes=5), [sf])
    assert out.critical_sent == 1 and {e.status for e in events(db, sf)} == {"sent"}


def test_one_message_per_report(db):
    rule(db, "leak", "critical")
    a, _ = outlook_report(db, H(2999, 1, 5, 6), [("s", "a", "Leak 1"), ("s", "b", "Leak 2"), ("s", "c", "Leak 3")])
    b, _ = outlook_report(db, H(2999, 1, 5, 7), [("s", "a", "Leak 4")], shift="Night")
    n = FakeNotifier()
    cycle(db, n, EARLY, [a, b])
    assert len(n.sent) == 2
    assert n.sent[0].subject.endswith("3 keyword alerts") and n.sent[1].subject.endswith("1 keyword alert")


def test_a_crashing_step_does_not_block_the_others(db):
    rule(db, "leak", "critical")
    sf, _ = outlook_report(db, H(2999, 1, 5, 6), [("s", "a", "Leak detected")])

    class Exploding(FakeNotifier):
        def send(self, n):
            raise RuntimeError("bug in the notifier")

    out = cycle(db, Exploding(), EARLY, [sf])
    assert out.new_alerts == 1 and any(e.startswith("critical: RuntimeError") for e in out.errors)
    assert events(db, sf)[0].status == "new"                          # will be retried


# ---------------------------------------------------------------- what counts as a hit

def test_negation_is_ignored_for_warning_but_never_for_critical(db):
    rule(db, "leak", "warning")
    sf, _ = outlook_report(db, H(2999, 1, 5, 6), [("Safety", "Gas detectors", "No leak detected")])
    res = engine.scan_pending(db, CUTOFF, EARLY, only=[sf.id])
    assert (res.alerts, res.negated) == (0, 1)
    rule(db, "leak", "critical")
    sf2, _ = outlook_report(db, H(2999, 1, 5, 7), [("Safety", "Gas detectors", "No leak detected")])
    res = engine.scan_pending(db, CUTOFF, EARLY, only=[sf2.id])
    assert (res.alerts, res.negated) == (1, 1)                       # critical alerts anyway; the warning rule still skips it


def test_unparsed_files_are_scanned_through_their_raw_cells(db):
    rule(db, "leak", "critical")
    sf = failed_outlook_file(db, H(2999, 1, 5, 6), [["Gas", "Leak at flange"], ["Flare", "ok"]], hidden_rows=[["leak in a hidden sheet"]])
    n = FakeNotifier()
    out = cycle(db, n, EARLY, [sf])
    (ev,) = events(db, sf)
    assert out.new_alerts == 1 and ev.report_id is None and ev.raw_ref == {"sheet": "Sheet1", "row": 1}
    assert f"unparsed file {sf.file_name}" in n.sent[0].subject
    assert any("could not be parsed" in x.subject for x in n.sent)    # and you are told the file failed


def test_context_shows_neighbouring_lines(db):
    rule(db, "leak", "critical")
    sf, _ = outlook_report(db, H(2999, 1, 5, 6), [("s", "A", "first line"), ("s", "B", "Leak found"), ("s", "C", "third line")])
    engine.scan_pending(db, CUTOFF, EARLY, only=[sf.id])
    assert events(db, sf)[0].context.splitlines() == ["  A: first line", "> B: Leak found", "  C: third line"]


def test_repeats_are_annotated_not_silenced(db):
    rule(db, "vibration", "critical")
    f1, _ = outlook_report(db, H(2999, 1, 2, 6), [("s", "K-101", "Vibration high at 41.2 mm/s")])
    f2, _ = outlook_report(db, H(2999, 1, 3, 6), [("s", "K-101", "Vibration high at 41.5 mm/s")])
    f3, _ = outlook_report(db, H(2999, 1, 4, 6), [("s", "K-102", "Vibration high at 3 mm/s")])
    engine.scan_pending(db, CUTOFF, H(2999, 1, 4, 7), only=[f1.id, f2.id, f3.id])
    assert [e.repeat_count for e in events(db, f1, f2, f3)] == [0, 1, 0]
    n = FakeNotifier()
    engine.dispatch_critical(db, n, H(2999, 1, 4, 8), only_sources=[f2.id])
    assert "seen 1x before" in n.sent[0].text


def test_a_regex_that_matches_everything_is_capped(db):
    rule(db, r"\w+", "info", "regex")
    sf, _ = outlook_report(db, H(2999, 1, 5, 6), [("s", f"L{i}", f"line {i}") for i in range(250)])
    res = engine.scan_pending(db, CUTOFF, EARLY, only=[sf.id])
    assert res.alerts == engine.MAX_ALERTS_PER_FILE and res.capped == 50


def test_an_invalid_rule_in_the_database_is_reported_but_others_still_work(db):
    db.add(KeywordRule(keyword="(", match_type="regex", severity="critical", channel="email"))
    rule(db, "leak", "critical")
    sf, _ = outlook_report(db, H(2999, 1, 5, 6), [("s", "a", "Leak")])
    out = cycle(db, FakeNotifier(), EARLY, [sf])
    assert out.new_alerts == 1 and len(out.bad_rules) == 1 and "invalid regex" in out.bad_rules[0]


def test_keywords_added_later_do_not_look_back(db):
    sf, _ = outlook_report(db, H(2999, 1, 5, 6), [("s", "a", "Leak detected")])
    assert engine.scan_pending(db, CUTOFF, EARLY, only=[sf.id]).alerts == 0      # no rules yet: file is marked scanned
    rule(db, "leak", "critical")
    assert engine.scan_pending(db, CUTOFF, EARLY, only=[sf.id]).files == 0 and events(db, sf) == []


# ---------------------------------------------------------------- failed files and missing reports

def test_failed_live_files_are_reported_once(db):
    bad = failed_outlook_file(db, H(2999, 1, 5, 6), [["x"]])
    old = failed_outlook_file(db, H(2998, 12, 30), [["x"]])
    archive = failed_outlook_file(db, H(2999, 1, 5, 6), [["x"]], origin="archive")
    n = FakeNotifier()
    out = cycle(db, n, EARLY, [bad, old, archive])
    assert out.failed_files_warned == 1 and n.sent[0].subject == "[ATTENTION] 1 report file(s) could not be parsed"
    assert bad.file_name in n.sent[0].text and old.file_name not in n.sent[0].text and archive.file_name not in n.sent[0].text
    assert cycle(db, n, EARLY, [bad, old, archive]).failed_files_warned == 0 and len(n.sent) == 1
    q = db.scalar(select(FailedParseQueue).where(FailedParseQueue.source_file_id == bad.id))
    assert q.notified_at == EARLY


def test_missing_report_is_warned_once_and_a_late_arrival_is_still_matched(db):
    t = ReportType(name="ZQX", frequency="12h", expected_schedule={"kind": "daily_times", "times": ["06:30", "18:30"], "tz": "UTC"})
    db.add(t)
    db.flush()
    first, _ = outlook_report(db, H(2999, 1, 1, 6, 45), [("s", "a", "b")], rtype="ZQX")
    n = FakeNotifier()
    out = cycle(db, n, H(2999, 1, 1, 20, 0), [first])               # 18:30 + 2 h grace = 20:30: not yet
    assert out.missing_warned == 0 or all("ZQX" not in x.text for x in n.sent)
    out = cycle(db, n, H(2999, 1, 1, 21, 0), [first])
    msgs = [x for x in n.sent if "missing" in x.subject and "ZQX expected 2999-01-01 18:30" in x.text]
    assert len(msgs) == 1
    cycle(db, n, H(2999, 1, 1, 22, 0), [first])
    assert len([x for x in n.sent if "ZQX expected 2999-01-01 18:30" in x.text]) == 1      # warned once
    late, _ = outlook_report(db, H(2999, 1, 1, 22, 30), [("s", "a", "b")], rtype="ZQX")
    cycle(db, n, H(2999, 1, 1, 23, 0), [first, late])
    slot = db.scalar(select(ExpectedReport).where(ExpectedReport.report_type_id == t.id,
                                                  ExpectedReport.expected_at == H(2999, 1, 1, 18, 30)))
    assert slot.received_report_id is not None and slot.warned_at is not None


# ---------------------------------------------------------------- service wrapper

def test_service_cycle_sets_heartbeat_and_logs_activity(db):
    rule(db, "leak", "critical")
    sf, _ = outlook_report(db, H(2999, 1, 5, 6), [("s", "a", "Leak detected")])
    n = FakeNotifier()
    before = db.scalar(select(func.count()).select_from(JobRun).where(JobRun.job_type == "alerts"))
    r = service.run_once(n, savepoint_factory(db), only_sources=[sf.id])
    assert r.critical_sent == 1
    assert dt.datetime.fromisoformat(get_setting("alert_check_last_ok_at", "", db)).tzinfo is not None
    job = db.scalar(select(JobRun).where(JobRun.job_type == "alerts").order_by(JobRun.id.desc()))
    assert db.scalar(select(func.count()).select_from(JobRun).where(JobRun.job_type == "alerts")) == before + 1
    assert job.status == "success" and job.details["critical_sent"] == 1
    service.run_once(n, savepoint_factory(db), only_sources=[sf.id])      # quiet cycle: heartbeat only
    assert db.scalar(select(func.count()).select_from(JobRun).where(JobRun.job_type == "alerts")) == before + 1


# ---------------------------------------------------------------- management CLI

@pytest.fixture()
def run_cli(db, monkeypatch, capsys):
    @contextmanager
    def fake_scope():
        yield db

    monkeypatch.setattr(cli, "session_scope", fake_scope)

    def go(*argv):
        code = cli.main(list(argv))
        return code, capsys.readouterr().out
    return go


def test_cli_keywords(run_cli, db):
    code, out = run_cli("keyword", "add", "zq-leak", "--severity", "critical", "--channel", "both")
    assert code == 0 and "added" in out and "zq-leak" in out
    code, out = run_cli("keyword", "add", "(", "--match", "regex")
    assert code == 2 and "invalid regex" in out
    rid = db.scalar(select(KeywordRule.id).where(KeywordRule.keyword == "zq-leak"))
    assert "zq-leak" in run_cli("keyword", "list")[1]
    assert f"#{rid} is now off" in run_cli("keyword", "off", str(rid))[1] and not db.get(KeywordRule, rid).is_active
    assert run_cli("keyword", "remove", str(rid))[1] == "removed\n"
    assert run_cli("keyword", "off", "999999")[0] == 2


def test_cli_remove_is_refused_when_alerts_exist(run_cli, db):
    r = rule(db, "leak", "critical")
    sf, _ = outlook_report(db, H(2999, 1, 5, 6), [("s", "a", "Leak")])
    engine.scan_pending(db, CUTOFF, EARLY, only=[sf.id])
    code, out = run_cli("keyword", "remove", str(r.id))
    assert code == 2 and "keyword off" in out


def test_cli_enable_disable_and_forward_only_start(run_cli, db):
    set_setting("alerts_enabled", "false", db)
    set_setting("alerts_active_from", "", db)
    assert "alerts start now" in run_cli("enable")[1]
    stamped = get_setting("alerts_active_from", "", db)
    assert get_setting("alerts_enabled", "", db) == "true" and dt.datetime.fromisoformat(stamped).tzinfo is not None
    assert "alerts continue from" in run_cli("enable")[1] and get_setting("alerts_active_from", "", db) == stamped
    assert "disabled" in run_cli("disable")[1] and get_setting("alerts_enabled", "", db) == "false"
    set_setting("alerts_active_from", "2000-01-01T00:00:00+00:00", db)
    run_cli("enable", "--restart")
    assert get_setting("alerts_active_from", "", db) > "2000"


def test_cli_alerts_and_acknowledge(run_cli, db):
    rule(db, "leak", "critical")
    sf, _ = outlook_report(db, H(2999, 1, 5, 6), [("s", "a", "Leak detected")])
    engine.scan_pending(db, CUTOFF, EARLY, only=[sf.id])
    ev = events(db, sf)[0]
    assert f"#{ev.id}" in run_cli("alerts", "list", "--status", "new", "--limit", "1000")[1]
    assert "Leak detected" in run_cli("alerts", "show", str(ev.id))[1]
    code, out = run_cli("ack", str(ev.id), "999999", "--note", "K-102 inspected, bearing replaced")
    assert code == 0 and "not found: [999999]" in out
    assert ev.status == "acknowledged" and ev.action_note == "K-102 inspected, bearing replaced" and ev.acknowledged_at
    assert run_cli("alerts", "show", "999999")[0] == 2


def test_cli_status_is_valid_json(run_cli):
    code, out = run_cli("status")
    data = json.loads(out)
    assert code == 0 and {"alerts_enabled", "alerts_active_from", "files_waiting_for_scan", "alerts_by_status", "recipients",
                          "last_live_import", "last_alert_check", "schedules"} <= set(data)
    assert data["recipients"] == ["me@example.com"]


def test_cli_schedule(run_cli, db):
    t = ReportType(name="ZQX", frequency="12h")
    db.add(t)
    db.flush()
    code, out = run_cli("schedule", "set", "ZQX", "--times", "18:30,06:30", "--tz", "Asia/Tehran")
    assert code == 0 and t.expected_schedule == {"kind": "daily_times", "times": ["06:30", "18:30"], "tz": "Asia/Tehran"}
    assert "ZQX" in run_cli("schedule", "show")[1]
    assert run_cli("schedule", "set", "ZQX", "--every-days", "14", "--anchor", "2026-01-05")[0] == 0
    assert t.expected_schedule["kind"] == "every_n_days"
    assert run_cli("schedule", "set", "ZQX", "--monthly-day", "last")[0] == 0 and t.expected_schedule["day"] == "last"
    assert run_cli("schedule", "set", "ZQX", "--times", "99:00")[0] == 2
    assert run_cli("schedule", "set", "ZQX")[0] == 2
    assert run_cli("schedule", "set", "NOPE", "--times", "06:00")[0] == 2
    assert run_cli("schedule", "clear", "ZQX")[0] == 0 and t.expected_schedule is None


def test_cli_preview_reads_history_and_changes_nothing(run_cli, db):
    outlook_report(db, H(2999, 1, 2), [("Safety", "Flare", "Leak at flange"), ("Safety", "Gas", "No leak detected")], origin="archive")
    outlook_report(db, H(2999, 1, 3), [("Safety", "Flare", "Small LEAK at valve")], origin="archive")
    before = db.scalar(select(func.count()).select_from(AlertEvent))
    code, out = run_cli("preview", "leak", "--severity", "warning", "--from", "2999-01-01")
    assert code == 0 and "2 matching line(s) in 2 report(s)" in out and "1 ignored as negated" in out
    assert "nothing was created or sent" in out
    out = run_cli("preview", "leak", "--severity", "critical", "--from", "2999-01-01")[1]
    assert "3 matching line(s)" in out                                # critical keeps the negated line too
    assert "2 matching" in run_cli("preview", r"le+ak at \w+", "--match", "regex", "--from", "2999-01-01")[1]
    assert db.scalar(select(func.count()).select_from(AlertEvent)) == before
    assert run_cli("preview", "(", "--match", "regex")[0] == 2


def test_cli_test_notify(run_cli, monkeypatch):
    n = FakeNotifier()
    monkeypatch.setattr(cli, "default_router", lambda recipients: n)
    assert run_cli("test-notify", "--channel", "teams")[0] == 0 and n.sent[0].channel == "teams"
    n.fail.add("email")
    code, out = run_cli("test-notify")
    assert code == 1 and "NOT sent" in out


def test_add_keyword_validates(db):
    with pytest.raises(RuleError):
        cli.add_keyword(db, "a*", "regex")


def test_cli_settings_are_validated(run_cli, db):
    assert run_cli("setting", "set", "notification_email", "a@x.com, b@y.org")[0] == 0
    assert get_setting("notification_email", "", db) == "a@x.com, b@y.org"
    assert run_cli("setting", "set", "timezone", "Asia/Tehran")[0] == 0
    assert run_cli("setting", "set", "daily_digest_time", "07:45")[0] == 0
    assert run_cli("setting", "set", "live_import_interval_minutes", "10")[0] == 0
    for key, value, message in [("notification_email", "nobody", "no valid email"), ("timezone", "Mars/Base", "unknown time zone"),
                                ("daily_digest_time", "25:00", "bad time"), ("live_import_interval_minutes", "0", "at least 1"),
                                ("live_import_since", "yesterday", ""), ("noitfication_email", "a@x.com", "unknown setting"),
                                ("alerts_enabled", "true", "enable"), ("alerts_active_from", "x", "enable")]:
        code, out = run_cli("setting", "set", key, value)
        assert code == 2 and message in out, (key, out)
    code, out = run_cli("setting", "list")
    assert code == 0 and "notification_email" in out and "daily_digest_time" in out
