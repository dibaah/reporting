"""Phase 6: email, Teams and message text."""
import datetime as dt
import smtplib

import httpx
import pytest

from alert_service import notify as nt
from alert_service.notify import (
    AlertView, EmailNotifier, Notification, NotifyConfigError, NotifyError, Router, TeamsNotifier,
    format_digest, format_report_alerts, format_system, split_recipients,
)


class FakeSMTP:
    instances: list = []
    fail: Exception | None = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.log, self.sent = host, port, [], []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        self.log.append("ehlo")

    def starttls(self):
        self.log.append("starttls")

    def login(self, u, p):
        self.log.append(("login", u, p))

    def send_message(self, m):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        self.sent.append(m)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances, FakeSMTP.fail = [], None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)


ENV = {"SMTP_HOST": "mail.local", "SMTP_PORT": "587", "SMTP_USER": "bot", "SMTP_PASSWORD": "pw", "SMTP_FROM": "alerts@local"}
N = Notification("[CRITICAL] 12H: 1 alert", "plain body", "<p>html body</p>", "critical")


def test_email_is_built_and_sent_with_starttls_and_login():
    EmailNotifier(["a@x.com", "b@x.com"], ENV).send(N)
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("mail.local", 587)
    assert "starttls" in smtp.log and ("login", "bot", "pw") in smtp.log
    m = smtp.sent[0]
    assert m["Subject"] == N.subject and m["From"] == "alerts@local" and m["To"] == "a@x.com, b@x.com"
    assert m.get_body(("plain",)).get_content().strip() == "plain body" and "html body" in m.get_body(("html",)).get_content()


def test_port_465_uses_ssl_without_starttls_and_sender_falls_back_to_user():
    env = {**ENV, "SMTP_PORT": "465"}
    del env["SMTP_FROM"]
    EmailNotifier(["a@x.com"], env).send(N)
    smtp = FakeSMTP.instances[0]
    assert "starttls" not in smtp.log and smtp.sent[0]["From"] == "bot"
    EmailNotifier(["a@x.com"], {**ENV, "SMTP_STARTTLS": "false"}).send(N)
    assert "starttls" not in FakeSMTP.instances[1].log


@pytest.mark.parametrize("env,recips,message", [
    ({}, ["a@x.com"], "SMTP_HOST"), (ENV, [], "no recipients"), ({"SMTP_HOST": "h"}, ["a@x.com"], "SMTP_FROM"),
])
def test_email_configuration_problems_are_clear(env, recips, message):
    with pytest.raises(NotifyConfigError, match=message):
        EmailNotifier(recips, env).send(N)


@pytest.mark.parametrize("exc", [smtplib.SMTPAuthenticationError(535, b"bad"), ConnectionRefusedError("down"), TimeoutError()])
def test_email_failures_become_notify_errors(exc):
    FakeSMTP.fail = exc
    with pytest.raises(NotifyError, match="email not sent"):
        EmailNotifier(["a@x.com"], ENV).send(N)


def teams(handler):
    return TeamsNotifier("https://prod.example/webhook", httpx.Client(transport=httpx.MockTransport(handler)))


def test_teams_posts_an_adaptive_card():
    seen = {}

    def handler(req):
        seen["body"] = __import__("json").loads(req.content)
        seen["url"] = str(req.url)
        return httpx.Response(202)

    teams(handler).send(Notification("Subject", "x" * 5000, severity="critical", channel="teams"))
    card = seen["body"]["attachments"][0]
    assert seen["body"]["type"] == "message" and card["contentType"] == "application/vnd.microsoft.card.adaptive"
    blocks = card["content"]["body"]
    assert blocks[0]["text"] == "Subject" and blocks[0]["color"] == "Attention" and len(blocks[1]["text"]) == 3000


def test_teams_errors():
    with pytest.raises(NotifyError, match="refused"):
        teams(lambda r: httpx.Response(400, text="bad card")).send(N)

    def boom(req):
        raise httpx.ConnectError("no route")

    with pytest.raises(NotifyError, match="not reachable"):
        teams(boom).send(N)
    with pytest.raises(NotifyConfigError, match="TEAMS_WEBHOOK_URL"):
        TeamsNotifier("").send(N)


def test_router_picks_the_channel():
    class Rec:
        def __init__(self):
            self.got = []

        def send(self, n):
            self.got.append(n)

    e, t = Rec(), Rec()
    r = Router(e, t)
    r.send(Notification("a", "b", channel="email"))
    r.send(Notification("a", "b", channel="teams"))
    assert len(e.got) == len(t.got) == 1
    with pytest.raises(NotifyConfigError):
        r.send(Notification("a", "b", channel="sms"))


def test_split_recipients():
    assert split_recipients("a@x.com; b@y.org,  c@z.net\nnot-an-address") == ["a@x.com", "b@y.org", "c@z.net"]
    assert split_recipients("") == []


def view(i=1, keyword="leak", sev="critical", repeat=0, label="12H 2026-11-23 Day", context="  prev\n> hit <b>x</b>\n  next"):
    return AlertView(i, sev, keyword, "Leak", context, repeat, label, "r.xlsx", dt.datetime(2026, 11, 23, 6, 41, tzinfo=dt.timezone.utc))


def test_report_alert_message():
    n = format_report_alerts([view(1), view(2, repeat=3)], "http://dash")
    assert n.subject == "[CRITICAL] 12H 2026-11-23 Day: 2 keyword alerts"
    assert "ALERT #1" in n.text and "seen 3x before" in n.text and "Dashboard: http://dash" in n.text
    assert "ack 2" in n.text and "2026-11-23 06:41 UTC" in n.text
    assert "&lt;b&gt;x&lt;/b&gt;" in n.html and "<b>x</b>" not in n.html     # report text is escaped in the HTML part
    assert format_report_alerts([view(1)]).subject.endswith("1 keyword alert")


def test_long_messages_are_cut_with_a_hint():
    n = format_report_alerts([view(i) for i in range(40)], max_alerts=25)
    assert n.text.count("ALERT #") == 25 and "and 15 more" in n.text


def test_digest_and_system_messages():
    d = format_digest([view(1, sev="warning"), view(2, sev="warning", label="Monthly 2026-11-01")], "2026-11-24")
    assert d.subject == "[WARNING digest] 2 keyword alerts - 2026-11-24" and "== Monthly 2026-11-01" in d.text
    s = format_system("something", ["line 1", "<b>"])
    assert s.subject == "[ATTENTION] something" and "&lt;b&gt;" in s.html and s.severity == "system"
