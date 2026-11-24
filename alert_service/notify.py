"""Sending notifications: email (SMTP) and Microsoft Teams (Workflows webhook)."""
from __future__ import annotations

import html
import os
import re
import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage

import httpx


class NotifyError(Exception):
    """Delivery failed (server down, refused). The alert stays unsent and is retried next cycle."""


class NotifyConfigError(NotifyError):
    """Not configured (no SMTP host, no recipients, no webhook)."""


@dataclass
class Notification:
    subject: str
    text: str
    html: str = ""
    severity: str = "info"  # critical | warning | info | system
    channel: str = "email"  # email | teams


class Notifier(ABC):
    @abstractmethod
    def send(self, n: Notification) -> None:
        """Deliver on n.channel or raise NotifyError."""


def split_recipients(value: str) -> list[str]:
    return [r for r in re.split(r"[,;\s]+", value or "") if "@" in r]


class EmailNotifier:
    def __init__(self, recipients: list[str], env=os.environ):
        self.recipients = recipients
        self.host = env.get("SMTP_HOST", "")
        self.port = int(env.get("SMTP_PORT") or 587)
        self.user, self.password = env.get("SMTP_USER", ""), env.get("SMTP_PASSWORD", "")
        self.sender = env.get("SMTP_FROM") or self.user
        self.starttls = (env.get("SMTP_STARTTLS", "true").lower() != "false")

    def send(self, n: Notification) -> None:
        if not self.host:
            raise NotifyConfigError("SMTP_HOST is not set (see .env)")
        if not self.recipients:
            raise NotifyConfigError("no recipients: set notification_email in the app settings")
        if not self.sender:
            raise NotifyConfigError("SMTP_FROM (or SMTP_USER) is not set")
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = n.subject, self.sender, ", ".join(self.recipients)
        msg.set_content(n.text)
        if n.html:
            msg.add_alternative(n.html, subtype="html")
        try:
            smtp_cls = smtplib.SMTP_SSL if self.port == 465 else smtplib.SMTP
            with smtp_cls(self.host, self.port, timeout=30) as smtp:
                smtp.ehlo()
                if self.starttls and self.port != 465:
                    smtp.starttls()
                    smtp.ehlo()
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            raise NotifyError(f"email not sent: {type(e).__name__}: {e}")


class TeamsNotifier:
    COLORS = {"critical": "Attention", "warning": "Warning", "system": "Accent"}

    def __init__(self, webhook_url: str, client: httpx.Client | None = None):
        self.url, self._http = webhook_url, client or httpx.Client(timeout=30)

    def send(self, n: Notification) -> None:
        if not self.url:
            raise NotifyConfigError("TEAMS_WEBHOOK_URL is not set (see .env)")
        card = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": n.subject, "weight": "Bolder", "size": "Medium", "wrap": True,
                         "color": self.COLORS.get(n.severity, "Default")},
                        {"type": "TextBlock", "text": n.text[:3000], "wrap": True, "fontType": "Monospace"},
                    ],
                },
            }],
        }
        try:
            r = self._http.post(self.url, json=card)
        except httpx.TransportError as e:
            raise NotifyError(f"Teams not reachable: {e}")
        if r.status_code >= 300:
            raise NotifyError(f"Teams refused the message ({r.status_code}): {r.text[:200]}")


class Router(Notifier):
    """Sends each notification through the notifier for its channel."""

    def __init__(self, email: EmailNotifier | None = None, teams: TeamsNotifier | None = None):
        self.email, self.teams = email, teams

    def send(self, n: Notification) -> None:
        target = self.email if n.channel == "email" else self.teams if n.channel == "teams" else None
        if target is None:
            raise NotifyConfigError(f"no notifier for channel {n.channel!r}")
        target.send(n)


def default_router(recipients: list[str], env=os.environ) -> Router:
    return Router(EmailNotifier(recipients, env), TeamsNotifier(env.get("TEAMS_WEBHOOK_URL", "")))


# ------------------------------------------------------------------ message text

@dataclass
class AlertView:
    id: int
    severity: str
    keyword: str
    matched_text: str
    context: str
    repeat_count: int
    report_label: str  # "12H 2026-11-23 Day" or the file name when it could not be parsed
    file_name: str
    received_at: datetime | None
    report_id: int | None = None
    channel: str = "email"


def _fmt_time(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "?"


def alert_text(a: AlertView) -> str:
    rep = f"   [seen {a.repeat_count}x before in the last 7 days]" if a.repeat_count else ""
    ctx = "\n".join("      " + l for l in (a.context or "").splitlines())
    return (f"ALERT #{a.id}  keyword '{a.keyword}' ({a.severity}){rep}\n"
            f"    matched: {a.matched_text}\n{ctx}\n"
            f"    acknowledge: python -m alert_service.cli ack {a.id} --note \"what you did\"")


def format_report_alerts(alerts: list[AlertView], base_url: str = "", max_alerts: int = 25) -> Notification:
    first = alerts[0]
    sev = max((a.severity for a in alerts), key=["info", "warning", "critical"].index)
    subject = f"[{sev.upper()}] {first.report_label}: {len(alerts)} keyword alert{'s' if len(alerts) != 1 else ''}"
    shown = alerts[:max_alerts]
    head = (f"Report: {first.report_label}\nFile: {first.file_name}\nReceived: {_fmt_time(first.received_at)}\n"
            + (f"Dashboard: {base_url}\n" if base_url else ""))
    text = head + "\n" + "\n\n".join(alert_text(a) for a in shown)
    if len(alerts) > len(shown):
        text += f"\n\n... and {len(alerts) - len(shown)} more (python -m alert_service.cli alerts list)"
    body = "".join(f"<p><b>ALERT #{a.id}</b> keyword <b>{html.escape(a.keyword)}</b> ({a.severity})"
                   + (f" <i>seen {a.repeat_count}x before in the last 7 days</i>" if a.repeat_count else "")
                   + f"<br>matched: <mark>{html.escape(a.matched_text)}</mark></p><pre>{html.escape(a.context or '')}</pre>"
                   for a in shown)
    page = (f"<p><b>{html.escape(first.report_label)}</b><br>File: {html.escape(first.file_name)}<br>"
            f"Received: {_fmt_time(first.received_at)}</p>{body}")
    return Notification(subject, text, page, sev)


def format_digest(alerts: list[AlertView], day: str, base_url: str = "") -> Notification:
    by_report: dict[str, list[AlertView]] = {}
    for a in alerts:
        by_report.setdefault(a.report_label, []).append(a)
    lines = [f"Daily digest {day}: {len(alerts)} warning alert{'s' if len(alerts) != 1 else ''} in {len(by_report)} report(s)"]
    if base_url:
        lines.append(f"Dashboard: {base_url}")
    for label, items in by_report.items():
        lines.append(f"\n== {label}  (file {items[0].file_name})")
        lines += [alert_text(a) for a in items[:15]]
        if len(items) > 15:
            lines.append(f"   ... and {len(items) - 15} more")
    text = "\n".join(lines)
    return Notification(f"[WARNING digest] {len(alerts)} keyword alert{'s' if len(alerts) != 1 else ''} - {day}",
                        text, "<pre>" + html.escape(text) + "</pre>", "warning")


def format_system(subject: str, lines: list[str]) -> Notification:
    text = "\n".join(lines)
    return Notification(f"[ATTENTION] {subject}", text, "<pre>" + html.escape(text) + "</pre>", "system")
