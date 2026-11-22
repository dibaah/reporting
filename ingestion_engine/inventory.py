"""Phase 0 discovery: what do the archive files really look like?  (no database needed)

  python -m ingestion_engine.inventory "D:\\reports" --out inventory_out

Writes two CSV files (open them in Excel):
  inventory.csv     one row per file: year, month, extension, real format, size, sheets,
                    hidden sheets, first rows fingerprint, or the reason it could not be opened
  layouts.csv       files grouped by layout: sheet names + the first rows of text (digits
                    masked, so dates and readings do not split a layout). Shows how many
                    files, which years, the exact-header variants, and three example paths.

Use layouts.csv to decide how many templates are needed (one per layout/era), then look at an
example with:  python -m parser_engine.cli --rows 40 "<example path>"

Do NOT commit these CSV files: they contain real report text and paths.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

from parser_engine.helpers import parse_date, to_number
from parser_engine.workbook import Workbook, WorkbookError, detect_format, load_workbook

from .historical import folder_hint, scan_files

ROWS_FOR_HEADER_FP = 30
ROWS_FOR_TITLE_FP = 3


def _norm_cell(v) -> str:
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return "<date>"
    if isinstance(v, bool):
        return "<bool>"
    if isinstance(v, (int, float)):
        return "<num>"
    if isinstance(v, str):  # dates and numbers stored as text look the same as real date/number cells
        if parse_date(v) is not None:
            return "<date>"
        if to_number(v) is not None:
            return "<num>"
    s = re.sub(r"\s+", " ", str(v).lower()).strip()
    s = re.sub(r"\d+", "#", s)  # remaining digits (times, ids) must not split a layout
    return s[:60]


def _first_rows(wb: Workbook, n: int) -> list[str]:
    sheet = next((s for s in wb.sheets if not s.hidden), wb.sheets[0] if wb.sheets else None)
    out: list[str] = []
    if sheet is None:
        return out
    for row in sheet.rows:
        cells = [_norm_cell(v) for v in row if v is not None]
        if cells:
            out.append("|".join(cells))
            if len(out) >= n:
                break
    return out


def _fp(parts: list[str]) -> str:
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:10]


def inspect_file(path: Path, root: Path, allow_libreoffice: bool) -> dict:
    year, month = folder_hint(path, root)
    row = {
        "path": str(path), "folder_year": year or "", "folder_month": month or "",
        "extension": path.suffix.lower(), "real_format": "", "size_kb": round(path.stat().st_size / 1024, 1),
        "sheets": "", "hidden_sheets": "", "n_sheets": "", "rows_first_sheet": "",
        "title_fp": "", "header_fp": "", "layout_key": "", "error": "", "title_text": "",
    }
    try:
        row["real_format"] = detect_format(path)
        wb = load_workbook(path, allow_libreoffice=allow_libreoffice)
    except WorkbookError as e:
        row["error"] = f"{e.reason}: {e.detail[:150]}"
        return row
    except Exception as e:
        row["error"] = f"unexpected: {type(e).__name__}: {e}"[:200]
        return row
    names = [s.name for s in wb.sheets]
    row["real_format"] = wb.file_format
    row["sheets"] = " | ".join(names)
    row["hidden_sheets"] = " | ".join(s.name for s in wb.sheets if s.hidden)
    row["n_sheets"] = len(names)
    row["rows_first_sheet"] = wb.sheets[0].n_rows if wb.sheets else 0
    head = _first_rows(wb, ROWS_FOR_HEADER_FP)
    title = head[:ROWS_FOR_TITLE_FP]
    sheet_key = "|".join(n.lower().strip() for n in names)
    row["title_fp"] = _fp(title)
    row["header_fp"] = _fp([sheet_key, *head])
    row["layout_key"] = _fp([sheet_key, *title])
    row["title_text"] = " // ".join(title)[:200]
    return row


def run_inventory(root: Path, out_dir: Path, allow_libreoffice: bool = False, progress=None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = list(scan_files(root))
    rows = []
    for i, path in enumerate(files, 1):
        rows.append(inspect_file(path, root, allow_libreoffice))
        if progress and i % 100 == 0:
            progress(i, len(files))

    inv_path, lay_path = out_dir / "inventory.csv", out_dir / "layouts.csv"
    with open(inv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["path"])
        w.writeheader()
        w.writerows(rows)

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["layout_key"]:
            groups[r["layout_key"]].append(r)
    layouts = []
    for key, g in groups.items():
        years = sorted({r["folder_year"] for r in g if r["folder_year"] != ""})
        layouts.append({
            "layout_key": key, "files": len(g),
            "years": ("" if not years else str(years[0]) if years[0] == years[-1] else f"{years[0]}-{years[-1]}"),
            "extensions": " ".join(sorted({r["extension"] for r in g})),
            "real_formats": " ".join(sorted({r["real_format"] for r in g})),
            "sheet_names": g[0]["sheets"], "title_rows": g[0]["title_text"],
            "header_variants": len({r["header_fp"] for r in g}),
            "example_1": g[0]["path"], "example_2": g[len(g) // 2]["path"], "example_3": g[-1]["path"],
        })
    layouts.sort(key=lambda d: -d["files"])
    with open(lay_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(layouts[0].keys()) if layouts else ["layout_key"])
        w.writeheader()
        w.writerows(layouts)

    errors = [r for r in rows if r["error"]]
    by_ext: dict[str, int] = defaultdict(int)
    by_fmt: dict[str, int] = defaultdict(int)
    for r in rows:
        by_ext[r["extension"]] += 1
        by_fmt[r["real_format"] or "?"] += 1
    ok_ext = {"xls": {".xls"}, "xlsx": {".xlsx", ".xlsm"}, "xlsb": {".xlsb"}}
    mismatched = sum(1 for r in rows if not r["error"] and r["real_format"] in ok_ext
                     and r["extension"] not in ok_ext[r["real_format"]])
    return {"files": len(rows), "layouts": len(layouts), "unreadable": len(errors),
            "by_extension": dict(by_ext), "by_real_format": dict(by_fmt), "wrong_extension": mismatched,
            "inventory_csv": str(inv_path), "layouts_csv": str(lay_path), "top_layouts": layouts[:10]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion_engine.inventory", description=__doc__.split("\n\n")[0])
    ap.add_argument("root", help="archive folder, for example D:\\reports")
    ap.add_argument("--out", default="inventory_out", help="folder for the CSV files")
    ap.add_argument("--libreoffice", action="store_true",
                    help="also try LibreOffice on damaged files (slow; default off for the inventory)")
    args = ap.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"error: folder not found: {root}")
        return 2
    started = time.time()
    res = run_inventory(root, Path(args.out), args.libreoffice,
                        progress=lambda i, n: print(f"  {i}/{n} ({time.time() - started:.0f}s)", flush=True))
    print(f"\nfiles: {res['files']}   distinct layouts: {res['layouts']}   unreadable: {res['unreadable']}")
    print("by extension      :", res["by_extension"])
    print("by real format    :", res["by_real_format"], f"(wrong extension: {res['wrong_extension']})")
    for lay in res["top_layouts"]:
        print(f"  {lay['files']:>6} files  years {lay['years'] or '?':<11} sheets: {lay['sheet_names'][:50]}")
    print(f"\nwritten: {res['inventory_csv']}\n         {res['layouts_csv']}")
    print("Keep these files private (they contain real report text).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
