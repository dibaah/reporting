"""Format-neutral workbook loading for .xlsx, .xlsm, .xls and .xlsb.

The format is detected from the file CONTENT (magic bytes), not the extension, because
old archives often contain files with the wrong extension. If the normal readers fail
and LibreOffice is installed, the file is converted to .xlsx and read again.
"""
from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import tempfile
import warnings
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .contract import REASON_PASSWORD, REASON_UNREADABLE, REASON_UNSUPPORTED

MAX_CELLS_PER_SHEET = 250_000


class WorkbookError(Exception):
    """The file could not be read. `reason` is a failed_parse_queue reason code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass
class Sheet:
    name: str
    index: int
    hidden: bool
    rows: list[list[Any]]  # raw values; merged areas hold the value only in the top-left cell
    merged: list[tuple[int, int, int, int]] = field(default_factory=list)  # r1, c1, r2, c2 inclusive, 0-based
    truncated: bool = False

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    def cell(self, r: int, c: int) -> Any:
        if 0 <= r < len(self.rows) and 0 <= c < len(self.rows[r]):
            return self.rows[r][c]
        return None

    def filled(self) -> "Sheet":
        """Copy of the sheet with every merged area filled with its top-left value.
        Useful for section labels merged down several rows."""
        rows = [list(r) for r in self.rows]
        for r1, c1, r2, c2 in self.merged:
            v = self.cell(r1, c1)
            for r in range(r1, min(r2, len(rows) - 1) + 1):
                for c in range(c1, c2 + 1):
                    while len(rows[r]) <= c:
                        rows[r].append(None)
                    if rows[r][c] is None:
                        rows[r][c] = v
        return Sheet(self.name, self.index, self.hidden, rows, self.merged, self.truncated)


@dataclass
class Workbook:
    path: Path
    file_format: str  # xlsx | xls | xlsb
    sheets: list[Sheet]
    warnings: list[str] = field(default_factory=list)

    def sheet_names(self) -> list[str]:
        return [s.name for s in self.sheets]

    def visible_sheets(self) -> list[Sheet]:
        return [s for s in self.sheets if not s.hidden]

    def get_sheet(self, name: str) -> Sheet | None:
        low = name.strip().lower()
        for s in self.sheets:
            if s.name.strip().lower() == low:
                return s
        return None

    def iter_cells(self) -> Iterator[tuple[Sheet, int, int, Any]]:
        for s in self.sheets:
            for r, row in enumerate(s.rows):
                for c, v in enumerate(row):
                    if v is not None:
                        yield s, r, c, v


# ---------------------------------------------------------------- value cleaning

def clean_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        v = v.replace("\u00a0", " ").strip()
        return v or None
    if isinstance(v, float):
        if v != v:  # NaN
            return None
        if v.is_integer() and abs(v) < 1e15:
            return int(v)
    return v


def _trim(rows: list[list[Any]]) -> list[list[Any]]:
    while rows and all(v is None for v in rows[-1]):
        rows.pop()
    width = 0
    for row in rows:
        for i in range(len(row) - 1, -1, -1):
            if row[i] is not None:
                width = max(width, i + 1)
                break
    return [r[:width] + [None] * (width - len(r[:width])) for r in rows]


# ---------------------------------------------------------------- format detection

def detect_format(path: Path) -> str:
    """Return xlsx | xls | xlsb | ole2 | xml_or_html | text | unknown."""
    with open(path, "rb") as f:
        head = f.read(4096)
    if not head:
        raise WorkbookError(REASON_UNREADABLE, "file is empty")
    if head[:2] == b"PK":
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile as e:
            raise WorkbookError(REASON_UNREADABLE, f"damaged zip container: {e}")
        if "xl/workbook.bin" in names:
            return "xlsb"
        if "xl/workbook.xml" in names:
            return "xlsx"
        return "unknown"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole2"  # .xls, or a password-protected .xlsx
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if stripped.startswith(b"<"):
        return "xml_or_html"  # Excel 2003 XML or an HTML table saved as .xls
    return "unknown"


# ---------------------------------------------------------------- readers

def _read_xlsx(path: Path) -> list[Sheet]:
    import openpyxl

    # Pass an open file, not the path: openpyxl otherwise rejects any file NAMED .xls, even when
    # the content is a valid .xlsx (old archives contain such misnamed files).
    with open(path, "rb") as fh, warnings.catch_warnings():
        # harmless notices about logos (.wmf images) and print areas: they do not affect cell text
        warnings.filterwarnings("ignore", category=UserWarning, module=r"openpyxl\..*")
        wb = openpyxl.load_workbook(fh, data_only=True, keep_vba=False)
    sheets: list[Sheet] = []
    for i, ws in enumerate(wb.worksheets):
        rows: list[list[Any]] = []
        truncated = False
        count = 0
        for row in ws.iter_rows(values_only=True):
            count += len(row)
            if count > MAX_CELLS_PER_SHEET:
                truncated = True
                break
            rows.append([clean_value(v) for v in row])
        merged = [
            (m.min_row - 1, m.min_col - 1, m.max_row - 1, m.max_col - 1)
            for m in ws.merged_cells.ranges
        ]
        sheets.append(Sheet(ws.title, i, ws.sheet_state != "visible", _trim(rows), merged, truncated))
    return sheets


def _read_xls(path: Path) -> list[Sheet]:
    import xlrd

    try:
        book = xlrd.open_workbook(str(path), formatting_info=True)
    except (NotImplementedError, AssertionError, KeyError):
        book = xlrd.open_workbook(str(path))  # some damaged/old files fail with formatting info
    sheets: list[Sheet] = []
    for i in range(book.nsheets):
        ws = book.sheet_by_index(i)
        rows: list[list[Any]] = []
        truncated = False
        count = 0
        for r in range(ws.nrows):
            count += ws.ncols
            if count > MAX_CELLS_PER_SHEET:
                truncated = True
                break
            row: list[Any] = []
            for c in range(ws.ncols):
                ctype = ws.cell_type(r, c)
                v = ws.cell_value(r, c)
                if ctype == xlrd.XL_CELL_DATE:
                    try:
                        v = xlrd.xldate_as_datetime(v, book.datemode)
                    except Exception:
                        pass
                elif ctype in (xlrd.XL_CELL_ERROR, xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    v = None
                elif ctype == xlrd.XL_CELL_BOOLEAN:
                    v = bool(v)
                row.append(clean_value(v))
            rows.append(row)
        merged = [
            (r1, c1, r2 - 1, c2 - 1)
            for (r1, r2, c1, c2) in getattr(ws, "merged_cells", [])
        ]
        hidden = getattr(ws, "visibility", 0) != 0
        sheets.append(Sheet(ws.name, i, hidden, _trim(rows), merged, truncated))
    return sheets


def _read_xlsb(path: Path) -> list[Sheet]:
    from pyxlsb import open_workbook

    sheets: list[Sheet] = []
    with open_workbook(str(path)) as wb:
        for i, name in enumerate(wb.sheets):
            grid: dict[int, dict[int, Any]] = {}
            max_r = max_c = -1
            count = 0
            truncated = False
            with wb.get_sheet(i + 1) as sh:
                for row in sh.rows(sparse=True):
                    for cell in row:
                        count += 1
                        if count > MAX_CELLS_PER_SHEET:
                            truncated = True
                            break
                        v = clean_value(cell.v)
                        if v is None:
                            continue
                        grid.setdefault(cell.r, {})[cell.c] = v
                        max_r, max_c = max(max_r, cell.r), max(max_c, cell.c)
                    if truncated:
                        break
            rows = [[grid.get(r, {}).get(c) for c in range(max_c + 1)] for r in range(max_r + 1)]
            # .xlsb gives dates as plain numbers (no style info): templates handle Excel serials.
            sheets.append(Sheet(name, i, False, _trim(rows), [], truncated))
    return sheets


def _convert_with_libreoffice(path: Path) -> Path | None:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        for guess in (
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ):
            if Path(guess).exists():
                exe = guess
                break
    if not exe:
        return None
    out = Path(tempfile.mkdtemp(prefix="reportconv_"))
    try:
        subprocess.run(
            [exe, "--headless", "--convert-to", "xlsx", "--outdir", str(out), str(path)],
            check=True, capture_output=True, timeout=180,
        )
    except Exception:
        return None
    produced = list(out.glob("*.xlsx"))
    return produced[0] if produced else None


# ---------------------------------------------------------------- public API

def _classify_error(exc: Exception) -> WorkbookError:
    msg = str(exc)
    low = msg.lower()
    if "encrypt" in low or "password" in low:
        return WorkbookError(REASON_PASSWORD, msg)
    return WorkbookError(REASON_UNREADABLE, f"{type(exc).__name__}: {msg}")


def load_workbook(path: str | Path, allow_libreoffice: bool = True) -> Workbook:
    path = Path(path)
    if not path.is_file():
        raise WorkbookError(REASON_UNREADABLE, f"file not found: {path}")
    fmt = detect_format(path)
    warnings: list[str] = []
    readers = {"xlsx": _read_xlsx, "xls": _read_xls, "xlsb": _read_xlsb, "ole2": _read_xls}

    if path.suffix.lower() not in {".xls", ".xlsx", ".xlsm", ".xlsb"}:
        warnings.append(f"unexpected extension {path.suffix!r}")
    expected = {".xls": {"ole2"}, ".xlsx": {"xlsx"}, ".xlsm": {"xlsx"}, ".xlsb": {"xlsb"}}.get(path.suffix.lower())
    if expected and fmt not in expected and fmt in readers:
        warnings.append(f"extension {path.suffix} does not match content ({fmt}); content was used")

    error: WorkbookError | None = None
    if fmt in readers:
        try:
            sheets = readers[fmt](path)
            return Workbook(path, "xls" if fmt == "ole2" else fmt, sheets, warnings)
        except Exception as exc:  # reader-specific errors vary a lot
            error = _classify_error(exc)
    else:
        error = WorkbookError(REASON_UNSUPPORTED, f"content type {fmt!r} is not a supported Excel format")

    # LibreOffice only for real Excel/HTML/XML content. Plain text and random bytes must NOT be
    # "converted": LibreOffice would import them as a one-column text sheet and hide the damage.
    if not allow_libreoffice or error.reason == REASON_PASSWORD or fmt not in {"ole2", "xlsx", "xlsb", "xml_or_html"}:
        raise error
    converted = _convert_with_libreoffice(path)
    if converted is not None:
        try:
            sheets = _read_xlsx(converted)
            if fmt != "xml_or_html" and len(sheets) == 1 and sheets[0].name == path.stem:
                # signature of LibreOffice's text-import fallback: the file was not a real workbook
                raise WorkbookError(error.reason, f"{error.detail} (LibreOffice could only import it as text)")
            warnings.append(f"read via LibreOffice conversion after error: {error.detail[:120]}")
            return Workbook(path, "xlsx", sheets, warnings)
        except WorkbookError as exc:
            error = exc
        except Exception as exc:
            error = _classify_error(exc)
        finally:
            shutil.rmtree(converted.parent, ignore_errors=True)
    raise error
