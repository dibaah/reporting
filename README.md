# Oil & Gas Report Intelligence

Internal assistant that imports operations reports (Excel, since 2013 plus new ones from
Outlook), makes them searchable by keyword, and alerts on keywords in NEW reports only.
Runs natively on Windows, no Docker. See the Master Plan v3 for all phases.

## Phase 1: Foundation (this state)
PostgreSQL schema (13 tables), Alembic migration, seed data, shared Python DB layer, tests.

## Phase 2: Parser engine (this state)
`parser_engine/` turns any report workbook into the standard structure (header, lines, metrics,
warnings) and saves it to the database. The engine is generic; **real templates are added after
Phase 0 discovery** (see `docs/adding_a_template.md`). Until then, real files fail cleanly with
`no_template_match` and their raw cells are saved.

- Reads `.xlsx`, `.xlsm`, `.xls`, `.xlsb`. Format is detected from the file content, not the extension.
- LibreOffice (optional, free) is used as a fallback for odd HTML/XML "Excel" files and damaged
  workbooks. Install it on the server if you have old archives; the engine finds `soffice` automatically.
- Handles merged cells, hidden sheets, several sheets per file and mixed date formats.
- Nothing is silently dropped: failures go to `failed_parse_queue` (reasons: `unreadable_file`,
  `password_protected`, `unsupported_format`, `no_template_match`, `parser_error`,
  `validation_failed`, `unknown_report_type`) and raw cells are always stored.

Inspect any file without a database:
`python -m parser_engine.cli --rows 30 path\to\report.xls`

## Phase 3: Historical importer (this state)
Loads the archive (2013 to today) into the database. Safe to stop with Ctrl+C and to run again:
every file is identified by its SHA-256 hash, so finished files are skipped and nothing is
duplicated. One database transaction per file; a bad file never stops the run. This importer
never creates alerts.

Use the virtual environment (`.\.venv\Scripts\python` on Windows). Recommended order:

1. **Look first (Phase 0), no database needed**
   `python -m ingestion_engine.inventory "D:\reports" --out inventory_out`
   Open `inventory_out\layouts.csv` in Excel: one row per layout, with file counts, years and example files.
2. **Write one template per layout** (see `docs/adding_a_template.md`), using
   `python -m parser_engine.cli --rows 40 "<example file>"`.
3. **Rehearse**: `python -m ingestion_engine.historical --root "D:\reports" --dry-run --csv dryrun.csv`
   Parses everything and writes nothing. The CSV shows, per file, what would happen.
4. **Import**: `python -m ingestion_engine.historical --root "D:\reports"`
   Try `--limit 100` first. Progress prints every 50 files; the result is also stored in `job_runs`.
5. **Fix failures**: `python -m ingestion_engine.historical --list-failed` groups them by reason with
   example paths. After adding a template: `python -m ingestion_engine.historical --reparse-failed`.
6. **Build the trigram indexes once the import is complete** (as `report_owner`):
   `psql -U report_owner -d report_intel -f database/sql/post_import_indexes.sql`

Pause, resume or stop a running import from a second window:
`python -m ingestion_engine.historical --set-control pause` (or `run`, `stop`).
The same setting (`historical_import_control` in `app_settings`) will be wired to the Settings screen.

Notes
- The archive folder can also be set once as `historical_archive_root` in `app_settings`; then `--root` is optional.
- Dates come from the report content. If a report's date does not match its folder (year or month),
  it is counted as an informational "folder mismatch"; it is still imported.
- Identical files in several folders are imported once and counted as duplicates.
- A file that cannot be read from disk (locked, permission) is counted as an error and retried on the next run.
- Inventory and dry-run CSV files contain real report text and paths: never commit them.

## Phase 4: Keyword search (this state)
Search over all imported reports, from the command line, from a JSON API and from a preview web page.
Search runs inside PostgreSQL (function `search_report_lines`, migration 0002), so the future
.NET Report plugin gets exactly the same results.

Apply the new migration first: `.\deploy\setup.ps1` (or `alembic upgrade head`).

**Command line**
```
python -m search_engine.cli "vibration K-101"
python -m search_engine.cli "leak" --type 12H --from 2024-01-01 --to 2024-12-31 --context 2
python -m search_engine.cli "compres" --partial          # parts of words and typos
python -m search_engine.cli "\"no leak\" -detector"      # exact phrase, exclusion
```

**Preview web page** (no build step): `python -m search_engine.api` then open http://127.0.0.1:8080
Search page with filters (type, dates, equipment), highlighted hits and a side panel showing the
whole report with every line in its own row; Reports page to browse by type and date; summary
cards; dark/light mode; press `/` to jump to the search box. It has no login and listens on this
computer only. It is a preview of the Report plugin, not the final dashboard
(see `docs/plugin_architecture.md`).

| You type | It finds |
|---|---|
| `K-101` | the tag K-101 (not K-102) |
| `vibration high` | lines with both words |
| `"no leak detected"` | the exact phrase |
| `leak -detector` | leak, but not lines with detector |
| `standby OR overhaul` | either word |
| `101` + **Partial / typos** | any text containing 101, and near spellings (`vibraton`) |

Notes
- Normal search matches whole words and tags. `101` alone does not find `K-101`; tick **Partial / typos** (`--partial`) for that.
- Results are newest first within the same relevance. Blank query + filters browses by date.
- The first search on millions of lines is fast with the default index; Partial mode becomes fast
  after you build the trigram indexes (`database/sql/post_import_indexes.sql`, see Phase 3).
- The tests write to the database in `.env`. Once real data is loaded, point `.env` at a separate
  test database (for example `report_intel_test`) before running `pytest`.

### Setup on Windows
1. Install PostgreSQL 16 (include command line tools) and Python 3.12 (tick "Add to PATH").
2. `.\deploy\create_database.ps1`  creates the database, two roles, and writes `.env`.
3. `.\deploy\setup.ps1`  venv, dependencies, migrations, seed, tests.

Run step 3 again after every update. Never commit `.env`, reports, backups or logs.

### Two database users
- `report_owner` runs migrations only (`MIGRATION_DATABASE_URL`).
- `report_app` is used by all workers (`DATABASE_URL`) and cannot change the schema.

### Useful commands (inside the activated venv)
- `alembic upgrade head` apply migrations
- `python -m database.seed` add missing report types and default settings (never overwrites)
- `python -m database.export_schema` regenerate `docs/schema.md`
- `python -m pytest -q tests`

### After the historical import (Phase 3)
Run `database/sql/post_import_indexes.sql` as `report_owner` to build the trigram indexes.

### Notes
- Full text search uses the `simple` configuration (no stemming) so equipment tags stay intact.
- `report_types.expected_schedule` is empty on purpose until Phase 0 confirms real arrival times.
- The webhook URL and SMTP credentials stay in `.env`, never in `app_settings`.
