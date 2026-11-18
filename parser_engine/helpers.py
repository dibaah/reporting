"""Building blocks for template authors: dates, label/value lookup, tables."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Iterator

from .workbook import Sheet

# ---------------------------------------------------------------- text and numbers


def text_of(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def to_number(v: Any) -> float | int | None:
    """Number from a cell: accepts 1,234.5 / 12,5 / '45 %' style text. None if not numeric."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    s = text_of(v)
    if not s:
        return None
    s = re.sub(r"[%\s]", "", s)
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):
        s = s.replace(",", "")
    elif re.fullmatch(r"-?\d+,\d+", s):
        s = s.replace(",", ".")
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


# ---------------------------------------------------------------- dates

_DATE_FORMATS_ISO = ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"]
_DATE_FORMATS_NAMED = [
    "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %B %Y", "%d %b %y",
    "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d-%B-%Y",
]
_DATE_FORMATS_DAYFIRST = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y"]
_DATE_FORMATS_MONTHFIRST = ["%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y"]


@dataclass
class DateResult:
    value: dt.date | None
    ambiguous: bool = False  # day and month both <= 12 in a purely numeric date


def parse_date_ex(value: Any, dayfirst: bool = True, allow_serial: bool = True) -> DateResult:
    """Parse mixed date formats. `dayfirst` decides 03/04/2013 (day first by default).
    Excel serial numbers (as in .xlsb files) are accepted. Returns ambiguous=True when the
    day/month order could not be proven, so a template can add a warning."""
    if isinstance(value, dt.datetime):
        return DateResult(value.date())
    if isinstance(value, dt.date):
        return DateResult(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if allow_serial and 20000 <= value <= 80000:  # roughly 1954 to 2119
            return DateResult((dt.datetime(1899, 12, 30) + dt.timedelta(days=float(value))).date())
        return DateResult(None)
    s = text_of(value)
    if not s:
        return DateResult(None)
    for candidate in (s, re.split(r"[ T]", s)[0]):
        for fmt in _DATE_FORMATS_ISO + _DATE_FORMATS_NAMED:
            try:
                return DateResult(dt.datetime.strptime(candidate, fmt).date())
            except ValueError:
                pass
        numeric = _DATE_FORMATS_DAYFIRST if dayfirst else _DATE_FORMATS_MONTHFIRST
        other = _DATE_FORMATS_MONTHFIRST if dayfirst else _DATE_FORMATS_DAYFIRST
        for fmt in numeric:
            try:
                d = dt.datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
            swapped = None
            for ofmt in other:
                try:
                    swapped = dt.datetime.strptime(candidate, ofmt).date()
                    break
                except ValueError:
                    pass
            return DateResult(d, ambiguous=swapped is not None and swapped != d)
        for fmt in other:  # only valid the other way round (e.g. 25/12 vs 12/25)
            try:
                return DateResult(dt.datetime.strptime(candidate, fmt).date())
            except ValueError:
                pass
    return DateResult(None)


def parse_date(value: Any, dayfirst: bool = True) -> dt.date | None:
    return parse_date_ex(value, dayfirst).value


# ---------------------------------------------------------------- locating cells

def find_cells(sheet: Sheet, pattern: str, *, flags: int = re.I) -> Iterator[tuple[int, int, str]]:
    rx = re.compile(pattern, flags)
    for r, row in enumerate(sheet.rows):
        for c, v in enumerate(row):
            if v is not None:
                t = text_of(v)
                if rx.search(t):
                    yield r, c, t


def find_cell(sheet: Sheet, pattern: str, *, flags: int = re.I) -> tuple[int, int, str] | None:
    return next(find_cells(sheet, pattern, flags=flags), None)


def value_right_of(sheet: Sheet, r: int, c: int, max_gap: int = 3) -> Any:
    """First non-empty cell to the right of (r, c) within `max_gap` columns."""
    for cc in range(c + 1, c + 1 + max_gap):
        v = sheet.cell(r, cc)
        if v is not None:
            return v
    return None


def value_below(sheet: Sheet, r: int, c: int, max_gap: int = 2) -> Any:
    for rr in range(r + 1, r + 1 + max_gap):
        v = sheet.cell(rr, c)
        if v is not None:
            return v
    return None


def label_value(sheet: Sheet, label_pattern: str, *, max_gap: int = 3) -> Any:
    """Value next to a label such as 'Date:' or 'Superintendent'. Handles 'Label: value'
    inside one cell as well."""
    hit = find_cell(sheet, rf"^\s*(?:{label_pattern})\s*[:\-]?\s*")
    if not hit:
        return None
    r, c, t = hit
    m = re.match(rf"^\s*(?:{label_pattern})\s*[:\-]\s*(\S.*)$", t, re.I)  # 'Label: value' in one cell
    if m:
        return m.group(1).strip()
    return value_right_of(sheet, r, c, max_gap)


# ---------------------------------------------------------------- tables

def read_table(
    sheet: Sheet,
    columns: dict[str, str],
    *,
    header_pattern: str | None = None,
    start_row: int | None = None,
    stop_pattern: str | None = None,
    max_blank_rows: int = 2,
) -> tuple[list[tuple[int, dict[str, Any]]], dict[str, int]]:
    """Read a table whose header row contains cells matching `columns` (name -> regex).

    The header row is the first row where every column pattern matches some cell (or the
    row given by `start_row`). Returns ([(row_index, {name: value}), ...], {name: col}).
    Reading stops after `max_blank_rows` empty rows or when `stop_pattern` matches.
    Cells outside the named columns are returned under the key '_other' as {col: value}."""
    header_row = start_row
    colmap: dict[str, int] = {}
    for r in range(sheet.n_rows) if header_row is None else [header_row]:
        found: dict[str, int] = {}
        for name, pat in columns.items():
            for c, v in enumerate(sheet.rows[r] if r < sheet.n_rows else []):
                if v is not None and re.search(pat, text_of(v), re.I):
                    found[name] = c
                    break
        if len(found) == len(columns):
            header_row, colmap = r, found
            break
    if header_row is None or not colmap:
        return [], {}
    used = set(colmap.values())
    out: list[tuple[int, dict[str, Any]]] = []
    blanks = 0
    stop = re.compile(stop_pattern, re.I) if stop_pattern else None
    for r in range(header_row + 1, sheet.n_rows):
        row = sheet.rows[r]
        if all(v is None for v in row):
            blanks += 1
            if blanks >= max_blank_rows:
                break
            continue
        blanks = 0
        if stop and any(v is not None and stop.search(text_of(v)) for v in row):
            break
        rec: dict[str, Any] = {n: sheet.cell(r, c) for n, c in colmap.items()}
        other = {c: v for c, v in enumerate(row) if v is not None and c not in used}
        if other:
            rec["_other"] = other
        out.append((r, rec))
    return out, colmap
