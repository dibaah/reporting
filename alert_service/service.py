"""Alert worker: scan new mail, send alerts and the daily digest, check for missing reports.

  python -m alert_service.service            # keep running, every keyword_check_interval_minutes
  python -m alert_service.service --once     # one cycle (Task Scheduler)

Nothing is sent until you run:  python -m alert_service.cli enable
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from database.db import session_scope
from database.models import JobRun
from ingestion_engine.settings import get_setting, set_setting

from . import engine
from .notify import Notifier, default_router

log = logging.getLogger("alerts")


def run_once(notifier: Notifier | None = None, session_factory=session_scope, only_sources=None) -> engine.CycleResult:
    if notifier is None:
        with session_factory() as s:
            notifier = default_router(engine.recipients(s))
    result = engine.run_cycle(notifier, session_factory=session_factory, only_sources=only_sources)
    with session_factory() as s:
        set_setting("alert_check_last_ok_at", datetime.now(timezone.utc).isoformat(timespec="seconds"), s)
        if result.active:
            s.add(JobRun(job_type="alerts", status="failed" if result.errors else "success",
                         finished_at=datetime.now(timezone.utc), details=result.as_dict(),
                         error="; ".join(result.errors[:3])[:2000] or None))
    return result


def run_forever(stop: threading.Event | None = None, once: bool = False, notifier_factory=None) -> int:
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            interval = max(30, int(get_setting("keyword_check_interval_minutes", "5")) * 60)
        except ValueError:
            interval = 300
        try:
            r = run_once(notifier_factory() if notifier_factory else None)
            if r.active:
                log.warning("alert cycle: %s", r.as_dict())
        except Exception:  # never let the loop die
            log.exception("unexpected error in alert cycle")
        if once:
            return 0
        stop.wait(interval)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="alert_service.service", description=__doc__.split("\n\n")[0])
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if os.environ.get("LOG_DIR"):
        try:
            Path(os.environ["LOG_DIR"]).mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(Path(os.environ["LOG_DIR"]) / "alerts.log", encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass
    return run_forever(stop, args.once)


if __name__ == "__main__":
    sys.exit(main())
