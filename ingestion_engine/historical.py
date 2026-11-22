"""Historical importer (Phase 3): load the archive (2013 to today) safely.

  python -m ingestion_engine.historical --dry-run --csv dryrun.csv      # look first, writes nothing
  python -m ingestion_engine.historical                                # import
  python -m ingestion_engine.historical --reparse-failed               # after adding a template
  python -m ingestion_engine.historical --set-control pause|run|stop   # from a second window
  python -m ingestion_engine.historical --list-failed

Rules (Master Plan section 5):
- Idempotent: every file is identified by its SHA-256 hash. Known hashes are skipped, so the
  import can be stopped (Ctrl+C) and started again at any time, or run twice, without duplicates.
- One database transaction per file. A bad file never stops the run; it goes to
  failed_parse_queue (raw cells are still stored).
- Dates come from the report content. Folder names (year, month) are only hints and are
  counted as informational "folder mismatches".
- This importer never creates alerts. Alerts only exist for Outlook reports (Phase 6).
"""
from __future__ import annotations

import argparse
import calendar
import csv
import logging
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from sqlalchemy import desc, func, select

from database.db import session_scope
from database.models import FailedParseQueue, JobRun, SourceFile
from parser_engine.pipeline import parse_file
from parser_engine.registry import Registry, default_registry
from parser_engine.store import save_outcome

from .hashing import sha256_file
from .settings import get_setting, set_setting

log = logging.getLogger("historical_import")

EXCEL_EXTENSIONS = {".xls", ".xlsx", ".xlsm", ".xlsb"}
MAX_CONSECUTIVE_ERRORS = 10  # systemic problem (for example the database is gone): stop the run
JOB_UPDATE_EVERY = 25


# ------------------------------------------------------------------ scanning

def scan_files(root: Path) -> Iterator[Path]:
    """All Excel files under root, any depth, in a stable order. Skips Office lock files
    (~$name.xlsx), hidden files and non-Excel files."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.startswith(("~$", ".")):
                continue
            if Path(name).suffix.lower() in EXCEL_EXTENSIONS:
                yield Path(dirpath) / name


_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTH_RX = re.compile(r"\b(" + "|".join(m[:3] for m in _MONTHS) + r")[a-z]*\b", re.I)


def folder_hint(path: Path, root: Path) -> tuple[int | None, int | None]:
    """(year, month) guessed from folder names such as 2013 / January. Only a hint."""
    parts = [root.name, *path.relative_to(root).parts[:-1]]
    year = month = None
    for part in parts:
        if year is None and re.fullmatch(r"(19|20)\d{2}", part.strip()):
            year = int(part)
            continue
        if month is None:
            p = part.strip().lower()
            if re.fullmatch(r"\d{1,2}", p) and 1 <= int(p) <= 12:
                month = int(p)
            else:
                m = _MONTH_RX.search(p)
                if m:
                    month = [i for n, i in _MONTHS.items() if n.startswith(m.group(1).lower())][0]
    return year, month


# ------------------------------------------------------------------ summary

@dataclass
class ImportSummary:
    root: str = ""
    dry_run: bool = False
    total_files: int = 0
    processed: int = 0
    imported: int = 0  # parsed and saved (dry run: would be imported)
    reparsed: int = 0  # previously failed, now parsed
    queued_failed: int = 0  # saved, but could not be parsed (dry run: would fail)
    skipped_known: int = 0  # hash already in the database
    duplicates_in_run: int = 0  # same content found again in another folder
    errors: int = 0  # database or file-system problems for a single file
    folder_mismatch: int = 0  # report date does not match the folder name (informational)
    stopped: bool = False
    aborted: bool = False
    by_reason: Counter = field(default_factory=Counter)
    by_template: Counter = field(default_factory=Counter)
    by_format: Counter = field(default_factory=Counter)
    error_samples: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)  # per-file detail (for --csv)

    def as_dict(self) -> dict:
        return {
            "root": self.root, "dry_run": self.dry_run, "total_files": self.total_files,
            "processed": self.processed, "imported": self.imported, "reparsed": self.reparsed,
            "queued_failed": self.queued_failed, "skipped_known": self.skipped_known,
            "duplicates_in_run": self.duplicates_in_run, "errors": self.errors,
            "folder_mismatch": self.folder_mismatch, "stopped": self.stopped, "aborted": self.aborted,
            "by_reason": dict(self.by_reason), "by_template": dict(self.by_template),
            "by_format": dict(self.by_format), "error_samples": self.error_samples[:20],
        }


# ------------------------------------------------------------------ job_runs

def _job_start(summary: ImportSummary) -> int:
    with session_scope() as s:
        job = JobRun(job_type="historical_import", status="running", details=summary.as_dict())
        s.add(job)
        s.flush()
        return job.id


def _job_update(job_id: int, summary: ImportSummary, status: str | None = None, error: str | None = None) -> None:
    with session_scope() as s:
        job = s.get(JobRun, job_id)
        job.details = summary.as_dict()
        if status:
            job.status = status
            job.finished_at = datetime.now(timezone.utc)
        if error:
            job.error = error[:2000]


# ------------------------------------------------------------------ the import

def run_import(
    root: str | Path | None = None,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    reparse_failed: bool = False,
    registry: Registry | None = None,
    control: Callable[[], str] | None = None,
    collect_rows: bool = False,
    progress: Callable[[ImportSummary, Path], None] | None = None,
    pause_poll_seconds: float = 1.0,
) -> ImportSummary:
    """Import every Excel file below `root` (default: setting historical_archive_root).

    limit: maximum number of NEW files to parse in this run (skipped files do not count).
    control: returns 'run' | 'pause' | 'stop'; default reads the app_settings key
             historical_import_control, so the Settings screen can pause and stop a run."""
    registry = registry or default_registry
    root_str = str(root) if root else get_setting("historical_archive_root", "")
    if not root_str:
        raise ValueError("No archive folder: pass --root or set historical_archive_root in app_settings")
    root_path = Path(root_str)
    if not root_path.is_dir():
        raise ValueError(f"Archive folder not found: {root_path}")

    summary = ImportSummary(root=str(root_path), dry_run=dry_run)
    files = list(scan_files(root_path))
    summary.total_files = len(files)

    with session_scope() as s:  # known files: hash -> (id, status)
        known = {h: (i, st) for h, i, st in s.execute(select(SourceFile.sha256, SourceFile.id, SourceFile.status))}
    seen_in_run: set[str] = set()

    job_id = None
    if not dry_run:
        set_setting("historical_import_control", "run")
        job_id = _job_start(summary)
    if control is None:
        control = (lambda: "run") if dry_run else (lambda: get_setting("historical_import_control", "run"))

    consecutive_errors = 0
    try:
        for path in files:
            state = control()
            while state == "pause":
                time.sleep(pause_poll_seconds)
                state = control()
            if state == "stop":
                summary.stopped = True
                break
            if limit is not None and (summary.imported + summary.reparsed + summary.queued_failed + summary.errors) >= limit:
                break

            row = {"path": str(path), "sha256": "", "outcome": "", "reason": "", "template": "",
                   "format": "", "report_date": "", "lines": "", "folder_year": "", "folder_month": "",
                   "folder_mismatch": ""}
            summary.processed += 1
            try:
                sha = sha256_file(path)
                row["sha256"] = sha
            except OSError as e:  # locked, permission denied, vanished
                summary.errors += 1
                consecutive_errors += 1
                _remember_error(summary, f"{path}: cannot read ({e})")
                row["outcome"], row["reason"] = "error", f"cannot read: {e}"
                _finish_row(summary, row, collect_rows, progress, path)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    summary.aborted = True
                    break
                continue

            if sha in seen_in_run:
                summary.duplicates_in_run += 1
                row["outcome"] = "duplicate_in_run"
                _finish_row(summary, row, collect_rows, progress, path)
                continue
            existing = known.get(sha)
            if existing is not None and not (reparse_failed and existing[1] == "failed"):
                summary.skipped_known += 1
                row["outcome"] = "skipped_known"
                seen_in_run.add(sha)
                _finish_row(summary, row, collect_rows, progress, path)
                continue
            seen_in_run.add(sha)

            outcome = parse_file(path, registry)
            row["format"] = outcome.file_format or ""
            row["template"] = outcome.template_name or ""
            if outcome.file_format:
                summary.by_format[outcome.file_format] += 1
            if outcome.template_name:
                summary.by_template[outcome.template_name] += 1
            if outcome.ok:
                rd = outcome.report.report_date
                row["report_date"], row["lines"] = rd.isoformat(), len(outcome.report.lines)
                fy, fm = folder_hint(path, root_path)
                row["folder_year"], row["folder_month"] = fy or "", fm or ""
                if (fy and rd.year != fy) or (fm and rd.month != fm):
                    summary.folder_mismatch += 1
                    row["folder_mismatch"] = "yes"
            else:
                row["reason"] = outcome.failure.reason

            if dry_run:
                if outcome.ok:
                    summary.imported += 1
                    row["outcome"] = "would_import"
                else:
                    summary.queued_failed += 1
                    summary.by_reason[outcome.failure.reason] += 1
                    row["outcome"] = "would_fail"
                _finish_row(summary, row, collect_rows, progress, path)
                continue

            try:
                with session_scope() as s:  # one transaction per file
                    sf = s.get(SourceFile, existing[0]) if existing else None
                    if sf is None:
                        sf = SourceFile(origin="archive", file_name=path.name, sha256=sha, size_bytes=path.stat().st_size)
                    sf.path = str(path)
                    save_outcome(s, sf, outcome, registry)
                    new_id, new_status = sf.id, sf.status
                known[sha] = (new_id, new_status)
                consecutive_errors = 0
            except Exception as e:
                summary.errors += 1
                consecutive_errors += 1
                _remember_error(summary, f"{path}: {type(e).__name__}: {e}")
                row["outcome"], row["reason"] = "error", f"{type(e).__name__}: {e}"
                seen_in_run.discard(sha)  # not saved: a later run (or the same one) may retry
                _finish_row(summary, row, collect_rows, progress, path)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    summary.aborted = True
                    break
                continue

            if outcome.ok:
                if existing is not None:
                    summary.reparsed += 1
                else:
                    summary.imported += 1
                row["outcome"] = "imported" if existing is None else "reparsed"
            else:
                summary.queued_failed += 1
                summary.by_reason[outcome.failure.reason] += 1
                row["outcome"] = "failed"
            _finish_row(summary, row, collect_rows, progress, path)
            if job_id and summary.processed % JOB_UPDATE_EVERY == 0:
                _job_update(job_id, summary)
    except BaseException as e:  # includes Ctrl+C: record it, then let it propagate
        if job_id:
            _job_update(job_id, summary, status="failed", error=f"interrupted: {type(e).__name__}: {e}")
        raise
    if job_id:
        err = "aborted: too many consecutive errors" if summary.aborted else None
        _job_update(job_id, summary, status="failed" if summary.aborted else "success", error=err)
    return summary


def _remember_error(summary: ImportSummary, msg: str) -> None:
    log.error(msg)
    if len(summary.error_samples) < 50:
        summary.error_samples.append(msg)


def _finish_row(summary, row, collect_rows, progress, path) -> None:
    if collect_rows:
        summary.rows.append(row)
    if progress:
        progress(summary, path)


# ------------------------------------------------------------------ failed files

def failed_summary(samples: int = 3) -> list[dict]:
    """Unresolved failures grouped by reason, with example paths."""
    out = []
    with session_scope() as s:
        groups = s.execute(
            select(FailedParseQueue.reason, func.count().label("n"))
            .where(FailedParseQueue.resolved.is_(False))
            .group_by(FailedParseQueue.reason)
            .order_by(desc("n"))
        ).all()
        for reason, n in groups:
            paths = s.scalars(
                select(SourceFile.path)
                .join(FailedParseQueue, FailedParseQueue.source_file_id == SourceFile.id)
                .where(FailedParseQueue.reason == reason, FailedParseQueue.resolved.is_(False))
                .order_by(SourceFile.path)
                .limit(samples)
            ).all()
            out.append({"reason": reason, "count": n, "examples": paths})
    return out


# ------------------------------------------------------------------ command line

def _print_summary(sm: ImportSummary) -> None:
    mode = "DRY RUN (nothing was written)" if sm.dry_run else "IMPORT"
    print(f"\n=== {mode} finished: {sm.root}")
    print(f"files found            : {sm.total_files}")
    print(f"processed              : {sm.processed}")
    label = "would import" if sm.dry_run else "imported"
    print(f"{label:<22} : {sm.imported}")
    if sm.reparsed:
        print(f"re-parsed (was failed) : {sm.reparsed}")
    print(f"{'would fail' if sm.dry_run else 'failed (queued)':<22} : {sm.queued_failed}")
    print(f"skipped, already known : {sm.skipped_known}")
    print(f"duplicates in this run : {sm.duplicates_in_run}")
    print(f"errors                 : {sm.errors}")
    print(f"folder/date mismatches : {sm.folder_mismatch} (informational)")
    for title, counter in (("failure reasons", sm.by_reason), ("templates used", sm.by_template), ("file formats", sm.by_format)):
        if counter:
            print(f"{title}: " + ", ".join(f"{k}={v}" for k, v in counter.most_common()))
    if sm.stopped:
        print("STOPPED by control setting. Run again to continue; finished files are skipped.")
    if sm.aborted:
        print("ABORTED after repeated errors. Check the database connection and the log, then run again.")
    for e in sm.error_samples[:5]:
        print(f"  error: {e}")
    if sm.queued_failed and not sm.dry_run:
        print("Next: python -m ingestion_engine.historical --list-failed")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion_engine.historical", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", help="archive folder (default: setting historical_archive_root)")
    ap.add_argument("--dry-run", action="store_true", help="parse everything but write nothing")
    ap.add_argument("--limit", type=int, help="parse at most N new files in this run")
    ap.add_argument("--reparse-failed", action="store_true", help="retry files that failed before")
    ap.add_argument("--csv", help="write one row per file to this CSV file")
    ap.add_argument("--set-control", choices=["run", "pause", "stop"], help="pause/resume/stop a running import")
    ap.add_argument("--list-failed", action="store_true", help="show unresolved failures grouped by reason")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    from parser_engine.templates import load_all
    from dotenv import load_dotenv

    load_dotenv()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_dir = os.environ.get("LOG_DIR")
    if log_dir:
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(Path(log_dir) / "historical_import.log", encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(level=logging.WARNING, handlers=handlers, format="%(asctime)s %(levelname)s %(message)s")

    if args.set_control:
        set_setting("historical_import_control", args.set_control)
        print(f"control set to {args.set_control}")
        return 0
    if args.list_failed:
        rows = failed_summary()
        if not rows:
            print("No unresolved failures.")
        for r in rows:
            print(f"{r['count']:>6}  {r['reason']}")
            for p in r["examples"]:
                print(f"          e.g. {p}")
        return 0

    load_all()
    started = time.time()

    def show(sm: ImportSummary, path: Path) -> None:
        if not args.quiet and sm.processed % 50 == 0:
            print(f"  {sm.processed}/{sm.total_files}  imported={sm.imported} failed={sm.queued_failed} "
                  f"skipped={sm.skipped_known + sm.duplicates_in_run} errors={sm.errors}  "
                  f"({time.time() - started:.0f}s)", flush=True)

    try:
        sm = run_import(args.root, dry_run=args.dry_run, limit=args.limit, reparse_failed=args.reparse_failed,
                        collect_rows=bool(args.csv), progress=show)
    except ValueError as e:
        print(f"error: {e}")
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Finished files are saved; run again to continue.")
        return 130
    if args.csv and sm.rows:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(sm.rows[0].keys()))
            w.writeheader()
            w.writerows(sm.rows)
        print(f"per-file report written to {args.csv}")
    _print_summary(sm)
    return 1 if (sm.errors or sm.aborted) else 0


if __name__ == "__main__":
    sys.exit(main())
