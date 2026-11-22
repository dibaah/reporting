"""Generate the SYNTHETIC sample files in tests/samples (no real data).
Run:  python -m tests.make_samples      (needs xlwt and openpyxl)"""
from __future__ import annotations

import datetime as dt
import os
import random
import zipfile
from pathlib import Path

import openpyxl

OUT = Path(__file__).parent / "samples"

LEGACY_ROWS = [  # section, item, reading, unit, remarks
    ("Compressors", "K-101", 4.2, "mm/s", "Vibration normal"),
    ("Compressors", "K-102", 5.1, "mm/s", "Vibration slightly high"),
    ("Compressors", "K-103", 3.9, "mm/s", None),
    ("Compressors", "K-104", "STANDBY", None, "Held in standby"),
    ("Pumps", "P-201", 62.5, "psi", "Lube oil pressure OK"),
    ("Pumps", "P-202", 58, "psi", None),
    ("Pumps", "P-203", 0, "psi", "Out of service for seal replacement"),
    ("Safety", "Permits", 3, None, "3 hot work permits open"),
    ("Safety", "Gas detectors", "OK", None, "No leak detected"),
    ("Safety", "Flare", "OK", None, "Pilot lit"),
    ("Remarks", "Handover", None, None, "Compressor K-102 to be inspected on day shift"),
    ("Remarks", "Downtime", 1.5, "h", "Planned downtime on train B"),
]


def make_legacy(path: Path, date_text: str | None = "05/01/2013") -> None:
    import xlwt  # dev dependency (requirements-dev.txt), only needed to regenerate .xls samples

    wb = xlwt.Workbook()
    ws = wb.add_sheet("Daily Report")
    ws.write(0, 0, "12 HOUR OPERATIONS REPORT")
    if date_text:
        ws.write(1, 0, "Date:")
        ws.write(1, 1, date_text)
    ws.write(2, 0, "Shift:")
    ws.write(2, 1, "Day")
    ws.write(3, 0, "Superintendent:")
    ws.write(3, 1, "A. Example")
    for c, h in enumerate(["Section", "Item", "Reading", "Unit", "Remarks"]):
        ws.write(5, c, h)
    r = 6
    groups: dict[str, list] = {}
    for row in LEGACY_ROWS:
        groups.setdefault(row[0], []).append(row)
    for section, rows in groups.items():
        start = r
        for _, item, reading, unit, remarks in rows:
            ws.write(r, 1, item)
            if reading is not None:
                ws.write(r, 2, reading)
            if unit:
                ws.write(r, 3, unit)
            if remarks:
                ws.write(r, 4, remarks)
            r += 1
        ws.write_merge(start, r - 1, 0, 0, section)  # section merged down its rows
    wb.save(str(path))


MODERN_ROWS = [
    (dt.time(6, 0), "K-201", "Discharge pressure", 41.2, "bar"),
    (dt.time(6, 0), "K-201", "Vibration", 3.1, "mm/s"),
    (dt.time(9, 30), "P-310", "Status", "Running", None),
    (dt.time(12, 0), "Flare", "Observation", None, None),
    (dt.time(15, 0), "T-100", "Level", 78, "%"),
    (dt.time(17, 45), "K-201", "Alarm", "High vibration alarm cleared", None),
]


def make_modern(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "12H Report"
    ws["A1"] = "Daily operations"
    ws["A2"], ws["B2"] = "Report date", dt.datetime(2024, 3, 10)
    ws["A3"], ws["B3"] = "Shift", "Night"
    ws["A4"], ws["B4"] = "Duty superintendent", "B. Example"
    for c, h in enumerate(["Time", "Equipment", "Description", "Value", "Unit"], start=1):
        ws.cell(row=6, column=c, value=h)
    for i, (t, eq, desc, val, unit) in enumerate(MODERN_ROWS, start=7):
        ws.cell(row=i, column=1, value=t)
        ws.cell(row=i, column=2, value=eq)
        ws.cell(row=i, column=3, value=desc)
        ws.cell(row=i, column=4, value=val)
        ws.cell(row=i, column=5, value=unit)
    ws.merge_cells("A7:A8")  # time merged over two readings
    hidden = wb.create_sheet("Lists")
    hidden["A1"] = "internal lookup list"
    hidden.sheet_state = "hidden"
    wb.save(path)


def make_unknown(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Maintenance plan"
    ws.append(["Week", "Task", "Owner"])
    ws.append([1, "Inspect valve V-9", "Crew C"])
    wb.save(path)


def make_exploding(path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active.title = "Boom"
    wb.active["A1"] = "x"
    wb.save(path)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    make_legacy(OUT / "legacy_12h_2013-01-05.xls")
    make_legacy(OUT / "legacy_missing_date.xls", date_text=None)
    make_modern(OUT / "modern_12h_2024-03-10.xlsx")
    (OUT / "misnamed_modern.xls").write_bytes((OUT / "modern_12h_2024-03-10.xlsx").read_bytes())
    make_unknown(OUT / "unknown_layout.xlsx")
    make_exploding(OUT / "exploding_layout.xlsx")
    random.seed(1)
    (OUT / "corrupt.xls").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(random.randrange(256) for _ in range(600)))
    data = (OUT / "modern_12h_2024-03-10.xlsx").read_bytes()
    (OUT / "truncated.xlsx").write_bytes(data[: len(data) // 2])
    print("samples written to", OUT)


if __name__ == "__main__":
    main()
