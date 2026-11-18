"""Seed report types and default settings. Safe to run repeatedly (never overwrites)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from database.db import session_scope
from database.models import AppSetting, ReportType

REPORT_TYPES = [
    # expected_schedule stays NULL until Phase 0 confirms the real arrival times.
    {"name": "12H", "frequency": "12h", "expected_schedule": None},
    {"name": "Biweekly", "frequency": "biweekly", "expected_schedule": None},
    {"name": "Monthly", "frequency": "monthly", "expected_schedule": None},
]

DEFAULT_SETTINGS = {
    "historical_archive_root": ("", "Folder the historical importer scans (year/month subfolders)"),
    "outlook_folder": ("", "Mailbox folder the live importer reads"),
    "live_import_interval_minutes": ("5", "How often Outlook is checked"),
    "keyword_check_interval_minutes": ("5", "How often new reports are scanned; also daily digest time source"),
    "alerts_active_from": ("", "Forward-only cutoff; stamped when alerts are first enabled"),
    "backup_location": ("", "Folder or network share for backups"),
    "backup_interval_minutes": ("1440", "How often to back up (1440 = daily)"),
    "backup_retention_count": ("14", "How many backups to keep"),
    "notification_email": ("", "Where alert emails are sent"),
    "missing_report_grace_minutes": ("120", "How late a report may be before a warning is sent"),
    "theme": ("system", "light | dark | system"),
}


def run() -> dict:
    added = {"report_types": 0, "settings": 0}
    with session_scope() as s:
        for rt in REPORT_TYPES:
            if s.scalar(select(ReportType.id).where(ReportType.name == rt["name"])) is None:
                s.add(ReportType(**rt))
                added["report_types"] += 1
        for key, (value, desc) in DEFAULT_SETTINGS.items():
            res = s.execute(
                insert(AppSetting)
                .values(key=key, value=value, description=desc)
                .on_conflict_do_nothing(index_elements=["key"])
                .returning(AppSetting.key)
            )
            added["settings"] += 1 if res.first() is not None else 0
    return added


if __name__ == "__main__":
    print(run())
