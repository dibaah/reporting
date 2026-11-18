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
