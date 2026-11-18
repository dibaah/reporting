"""Inspect report files without a database.

  python -m parser_engine.cli FILE [FILE ...] [--rows 25] [--json]

Shows the detected format, every sheet with its first rows (this is what you need to write
a template), which template matches, and the parse result.
"""
from __future__ import annotations

import argparse
import sys

from .pipeline import outcome_json, parse_file
from .templates import load_all
from .workbook import WorkbookError, load_workbook


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="parser_engine.cli")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--rows", type=int, default=0, help="print the first N non-empty rows of each sheet")
    ap.add_argument("--json", action="store_true", help="print the full parse result as JSON")
    args = ap.parse_args(argv)
    load_all()
    exit_code = 0
    for f in args.files:
        print(f"=== {f}")
        if args.rows:
            try:
                wb = load_workbook(f)
                print(f"format: {wb.file_format}  warnings: {wb.warnings or '-'}")
                for s in wb.sheets:
                    print(f"-- sheet {s.index} {s.name!r} hidden={s.hidden} rows={s.n_rows} merged={len(s.merged)}")
                    shown = 0
                    for r, row in enumerate(s.rows):
                        if any(v is not None for v in row):
                            print(f"   r{r + 1:>3}: {[v for v in row]}")
                            shown += 1
                            if shown >= args.rows:
                                break
            except WorkbookError as e:
                print(f"cannot read: {e.reason}: {e.detail}")
        out = parse_file(f)
        if args.json:
            print(outcome_json(out))
        elif out.ok:
            r = out.report
            print(f"OK template={out.template_name} confidence={out.confidence:.2f} type={r.report_type} "
                  f"date={r.report_date} shift={r.shift} lines={len(r.lines)} metrics={len(r.metrics)}")
            for w in r.warnings:
                print(f"   warning: {w}")
        else:
            print(f"FAILED {out.failure.reason}: {out.failure.detail.splitlines()[0] if out.failure.detail else ''}")
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
