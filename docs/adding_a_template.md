# Adding a report template (new layout or new report type)

The parser engine is generic. Each report layout ("era") is one small class in
`parser_engine/templates/`. The engine finds the best template for every file by asking each
one `detect()`, then calls `parse()` on the winner.

## 1. Look at the real file first
```
python -m parser_engine.cli --rows 40 "D:\reports\2013\January\somefile.xls"
```
Shows the real format, every sheet (including hidden ones), merged-cell counts and the first
rows with their row numbers. Compare 3 to 5 files of the same era before writing code.

## 2. Write the template
Create `parser_engine/templates/daily_2013.py`:

```python
from parser_engine.contract import ParsedLine, ParsedMetric, ParsedReport
from parser_engine.helpers import find_cell, label_value, parse_date_ex, read_table, text_of, to_number
from parser_engine.registry import Template, register


@register
class Daily2013(Template):
    name = "daily_2013_v1"        # unique; stored in report_templates.parser_name
    report_type = "12H"           # must exist in report_types
    era = "2013"

    def detect(self, wb):         # return 0.0 to 1.0, never raise
        sheet = wb.get_sheet("Daily Report")
        if sheet is None:
            return 0.0
        return 1.0 if find_cell(sheet, r"OPERATIONS REPORT") else 0.5

    def parse(self, wb):
        sheet = wb.get_sheet("Daily Report")
        rep = ParsedReport(report_type=self.report_type, template_name=self.name)
        d = parse_date_ex(label_value(sheet, "date"), dayfirst=True)   # from the CONTENT
        rep.report_date = d.value
        if d.ambiguous:
            rep.warnings.append("date order assumed day-first")
        rows, _ = read_table(sheet.filled(), {"item": r"^item", "reading": r"^reading"})
        for i, (_, rec) in enumerate(rows, start=1):
            rep.lines.append(ParsedLine(line_no=i, label=text_of(rec["item"]),
                                        numeric_value=to_number(rec["reading"])))
        return rep
```

## 3. Rules
- Report date comes from the report content, never from the folder name.
- Keep what you cannot map: put it in `ParsedLine.extra` (see `_other` from `read_table`).
- `sheet.filled()` copies merged-cell values into every cell of the merged area (good for
  section labels merged over several rows). `sheet.rows` keeps the raw layout.
- Dates in `.xlsb` files arrive as plain numbers; `parse_date_ex` accepts Excel serials.
- Do not try to catch every error. If something is truly unexpected, raise: the file goes to
  `failed_parse_queue` with the reason and the raw cells are still saved and searchable.

## 4. Test it
1. Put an anonymized or synthetic sample of the layout in `tests/samples/`. Never commit real reports.
2. Add a golden test (see `tests/test_parser_engine.py`), run once with `UPDATE_GOLDEN=1`,
   and read the generated JSON in `tests/golden/` carefully before committing it.
3. `python -m pytest -q tests`

## 5. Re-parse after a fix
Saving a file again replaces its raw cells and report (no duplicates), and closes its entry in
`failed_parse_queue`. The Phase 3 importer will do this for you with a `--reparse-failed` option.

The templates in `tests/example_templates.py` are SYNTHETIC and only demonstrate the pattern.
