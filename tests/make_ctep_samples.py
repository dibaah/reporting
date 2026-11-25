"""Generate SYNTHETIC CTEP shift reports (invented names, dates in 2099, invented readings) that have
the same structure as the real forms: area names merged down column A, activity text merged over
several areas, WR/WO columns, storage-tank pairs, shut-down block, footer.

   python -m tests.make_ctep_samples

Real reports must never be committed (the repository is public): the template is tested on these files,
and, when present, on real files placed in tests/real_samples/ (git-ignored)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import openpyxl

OUT = Path(__file__).parent / "samples"

AREAS = [  # area, [(unit, status, highlight)], fixed
    ("Wells & MFs", [("Wells in service", "Highlights", None), ("HC  No : 25", "Gas Injection is on going for 21 Test Wells", None),
                     ("LC   No : 22", None, None), ("WD : 2", "Injection Press :  128 bar", "Inj Flow rate:  22.5  ton /h")]),
    ("Oil Treatment", [("LC A", "In Service", "S.C: \n26.5 bar"), ("LC B", "In Service", None), ("HC", "In Service", "S.C: \n8.45 bar")]),
    ("Gas Treatment", [(None, "In Service", None), (None, None, None), (None, None, None)]),
    ("Utility Systems", [("HP flare stack drain volume@ 06:00 A.M.", None, "200 lits"), ("LP flare stack drain volume@ 06:00 A.M.", None, 0),
                         ("HP flare stack drain volume@ 18:00 P.M.", None, None), ("LP flare stack drain volume@ 18:00 P.M.", None, None),
                         (None, "In Service", None)]),
    ("GTG & Power Plant", [("In Service GTG units and fuel type", None, "Available Stand by unit"), ("Units  1 , 2 , 3  with FG", None, "Unit  5"), (None, None, None)]),
    ("Substations & UPS", [(None, None, None)]),
    ("ICSS & HMI issue", [(None, None, None)]),
]
TANKS = [("LC TK 501", 26.9, "F W 901A", 98.23, "TK 901-61", 83.2), ("LC TK 511", 26, "F W 901B", 98.61, None, None),
         ("HC TK502", 19.3, "S W 701A", 84, None, None), ("OFF Spec", 0, "S W 701B", 84.1, None, None),
         (None, None, "P W 401A", 54.1, None, None), (None, None, "P W 401B", 42.9, None, None)]

GAS_ITEMS = ("1. UCP problem of C-0401B-12 was fixed .\n2. Blinding of C-0102B-31 is ongoing .\n"
             "3. Suction and discharge of light fast loop pump have been closed , due to leakage of PRV .\n"
             "4. Chemical refill   :   Hypochlorite  :  400 liter / Filter Aid  :  200 liter .\n5. CMS charging of PSA (A) was done .\n"
             "6. Deep test of N2 package was done . \n7. Electrical and mechanical PMR of P-0701A-50 was done .\n"
             "8. Hydrant No.88 in work shop area is damage , valve is isolated .")


def build(path: Path, era: str, *, highlight: bool = True, wr: str = "none", date_text: str, duty: str | None = None,
          doc_no: str | None = None, shutdown: tuple | None = None, activity: dict | None = None) -> Path:
    """era 'A' (simple title) or 'B' (formal header). wr: none | sub (WR Status sub-column) | assoc (Associated WR/WO)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CTEP Day Shift Report"
    act_col = 6                                      # F
    wr_col = {"none": None, "sub": 8, "assoc": 9 if era == "B" else 8}[wr]
    act_span = 3 if era == "B" else 2
    wr_span = 2 if era == "B" else 1

    def put(r, c, v, merge_to=None):
        ws.cell(row=r, column=c, value=v)
        if merge_to:
            ws.merge_cells(start_row=r, start_column=c, end_row=merge_to[0], end_column=merge_to[1])

    if era == "B":
        put(1, 2, "EXAMPLE OIL FIELD PRODUCTION PROJECT", (1, 9))
        put(2, 2, "Report Title: CTEP Shift Report", (2, 5)); put(2, 6, "Section: CTEP Operation", (2, 7)); put(2, 8, "Basis: Shift", (2, 9))
        put(3, 1, "Supervisor : T.Supervisor", (4, 2)); put(3, 3, "Shift: A", (4, 5)); put(3, 6, "Internal Doc. No.:", (4, 6))
        put(3, 7, doc_no, (4, 8)); put(3, 9, f"Date: {date_text}", (4, 10))
        hdr = 5
    else:
        put(1, 2, "CTEP Shift Report", (1, 7))
        put(2, 2, f"Date: {date_text}"); put(2, 7, duty)
        hdr = 3
    head = {1: "Production area", 2: "unit", 3: "Status"}
    if highlight:
        head[4] = "Highlight"
    for c, t in head.items():
        put(hdr, c, t)
    put(hdr, act_col, "Activity Report", (hdr, act_col + act_span - 1) if era == "B" or wr == "sub" else None)
    if wr == "assoc":
        put(hdr, wr_col, "Associated WR/WO")
    first = hdr + 1
    if wr == "sub":
        put(hdr + 1, act_col, "Activity", (hdr + 1, act_col + 1)); put(hdr + 1, wr_col, "WR Status")
        first += 1

    # activity blocks: (first area index, last area index) -> (text, wr text)
    default = {(0, 1): ("S17 : Fully chokeup @16:30", "3. WO Request\n8. WO Request" if wr == "assoc" and era == "B" else None),
               (2, 3): (GAS_ITEMS, None), (4, 5): ("GTG 4 is out of service.", None)}
    blocks = activity if activity is not None else default
    r = first
    start_of: list[int] = []
    for name, rows in AREAS:
        start = r
        start_of.append(start)
        for i, (unit, status, hl) in enumerate(rows):
            put(r, 2, unit); put(r, 3, status)
            if highlight:
                put(r, 4, hl)
            elif hl is not None:
                put(r, 4, hl)
            r += 1
        put(start, 1, name, (r - 1, 1) if r - 1 > start else None)
    end_of = [s2 - 1 for s2 in start_of[1:]] + [r - 1]
    for (a, b), (text, wr_text) in blocks.items():
        top, bottom = start_of[a], end_of[b]
        put(top, act_col, text, (bottom, act_col + act_span - 1))
        if wr_col and wr_text:
            put(top, wr_col, wr_text, (bottom, wr_col + wr_span - 1) if era == "B" else (bottom, wr_col))
    # storage tank level
    put(r, 1, "Storage Tank Level", (r + len(TANKS), 1))
    put(r, 2, "Oil Tanks"); put(r, 4, "Water Tanks"); put(r, 6, "Diesel Storage")
    r += 1
    for t in TANKS:
        for c, v in zip((2, 3, 4, 5, 6, 7), t):
            put(r, c, v)
        r += 1
    # shut down block
    put(r, 1, "Shut down", (r + 1, 1))
    for c, t in zip((2, 3, 4, 6, 7), ("S/D level", "Imposed system", "Root cause", "Initiated time", "Recovery Time")):
        put(r, c, t)
    r += 1
    if shutdown:
        for c, v in zip((2, 3, 4, 6, 7), shutdown):
            put(r, c, v)
    r += 1
    for key, value in (("shift supervisor", "T.Supervisor" if era == "B" or doc_no else None), ("CCR members", "A.One / B.Two / C.Three"),
                       ("Site members", "D.Four / E.Five"), ("Remark", "LC Export Rate 70,000 bbl/day & HC Export Rate 30,000 bbl/day.")):
        put(r, 1, key); put(r, 2, value, (r, 8))
        r += 1
    wb.save(path)
    return path


def main() -> None:
    OUT.mkdir(exist_ok=True)
    build(OUT / "CWRS-20990105D.xlsx", "B", wr="assoc", date_text="2099-Jan-05", doc_no="CWRS-20990105D")
    build(OUT / "CSR-20990106N.xlsx", "A", highlight=False, wr="sub", date_text="06.Jan.2099", duty="Night shift Duty:\nA. Tester",
          activity={(0, 1): ("1 - note about wells", "1 - 11503"), (2, 3): ("1. Seal pot charged.\n2. Calibration done.", "2 - 11504"), (4, 5): ("GTG 4 is out of service.", None)})
    build(OUT / "CSR-20990107D.xlsx", "A", wr="assoc", date_text="07.Jan.2098", duty="Day Duty:\nB. Tester")      # year typo in the header
    build(OUT / "CSR-20990108D.xlsx", "A", date_text="08.Jan.2099", duty="Day Duty:\nC. Tester",
          shutdown=("Unit 2 trip", "Gas compressor K-01", "High vibration", dt.time(3, 15), dt.time(4, 40)))
    print("synthetic CTEP samples written to", OUT)


if __name__ == "__main__":
    main()
