"""CTEP Shift Report (Day / Night shift, one workbook per shift): "Shift Report (CWRS)" folder.

One template for every layout seen so far (2019): the report is a FORM, so the parser walks it
instead of reading a flat table:

  header   title, date, duty / supervisor, shift crew, internal doc no.
  table    Production area | unit | Status | [Highlight] | Activity Report | [WR Status | Associated WR/WO]
           areas: Wells & MFs, Oil Treatment, Gas Treatment, Utility Systems, GTG & Power Plant,
                  Substations & UPS, ICSS & HMI issue, Storage Tank Level, Shut down
  footer   shift supervisor, CCR members, Site members, Remark

Column positions are read from the header row, so files with or without the Highlight / WR columns
work. Merged cells (area names merged down, activity text merged over several areas) are handled.

Lines (one per row of the form, and one per numbered activity item):
  section  the production area            label  the unit / item          text  status and remarks
  numeric_value / unit  when the row is a reading such as "HC No : 25" or "Injection Press : 128 bar"
  extra    {"row": excel row, ...}  and, for activity items, {"item_no", "covers", "wr_wo"}
Metrics (for trend charts): "name: number unit" readings, tank levels, export rates.

Date and shift: the report's own date is used, but when the document number (era B) or the file name
(CWRS-20190504D / CSR-20190504N) disagrees, that identity wins and a warning is recorded: the
header date of a few files still carries last year's date.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from parser_engine.contract import ParsedLine, ParsedMetric, ParsedReport
from parser_engine.helpers import find_cell, label_value, parse_date_ex, text_of, to_number
from parser_engine.registry import Template, register
from parser_engine.workbook import Sheet, Workbook

SHEET_NAME = re.compile(r"^CTEP\s+(Day|Night)\s+Shift\s+Report\s*$", re.I)
ID_IN_NAME = re.compile(r"-(\d{8})([DN])\b")
NUMBERED = re.compile(r"^\s*(\d+)\s*[-.)]\s*(.*)$")
MEASURE = re.compile(r"^(?P<name>[A-Za-z][^:=]{0,40}?)\s*[:=]\s*(?P<num>[-+]?\d[\d,]*\.?\d*)\s*(?P<unit>[^\d\s].*)?$")
NUMBER_UNIT = re.compile(r"^(?P<num>[-+]?\d[\d,]*\.?\d*)\s*(?P<unit>[A-Za-z%/][A-Za-z%/ ]*)?$")
EXPORT_RATE = re.compile(r"(?P<name>[A-Z]{2}\s+Export\s+Rate)\s*[:=]?\s*(?P<num>\d[\d,]*\.?\d*)\s*(?P<unit>[A-Za-z]+\s*/\s*[A-Za-z]+)", re.I)


def clean(v: Any) -> str:
    return re.sub(r"\s+", " ", text_of(v)).strip()


def _num(s: str) -> float | int:
    f = float(s.replace(",", ""))
    return int(f) if f.is_integer() else f


def measure(cell: str) -> tuple[str, float | int, str | None] | None:
    """'HC  No : 25' -> ('HC No', 25, None);  'S.C: 26.5 bar' -> ('S.C', 26.5, 'bar').
    Times such as 'volume@ 06:00 A.M.' are not readings."""
    m = MEASURE.match(cell)
    if not m or m.group("name").rstrip()[-1].isdigit() or "@" in m.group("name"):
        return None
    return clean(m.group("name")), _num(m.group("num")), (clean(m.group("unit")) or None) if m.group("unit") else None


def value_with_unit(v: Any) -> tuple[float | int, str | None] | None:
    """A cell that is just a number, or a number with a unit ('200 lits')."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v, None
    m = NUMBER_UNIT.match(clean(v))
    return (_num(m.group("num")), clean(m.group("unit")) or None) if m else None


def split_items(text: str) -> list[tuple[int | None, str]]:
    """Activity text -> [(item number or None, item text)]. A line starting '3.' / '3-' / '3)' starts a new item."""
    items: list[list] = []
    for raw in re.split(r"[\r\n]+", text or ""):
        line = raw.strip()
        if not line:
            continue
        m = NUMBERED.match(line)
        if m:
            items.append([int(m.group(1)), m.group(2).strip()])
        elif items:
            items[-1][1] += " " + line
        else:
            items.append([None, line])
    return [(n, re.sub(r"\s+", " ", t).strip()) for n, t in items if t.strip()]


@register
class CtepShiftReport(Template):
    name = "ctep_shift_report"
    report_type = "12H"  # two 12-hour shift reports per day
    era = "2019"

    # ------------------------------------------------------------ detection
    def _header_row(self, sheet: Sheet) -> int | None:
        for r, row in enumerate(sheet.rows[:15]):
            cells = [clean(v).lower() for v in row]
            if cells and cells[0] == "production area" and "status" in cells:
                return r
        return None

    def _sheet(self, wb: Workbook) -> Sheet | None:
        for s in wb.visible_sheets():
            if SHEET_NAME.match(s.name) or self._header_row(s) is not None:
                return s
        return None

    def detect(self, wb: Workbook) -> float:
        s = self._sheet(wb)
        if s is None:
            return 0.0
        score = 0.4 if self._header_row(s) is not None else 0.0
        score += 0.3 if SHEET_NAME.match(s.name) else 0.0
        score += 0.3 if find_cell(s, r"CTEP\s+Shift\s+Report") else 0.0
        return score

    # ------------------------------------------------------------ parsing
    def parse(self, wb: Workbook) -> ParsedReport:
        sheet = self._sheet(wb)
        rep = ParsedReport(report_type=self.report_type, template_name=self.name)
        hdr = self._header_row(sheet)
        if hdr is None:
            raise ValueError("table header row (Production area / unit / Status) not found")
        self._header_block(wb, sheet, rep, hdr)

        head = [clean(v).lower() for v in sheet.rows[hdr]]
        status_col = head.index("status")
        wr_col = next((i for i, h in enumerate(head) if "wr" in h and ("wo" in h or "status" in h)), None)
        activity_col = next((i for i, h in enumerate(head) if h.startswith("activity")), None)
        if activity_col is None:
            # Recent workbooks label this merged narrative column "Section: CTEP Operation".
            activity_col = next((i for i, h in enumerate(head) if h.startswith("section:")), None)
        if activity_col is None:
            # Some newer forms leave that header blank; the narrative column has the most text.
            end = wr_col if wr_col is not None else len(head)
            candidates = [i for i in range(status_col + 1, end) if not head[i]]
            activity_col = max(
                candidates,
                key=lambda c: sum(len(clean(row[c])) for row in sheet.rows[hdr + 1:] if c < len(row)),
                default=None,
            )
        first = hdr + 1
        nxt = [clean(v).lower() for v in sheet.rows[first]] if first < sheet.n_rows else []
        if nxt and not nxt[0] and any(h in ("activity", "wr status") for h in nxt):  # sub-header row
            if wr_col is None and "wr status" in nxt:
                wr_col = nxt.index("wr status")
            first += 1
        if activity_col is None:
            raise ValueError("'Activity Report' column not found in the header row")
        footer = next((r for r in range(first, sheet.n_rows) if clean(sheet.cell(r, 0)).lower().startswith("shift supervisor")), sheet.n_rows)

        W = _Walker(sheet, rep, status_col, activity_col, wr_col)
        W.walk(first, footer)
        self._footer(sheet, rep, footer, W)
        return rep

    # -- header block: title, date, duty, doc no, shift ---------------------------------------
    def _header_block(self, wb: Workbook, sheet: Sheet, rep: ParsedReport, hdr: int) -> None:
        top = Sheet(sheet.name, sheet.index, sheet.hidden, sheet.rows[:hdr], [])  # only rows above the table
        h = rep.header
        date_text = label_value(top, "date")
        doc_no = clean(label_value(top, r"internal\s+doc\.?\s*no\.?")) or None
        duty_cell = find_cell(top, r"^\s*(day|night)(\s+shift)?\s+duty")
        duty_shift = duty_name = None
        if duty_cell:
            duty_shift = duty_cell[2].split()[0].capitalize()
            duty_name = clean(re.sub(r"^\s*(?:day|night)(?:\s+shift)?\s+duty\s*[:\-]?", "", duty_cell[2], flags=re.I)) or None
        h.update({
            "report_title": clean(label_value(top, r"report\s+title")) or "CTEP Shift Report",
            "section": clean(label_value(top, "section")) or None,
            "basis": clean(label_value(top, "basis")) or None,
            "doc_no": doc_no, "duty": duty_name,
            "supervisor": clean(label_value(top, r"supervisor")) or None,
            "crew": clean(label_value(top, r"shift(?!\s*supervisor)")) or None,
        })
        h["supervisor"] = h["supervisor"] or duty_name

        content = parse_date_ex(date_text, dayfirst=True)
        ident_date = ident_shift = None
        for source in (doc_no or "", wb.path.name):  # the document number first, then the file name
            m = ID_IN_NAME.search(source) or re.search(r"(?<!\d)(\d{8})([DN])\b", source)
            if m:
                try:
                    ident_date = dt.datetime.strptime(m.group(1), "%Y%m%d").date()
                except ValueError:
                    continue  # malformed document number; try the file name next
                ident_shift = {"D": "Day", "N": "Night"}[m.group(2)]
                break
        rep.report_date = content.value
        if content.value is None and ident_date:
            rep.report_date = ident_date
            rep.warnings.append("report date could not be read; the date in the document number / file name was used")
        elif ident_date and content.value != ident_date:
            rep.warnings.append(f"date in the report ({content.value}) differs from the document number / file name ({ident_date}); {ident_date} was used")
            rep.report_date = ident_date
        h["date_text"] = clean(date_text) or None

        shifts = {"duty line": duty_shift, "document number / file name": ident_shift}
        known = {k: v for k, v in shifts.items() if v}
        rep.shift = ident_shift or duty_shift
        if rep.shift is None:
            m = SHEET_NAME.match(sheet.name)
            rep.shift = m.group(1).capitalize() if m else None
            if rep.shift:
                rep.warnings.append("shift taken from the sheet name only (sheet names are unreliable)")
        if len(set(known.values())) > 1:
            rep.warnings.append(f"shift differs between sources: {known}; the document number / file name was used")

    # -- footer: supervisor, crew, remark -------------------------------------------------------
    def _footer(self, sheet: Sheet, rep: ParsedReport, footer: int, W: "_Walker") -> None:
        for r in range(footer, sheet.n_rows):
            key = clean(sheet.cell(r, 0)).lower()
            value = " ".join(clean(v) for v in sheet.rows[r][1:] if clean(v))
            if not key or not value:
                continue
            if key.startswith("shift supervisor"):
                rep.header["supervisor"] = value
            elif key in ("ccr members", "site members"):
                rep.header[key.replace(" ", "_")] = value
            elif key == "remark":
                W.line("Remark", "Remark", value, r)
                for m in EXPORT_RATE.finditer(value):
                    rep.metrics.append(ParsedMetric(clean(m.group("name")), _num(m.group("num")), None, re.sub(r"\s+", "", m.group("unit"))))
            else:
                W.line("Other", sheet.cell(r, 0), value, r)


class _Walker:
    """Walks the area rows of the form and appends lines and metrics to the report."""

    def __init__(self, sheet: Sheet, rep: ParsedReport, status_col: int, activity_col: int, wr_col: int | None):
        self.s, self.rep = sheet, rep
        self.status_col, self.activity_col, self.wr_col = status_col, activity_col, wr_col
        self.span = {(r1, c1): (r2, c2) for r1, c1, r2, c2 in sheet.merged}
        self.n = 0
        self.queue: dict[int, list[dict]] = {}          # activity blocks, emitted after the last row they span
        self.activity_lines: list[tuple[ParsedLine, int | None]] = []
        self.pending_wr: list[tuple[int | None, str, str, int]] = []

    # -- output helpers
    def line(self, section: str | None, label: Any, text: Any, row: int, *, value=None, unit=None, extra: dict | None = None) -> ParsedLine:
        self.n += 1
        ln = ParsedLine(self.n, clean(section) or None, clean(label) or None, clean(text) or None, value, unit, {"row": row + 1, **(extra or {})})
        self.rep.lines.append(ln)
        return ln

    def metric(self, name: str, value, equipment: str | None, unit: str | None) -> None:
        self.rep.metrics.append(ParsedMetric(name, value, equipment or None, unit))

    # -- the walk
    def walk(self, first: int, footer: int) -> None:
        rows = self.s.rows
        areas: dict[int, str] = {}
        kinds: dict[int, str] = {}
        area, kind = "", "generic"
        for r in range(first, footer):                       # pass 1: which area does each row belong to?
            a = clean(self.s.cell(r, 0))
            if a:
                area = a
                low = a.lower()
                kind = "storage" if "storage tank" in low else "shutdown" if re.sub(r"\W", "", low) in ("shutdown", "sd") else "gtg" if "gtg" in low else "generic"
            areas[r], kinds[r] = area, kind
        self.areas = areas
        groups: dict[int, str] = {}
        done_text: set[int] = set()
        prev_area = None
        for r in range(first, footer):                       # pass 2: lines
            area, kind = areas[r], kinds[r]
            is_first = clean(self.s.cell(r, 0)) != ""
            if is_first:
                groups = {}
            if any(v is not None for v in rows[r]):
                if kind == "storage":
                    self._storage(r, area, is_first, groups)
                elif kind == "shutdown":
                    self._shutdown(r, area, is_first, groups)
                else:
                    if r not in done_text:
                        self._generic_text(r, area, kind, done_text, footer)
                    self._queue_activity(r, area)
            for job in self.queue.pop(r, []):
                self._emit_activity(job)
        for end in sorted(self.queue):                       # blocks that reach past the last row
            for job in self.queue[end]:
                self._emit_activity(job)
        self._resolve_wr()

    # -- ordinary area rows
    def _generic_text(self, r: int, area: str, kind: str, done_text: set[int], footer: int) -> None:
        unit = clean(self.s.cell(r, 1))
        cells = [clean(self.s.cell(r, c)) for c in range(self.status_col, self.activity_col)]
        cells = [c for c in cells if c]
        if kind == "gtg" and unit.lower().startswith("in service gtg") and r + 1 < footer:
            nxt_unit = clean(self.s.cell(r + 1, 1))
            nxt_other = [c for c in (clean(self.s.cell(r + 1, c)) for c in range(self.status_col, self.activity_col)) if c]
            if nxt_unit:
                self.line(area, "GTG units in service (fuel type)", nxt_unit, r + 1)
            if nxt_other:
                self.line(area, "Available stand-by units", " ; ".join(nxt_other), r + 1)
            done_text.add(r + 1)
            return
        m = measure(unit) if unit else None
        value = unit_of_value = None
        if m:
            value, unit_of_value = m[1], m[2]
            self.metric(m[0], m[1], area, m[2])
        for c in cells:
            mc = measure(c)
            if mc:
                self.metric(mc[0], mc[1], unit if unit and not m else area, mc[2])
        if not m and len(cells) == 1 and unit:
            vu = value_with_unit(cells[0])
            if vu:  # "HP flare stack drain volume@ 06:00 A.M." | 200 lits
                value, unit_of_value = vu
                self.metric(unit, vu[0], area, vu[1])
        if not m and not cells:
            return  # only the fixed form label and nothing reported: no line (labels alone would trigger keywords every day)
        self.line(area, m[0] if m else (unit or None), " ; ".join(cells), r, value=value, unit=unit_of_value)

    # -- activity report: one merged cell, possibly spanning several areas, plus its WR/WO column
    def _queue_activity(self, r: int, area: str) -> None:
        text = text_of(self.s.cell(r, self.activity_col))
        wr_text = text_of(self.s.cell(r, self.wr_col)) if self.wr_col is not None else ""
        if not text.strip() and not wr_text.strip():
            return
        r2, _ = self.span.get((r, self.activity_col), (r, 0))
        covers = list(dict.fromkeys(self.areas[rr] for rr in range(r, r2 + 1) if rr in self.areas and self.areas[rr]))
        self.queue.setdefault(max(r, r2), []).append(
            {"row": r, "area": area, "covers": covers, "items": split_items(text), "wr": split_items(wr_text)})

    def _emit_activity(self, job: dict) -> None:
        wr: dict[int | None, str] = {}
        for n, t in job["wr"]:
            wr[n] = t
        used: set = set()
        for n, t in job["items"]:
            ex: dict[str, Any] = {"item_no": n, "kind": "activity"}
            if len(job["covers"]) > 1:
                ex["covers"] = job["covers"]
            if n is not None and n in wr:
                ex["wr_wo"] = wr[n]
                used.add(n)
            self.activity_lines.append((self.line(job["area"], "Activity", t, job["row"], extra=ex), n))
        for n, t in wr.items():
            if n not in used:
                self.pending_wr.append((n, t, job["area"], job["row"]))

    def _resolve_wr(self) -> None:
        """A WR/WO entry '3. WO Request' that matches no item of its own block is attached to the
        item with that number elsewhere in the report, when exactly one exists; otherwise kept as a line."""
        for n, t, area, row in self.pending_wr:
            hits = [ln for ln, k in self.activity_lines if n is not None and k == n]
            if len(hits) == 1:
                hits[0].extra.update({"wr_wo": t, "wr_wo_matched_by": "item number across the report"})
            else:
                self.line(area, "WR/WO", (f"{n}. " if n else "") + t, row, extra={"item_no": n, "kind": "wr_wo"})

    # -- Storage Tank Level: (name, level) pairs under Oil / Water / Diesel headings
    def _storage(self, r: int, area: str, is_first: bool, groups: dict[int, str]) -> None:
        if is_first:
            groups.update({c: clean(self.s.cell(r, c)) for c in (1, 3, 5) if clean(self.s.cell(r, c))})
            return
        for name_col in (1, 3, 5):
            name, raw = clean(self.s.cell(r, name_col)), self.s.cell(r, name_col + 1)
            if not name and raw is None:
                continue
            vu = value_with_unit(raw) if raw is not None else None
            self.line(area, name, None if vu else raw, r, value=vu[0] if vu else None, unit=vu[1] if vu else None,
                      extra={"group": groups.get(name_col)})
            if vu and name:
                self.metric("tank level", vu[0], name, vu[1])

    # -- Shut down: header row, then one line per filled row
    def _shutdown(self, r: int, area: str, is_first: bool, headers: dict[int, str]) -> None:
        if is_first:
            headers.update({c: clean(v) for c, v in enumerate(self.s.rows[r]) if c > 0 and clean(v)})
            return
        parts, first_value = [], None
        for c, v in enumerate(self.s.rows[r]):
            t = v.strftime("%Y-%m-%d %H:%M") if isinstance(v, dt.datetime) else v.strftime("%H:%M") if isinstance(v, dt.time) else clean(v)
            if c == 0 or not t:
                continue
            head = next((headers[k] for k in sorted(headers, reverse=True) if k <= c), "")
            parts.append(f"{head}: {t}" if head else t)
            first_value = first_value or t
        if parts:
            self.line(area, first_value, " ; ".join(parts), r, extra={"kind": "shutdown"})
