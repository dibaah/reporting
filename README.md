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

## Phase 5: Outlook live importer (this state)
New reports that arrive in your Outlook folder are imported by themselves. The mailbox is only **read**,
never changed: "already processed" is tracked in the database (file hash and mail id), so the same
mail or file can never be imported twice, and the importer can be stopped and started at any time.

Choose the way to read Outlook in `.env` (`MAIL_SOURCE`) and set the folder with
`python -m alert_service.cli setting set outlook_folder "Inbox/Reports"`:

| MAIL_SOURCE | What it needs | Notes |
|---|---|---|
| `graph` | An app registration made by IT: **application** permission `Mail.Read`, admin consent, ideally limited to this one mailbox (Exchange application access policy). Put `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`, `GRAPH_MAILBOX` in `.env` | Best for a server. The client secret expires: note the date |
| `outlook` | Desktop Outlook installed and signed in, `pip install pywin32` | **Run as a scheduled task "At log on" in your own session, not as a Windows service** (Outlook does not work from services) |
| `eml` | `EML_FOLDER` = a folder of `.eml` files | For testing or when IT exports mail for you |

Run it: `python -m ingestion_engine.live` (keeps running; interval = setting `live_import_interval_minutes`)
or `python -m ingestion_engine.live --once` (for Task Scheduler). Useful settings (`python -m alert_service.cli setting list`):
`outlook_folder`, `live_import_interval_minutes`, `live_import_since` (ignore older mail on the first run).

What it does per attachment: skips non-Excel files, skips content it already knows, keeps the original in
`RAW_STORE_PATH` (outside the repo, `outlook\YYYY\MM\`), parses it, saves it. A file that cannot be parsed
is saved to `failed_parse_queue`; its text is still searched for keywords (Phase 6). Temporary mailbox
problems are retried with back-off; a wrong folder or missing permission is reported clearly in the log.
The report type comes from the matching template (file content), not from the email subject.

**Not yet tried against a real mailbox:** the Microsoft Graph and desktop Outlook readers are tested with
simulated servers and objects only. Try `--once` first and read the log.

## Phase 6: Alerts (this state)
Keyword alerts for **new Outlook reports only**, a daily digest, warnings about unreadable files, and
warnings about reports that did not arrive. Nothing is sent until you switch alerts on.

Quick start:
```
python -m alert_service.cli setting set notification_email "me@company.com"      # who gets mail
#   put SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM in .env (TEAMS_WEBHOOK_URL is optional)
python -m alert_service.cli test-notify                                        # does email work?
python -m alert_service.cli keyword add "leak" --severity critical --channel both
python -m alert_service.cli keyword add "vibration" --severity warning
python -m alert_service.cli preview "leak" --severity warning --from 2024-01-01 # try it on history, sends nothing
python -m alert_service.cli schedule set 12H --times 06:30,18:30 --tz Asia/Tehran   # when reports should arrive
python -m alert_service.cli enable                                             # alerts start NOW
python -m alert_service.service                                                # keep running (or --once)
python -m alert_service.cli alerts list --status new
python -m alert_service.cli ack 12 --note "K-102 inspected, bearing replaced"
```

| Severity | What happens |
|---|---|
| critical | Sent at once, one message per report. Negation is **never** ignored ("no leak detected" still alerts: a false alarm is better than a missed one; the message shows the lines around the hit) |
| warning | Collected into one **daily digest** at `daily_digest_time`. Simple negation ("no leak detected", "leak: none") is ignored |
| info | Recorded only, never sent. Visible with `alerts list --severity info` |

Rules worth knowing
- **Forward-only.** `enable` stamps the start time. Mail received before it, and everything from the historical import, never creates alerts. `disable` pauses; `enable` continues from the same start (mail that arrived meanwhile is checked), `enable --restart` starts from now.
- **Keywords match whole words**, any case. `leak` does not match `leakage`; use a regex such as `leak(s|age|ing)?` (`--match regex`). Tags work: `K-101` does not match `K-1010`.
- A keyword added later only applies to mail that arrives later.
- **Nothing depends on the parser:** if a report could not be parsed, its text cells are scanned instead, and you also get one "could not be parsed" warning per file.
- **Repeats are annotated, not silenced:** the same finding in later reports is still sent, marked "seen 3x before in the last 7 days".
- Delivery that fails (SMTP down) is retried every cycle; an alert counts as delivered when at least one of its channels worked.
- **Missing reports:** after `schedule set`, each expected arrival with no report after `missing_report_grace_minutes` produces one warning. A late report still fills its slot. Biweekly: `schedule set Biweekly --every-days 14 --anchor 2026-01-05 --time 08:00`; monthly: `--monthly-day 1`.
- Settings are checked: `python -m alert_service.cli setting set timezone Asia/Tehran` (Windows needs the `tzdata` package, which is in `requirements.txt`).

Run both workers as scheduled tasks (Task Scheduler: *Create Task*, trigger *At startup*, or *At log on* when using desktop Outlook;
action: `.venv\Scripts\python.exe -m ingestion_engine.live`, start in the repository folder). Tested installers come with Phase 9.

### Tests on a separate database (recommended once real data is loaded)
`psql -U postgres -f database/sql/create_test_database.sql`, then follow the comments in that file. With
`TEST_DATABASE_URL` set, every test uses that database, so real reports, keywords and alerts are never touched.

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
