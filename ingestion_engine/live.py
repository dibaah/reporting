"""Outlook live importer (Phase 5): new reports arrive in the database by themselves.

  python -m ingestion_engine.live            # keep running, checking every live_import_interval_minutes
  python -m ingestion_engine.live --once     # one cycle (for Task Scheduler)
  python -m ingestion_engine.live --source eml --eml-folder C:\\exports   # no Outlook needed

Per attachment: skip if the same file is known (SHA-256), store the original file in RAW_STORE_PATH,
parse it, save it. One transaction per attachment. The mailbox is never modified: "already
processed" lives in our database. Failed attachments go to failed_parse_queue (the alert service
warns you about them) and their raw cells are still scanned for keywords.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, ContextManager, Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.db import session_scope
from database.models import JobRun, SourceFile
from parser_engine.pipeline import parse_file
from parser_engine.registry import Registry, default_registry
from parser_engine.store import save_outcome

from .hashing import sha256_file  # noqa: F401  (re-exported for callers)
from .mail.base import MailConfigError, MailSource, MailSourceError
from .settings import get_setting, set_setting

import hashlib

log = logging.getLogger("live_import")

EXCEL_EXTENSIONS = {".xls", ".xlsx", ".xlsm", ".xlsb"}
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
DEFAULT_OVERLAP = timedelta(days=2)  # re-list the last days each cycle; known files are skipped cheaply

SessionFactory = Callable[[], ContextManager[Session]]


@dataclass
class LiveSummary:
    messages: int = 0
    attachments: int = 0  # Excel attachments seen
    ignored: int = 0  # not Excel (signature images, PDFs, ...)
    imported: int = 0
    failed: int = 0  # saved, but could not be parsed
    duplicates: int = 0  # same file content already in the database
    skipped_known: int = 0  # same mail + file name already imported (no download)
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.imported or self.failed or self.errors)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("messages", "attachments", "ignored", "imported", "failed",
                                              "duplicates", "skipped_known", "errors")} | {"error_samples": self.error_samples[:10]}


def raw_store_root() -> Path:
    return Path(os.environ.get("RAW_STORE_PATH") or "raw_store")


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", Path(name).name)[:120] or "attachment"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _since(session: Session, overlap: timedelta) -> datetime | None:
    last = session.scalar(select(func.max(SourceFile.received_at)).where(SourceFile.origin == "outlook"))
    floor_text = get_setting("live_import_since", "", session)
    floor = None
    if floor_text:
        try:
            floor = datetime.combine(date.fromisoformat(floor_text), datetime.min.time(), tzinfo=timezone.utc)
        except ValueError:
            log.warning("setting live_import_since is not a date (YYYY-MM-DD): %r", floor_text)
    if last is None:
        return floor
    start = last - overlap
    return max(start, floor) if floor else start


def run_cycle(
    source: MailSource,
    *,
    registry: Registry | None = None,
    session_factory: SessionFactory = session_scope,
    raw_store: Path | None = None,
    overlap: timedelta = DEFAULT_OVERLAP,
) -> LiveSummary:
    """Import everything new from the mail source. Raises MailSourceError/MailConfigError when the
    mailbox cannot be read; problems with single attachments are counted and do not stop the cycle."""
    registry = registry or default_registry
    raw_store = raw_store or raw_store_root()
    summary = LiveSummary()

    with session_factory() as s:
        since = _since(s, overlap)
        known_pairs = {
            (e, n) for e, n in s.execute(
                select(SourceFile.email_id, SourceFile.file_name).where(
                    SourceFile.origin == "outlook", SourceFile.email_id.is_not(None),
                    *( [SourceFile.received_at >= since] if since else [] )))
        }

    for msg in source.messages(since):
        summary.messages += 1
        seen_names: dict[str, int] = {}
        for att in msg.attachments:
            if Path(att.name).suffix.lower() not in EXCEL_EXTENSIONS:
                summary.ignored += 1
                continue
            summary.attachments += 1
            n = seen_names[att.name] = seen_names.get(att.name, 0) + 1
            name = att.name if n == 1 else f"{Path(att.name).stem}_{n}{Path(att.name).suffix}"
            if (msg.id, name) in known_pairs:
                summary.skipped_known += 1
                continue
            try:
                data = att.read()
                if len(data) > MAX_ATTACHMENT_BYTES:
                    raise ValueError(f"attachment is {len(data) // 1024 // 1024} MB, over the limit")
                sha = hashlib.sha256(data).hexdigest()
                with session_factory() as s:
                    if s.scalar(select(SourceFile.id).where(SourceFile.sha256 == sha)) is not None:
                        summary.duplicates += 1
                        continue
                dest = raw_store / "outlook" / f"{msg.received_at:%Y}" / f"{msg.received_at:%m}" / f"{sha[:12]}_{_safe_name(name)}"
                _write_atomic(dest, data)
                outcome = parse_file(dest, registry)
                with session_factory() as s:
                    sf = SourceFile(origin="outlook", email_id=msg.id, file_name=name, sha256=sha, size_bytes=len(data),
                                    path=str(dest), received_at=msg.received_at)
                    save_outcome(s, sf, outcome, registry)
                known_pairs.add((msg.id, name))
                if outcome.ok:
                    summary.imported += 1
                else:
                    summary.failed += 1
                    log.warning("could not parse %s (%s): %s", name, msg.subject, outcome.failure.reason)
            except MailSourceError:
                raise  # the mailbox itself is in trouble: stop this cycle, retry later
            except Exception as e:
                summary.errors += 1
                msg_text = f"{name} / {msg.subject!r}: {type(e).__name__}: {e}"
                log.error(msg_text)
                if len(summary.error_samples) < 20:
                    summary.error_samples.append(msg_text)
    return summary


# ------------------------------------------------------------------ one pass with bookkeeping

def _record_job(summary: LiveSummary | None, status: str, error: str | None = None) -> None:
    with session_scope() as s:
        s.add(JobRun(job_type="live_import", status=status, finished_at=datetime.now(timezone.utc),
                     details=summary.as_dict() if summary else None, error=(error or None) and error[:2000]))


def run_once(source: MailSource | None = None, registry: Registry | None = None) -> LiveSummary:
    """One full pass: build the source (unless given), import, update the heartbeat, log activity.
    job_runs gets a row only when something was imported or went wrong (not every 5 minutes)."""
    from .mail.factory import make_source

    own = source is None
    source = source or make_source()
    try:
        summary = run_cycle(source, registry=registry)
    except (MailSourceError, MailConfigError) as e:
        _record_job(None, "failed", f"{type(e).__name__}: {e}")
        raise
    finally:
        if own:
            source.close()
    set_setting("live_import_last_ok_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if summary.active:
        _record_job(summary, "failed" if summary.errors and not (summary.imported or summary.failed) else "success",
                    "; ".join(summary.error_samples[:3]) or None)
    return summary


def run_forever(stop: threading.Event | None = None, once: bool = False, source_factory=None) -> int:
    stop = stop or threading.Event()
    backoff = 0
    while not stop.is_set():
        wait = 0.0
        try:
            interval = max(30, int(get_setting("live_import_interval_minutes", "5")) * 60)
        except ValueError:
            interval = 300
        try:
            s = run_once(source_factory() if source_factory else None)
            backoff, wait = 0, interval
            if s.active or s.messages:
                log.warning("live import: %s", s.as_dict())
        except MailConfigError as e:
            log.error("configuration problem, fix it and the importer will pick it up: %s", e)
            wait = 300
        except MailSourceError as e:
            backoff = min(600, backoff * 2 or 30)
            log.error("mailbox not reachable (%s); retrying in %ss", e, backoff)
            wait = backoff
        except Exception:  # never let the loop die
            log.exception("unexpected error in live import")
            wait = 120
        if once:
            return 0
        stop.wait(wait)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion_engine.live", description=__doc__.split("\n\n")[0])
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--source", choices=["graph", "outlook", "eml"], help="overrides MAIL_SOURCE")
    ap.add_argument("--folder", help="overrides the outlook_folder setting")
    ap.add_argument("--eml-folder", help="folder with .eml files (with --source eml)")
    args = ap.parse_args(argv)

    from dotenv import load_dotenv
    from parser_engine.templates import load_all

    load_dotenv()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if os.environ.get("LOG_DIR"):
        try:
            Path(os.environ["LOG_DIR"]).mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(Path(os.environ["LOG_DIR"]) / "live_import.log", encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s %(levelname)s %(message)s")
    load_all()

    from .mail.factory import make_source

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):  # not in the main thread / unsupported signal
            pass
    return run_forever(stop, args.once, lambda: make_source(args.source, args.folder, args.eml_folder))


if __name__ == "__main__":
    sys.exit(main())
