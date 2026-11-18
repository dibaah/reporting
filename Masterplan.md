Oil & Gas Report Intelligence: Master Plan v3
Revision of Master Plan v2. Same goals, but simpler to build and run: no Docker, a keyword-first order of work, and fixes for the gaps found in review. Each phase is self-contained, so it can be given to an AI coding assistant on its own.
0. What changed from v2
• No Docker. Everything runs natively on one Windows machine or server: PostgreSQL (or SQL Server) installed normally, Python in a virtual environment, the API as a Windows Service, workers as scheduled tasks. Phase 9 is now "Deployment without containers".
• Keyword value comes first. The tool is an assistant for finding keywords and acting on them, so import, search and alerts come before the full dashboard and API.
• Alerts cannot be missed by parse failures. Live reports are keyword-scanned on raw cell text even if the structured parser fails.
• Missing-report detection. The system tells you when an expected 12H report has not arrived.
• One schema owner, no early dependency on .NET. The Python side owns migrations until the API exists.
• Smaller first release. Role-based auth, CI/CD and extra channels are optional and deferred.
• Confidentiality fixes. No real report data in the Git repository; practical rules for AI assistants and sample files.
1. Requirements
#
Requirement
R1
Import historical Excel reports from 2013 onward from a folder tree (year, then month, about 60 files per month), in any Excel format and any old layout
R2
Import new reports automatically from a dedicated Outlook folder (mail rules already route them there)
R3
Support 12H (daily), biweekly and monthly reports, and make adding another type easy
R4
Keyword search across all reports since 2013
R5
Keyword notifications for new reports only, from the day alerts are switched on; never for historical files
R6
Dashboard where you choose the report type and every line of a report appears in its own row and cells
R7
Settings screen: historical folder, Outlook folder, backup location, intervals (import, backup, keyword check), keyword list
R8
Dark and light mode
R9
Later: plug into the company .NET Core dashboard
R10
Runs internally without Docker
R11
Each alert can be acknowledged with a short note about the action taken
2. Architecture
Archive folders (2013...) --+
                            +--> Python workers --> Parser engine --> PostgreSQL <-- .NET 8 API <-- Dashboard UI
Outlook folder -------------+                                              |
                                                                   Alert service --> Email (+ Teams optional)
Area
Choice
Why
Database
PostgreSQL 16 installed natively (SQL Server if that is the company standard)
Full-text search, trigram matching and JSONB built in. About 9,500 12H reports is small data, so no separate search engine
Schema owner
Alembic migrations (Python) until the API exists, then EF Core models scaffolded from the database
Avoids needing the .NET project before the first import works. If you prefer EF Core migrations from day one, that also works, but pick one owner and never both
Workers
Python 3.12, pandas, openpyxl, xlrd, pyxlsb, APScheduler
openpyxl for .xlsx/.xlsm, xlrd for .xls, pyxlsb for .xlsb, LibreOffice headless conversion as a fallback for odd or damaged files
API
.NET 8 Web API, EF Core, Swagger
Matches the company stack. Built after search and alerts work
Dashboard
React + TypeScript (Vite), Tailwind, shadcn/ui, TanStack Table, ECharts. Blazor is a valid alternative
Dark mode through CSS variables
Settings
app_settings table edited in the UI
Workers re-read settings every cycle, so changes apply without restarts
Outlook access
Microsoft Graph (needs an IT app registration) or pywin32 with desktop Outlook, both behind one interface
See the note on pywin32 below
Alerts
Email to yourself first; Teams Workflows webhook optional
Old Office 365 connector webhooks are retired. Email works with no extra approvals
Process hosting
Windows Services (via NSSM or the .NET Windows Service host) and Task Scheduler
Replaces Docker
Important note on pywin32. Outlook COM automation does not work reliably from a Windows Service. If you use pywin32, run the live importer as a scheduled task "when I log on" in your own user session, with desktop Outlook open. Microsoft Graph has no such limitation and is the better choice for an always-on server, if IT approves the app registration.
3. Repository structure
oil-gas-report-intelligence/
├── docs/              architecture, sample-file findings, schema, runbook
├── database/          migrations, seed data, full-text SQL
├── parser_engine/     template registry, era detectors, one parser per template
├── ingestion_engine/  folder importer, Outlook importer, scheduler, hashing
├── alert_service/     keyword matcher, dedupe, senders
├── backup_service/    scheduled database dump and raw file copy
├── api_gateway/       .NET 8 Web API (added later)
├── dashboard_ui/      React + TypeScript app (added later)
├── deploy/            PowerShell install scripts, service definitions, .env template
└── tests/             unit tests plus synthetic sample files for each era
Repository hygiene. Real reports, the raw file store, backups and .env files must never be committed. Add them to .gitignore before the first commit. Use a private repository (or your company's internal Git) and keep only synthetic or fully anonymized sample files in tests/.
4. Data model
Principle: keep the original file, keep a raw copy of the extracted cells, then normalize on top. A parsing bug can be fixed by re-parsing without going back to Outlook or the archive.
Table
Purpose
report_types
12H, Biweekly, Monthly and any future type (name, frequency, expected schedule, is_active). A new type is a new row
report_templates
One row per layout version of a report type (era, valid from and to, detection rules, parser name)
source_files
Every file ever seen: path or email id, SHA-256 hash (unique), origin (archive or outlook), size, status, received_at, ingested_at
reports
One row per report: type, template, report date, shift, source file
report_lines
One row per line: report_id, section, line_no, label, text, numeric value, unit, extra JSONB. A 12-line report is 12 rows. Generated tsvector column with GIN index
report_metrics
Numeric readings by equipment and time, for trend charts
raw_cells
Raw cell grid per sheet, so reports can be re-parsed and so alerts still work when parsing fails
failed_parse_queue
Files that failed: reason, error detail, retry count, resolved flag
keyword_rules
keyword, match type (word, phrase, regex), severity, channel, is_active
alert_events
Rule, report line or raw cell reference, matched text with context, status (new, sent, acknowledged), dedupe key, sent_at, action_note
expected_reports
Expected arrivals per report type and date, with received flag, for missing-report detection
app_settings
Key/value settings from section 6
job_runs
History of imports, alert checks and backups, for the status page
Search note. Equipment tags such as K-101 or P-2204A tokenize badly in standard full-text search. Use the simple text search configuration (no stemming), and rely on a trigram index on report_lines.text for tag and partial-word searches. Trigram indexes are large, so create it after the historical import, not before.
5. Rules for every phase
• Alerts are forward-only. An alert is created only for reports with origin = outlook and received_at on or after alerts_active_from. Historical import never creates alerts.
• Idempotent imports. Check the file hash before inserting, so a file is never loaded twice.
• Dates come from the report content. Folder names are only hints.
• Nothing is silently dropped. A file that cannot be parsed goes to failed_parse_queue with a reason. For live reports, the raw cell text is still scanned for keywords, and the failure itself raises a warning to you.
• Secrets (mail credentials, webhook URLs, database password) live in environment variables or a protected .env file outside the repository.
• Report contract. Every parser returns the same structure: header, list of lines, list of metrics, warnings.
• Data handling. Check with IT before sharing any real report content with an AI assistant. Use synthetic or anonymized samples.
6. Settings screen
Setting
Default
What it does
Historical archive root folder
empty
Folder the historical importer scans
Historical import controls
buttons
Start, pause, resume, dry run, progress bar, failed files list
Outlook folder
empty
Mailbox folder the live importer reads
Live import interval
5 minutes
How often Outlook is checked
Keyword check interval
5 minutes
How often new reports are scanned; also the time of the daily digest
Alerts active from
set when alerts are enabled
Forward-only cutoff
Keyword list
Keywords page
Keyword, severity, channel, on/off
Backup location
empty
Folder or network share
Backup interval and retention
daily, keep 14
How often and how many copies
Notification targets
your email
Email address; Teams webhook optional
Missing-report grace period
2 hours
How late a report can be before you are warned
Theme
system
Light, dark or follow system
7. Phases
The order is changed from v2: first a usable keyword search over history, then live alerts, then the full dashboard.
Phase 0: Discovery (before any code)
Goal: know what the Excel files really look like.
• Inventory script: files per year, month and extension (include .xls, .xlsx, .xlsm, .xlsb), sheet names, and a fingerprint of the header rows.
• Group fingerprints into eras. Pick 3 to 5 sample files per era for tests, anonymized as needed.
• List the fields to extract per report type (header fields, equipment metrics, remark sections).
• Confirm with IT: Graph app registration or desktop Outlook, where the workers will run, whether the machine stays on 24/7, and the rules for AI tools and sample data.
Done when: docs/eras.md lists every layout with sample files and a field mapping.
AI prompt: Write a Python CLI that walks a folder tree of .xls, .xlsx, .xlsm and .xlsb files. For each file output year and month (from the path), extension, sheet names and a fingerprint hash of the first 30 non-empty rows (normalized text). Save a CSV, plus a summary of distinct fingerprints with counts and example paths. Files that cannot be opened must be listed, not skipped.
Phase 1: Foundation (no Docker)
Goal: repository, database and settings in place.
• Create the repo structure, .gitignore and .env.template.
• Install PostgreSQL natively. Create a database and a least-privilege application user.
• Create the first migration for all tables in section 4, with indexes (the trigram index is added after the historical import).
• Seed report_types and default app_settings.
• Shared Python database layer (SQLAlchemy).
• deploy/setup.ps1 that creates the virtual environment, installs dependencies and runs migrations.
Done when: a fresh machine can reach a migrated database with one script, and Python can read and write the tables.
AI prompt: Act as a senior database architect. Using SQLAlchemy and Alembic with PostgreSQL, create models and an initial migration for report_types, report_templates, source_files, reports, report_lines (with a generated tsvector column using the simple configuration and a GIN index), report_metrics, raw_cells, failed_parse_queue, keyword_rules, alert_events, expected_reports, app_settings and job_runs. Include seed data and a PowerShell setup script. No Docker.
Phase 2: Parser engine
Goal: turn any report file, from any era, into the standard structure.
• Template registry: each template has a detector (sheet names, header text, cell positions) and a parser. A new report type or era means one new parser.
• Support .xls, .xlsx, .xlsm and .xlsb. Handle merged cells, hidden sheets, several sheets per file, mixed date formats, and password-protected or damaged files (clean failure).
• Output: header, lines (section, label, text, value, unit) and metrics. Cells that cannot be mapped go into the line's extra JSONB.
• Always store raw_cells, even when structured parsing fails.
• Golden tests: for each sample file, the expected output is stored and compared.
Done when: all sample files parse correctly and unknown files fail cleanly into the queue with raw cells saved.
AI prompt: Write a Python module with a template registry. Each template provides detect(workbook) returning a confidence score, and parse(workbook) returning a ParsedReport dataclass (header, lines, metrics, warnings). Support .xls through xlrd, .xlsx and .xlsm through openpyxl, and .xlsb through pyxlsb. Include a resolver that picks the best template, a raw-cell extractor that works even when no template matches, and unit tests.
Phase 3: Historical importer
Goal: load the 2013 to present archive safely.
• CLI and background job that walks the archive root recursively.
• SHA-256 for each file, skip if already in source_files. Resumable after a crash.
• Dry-run mode listing what would be imported and what would fail.
• Progress and failures recorded in job_runs and failed_parse_queue.
• Alerts disabled for this importer. After the import finishes, create the trigram index and run ANALYZE.
Done when: a full month folder (about 60 files) imports twice with zero duplicates and a damaged file lands in the failed queue.
AI prompt: Write a Python importer that recursively scans a configured root folder for Excel files, computes SHA-256, skips known hashes, parses with the parser registry, saves reports, lines and raw cells in one transaction per file, records failures, supports dry-run and resume, and never triggers alerts.
Phase 4: Keyword search (first usable release)
Goal: find anything across 13 years, immediately.
• Search: full-text plus trigram, with highlighted snippets and filters by report type, date range and equipment.
• Start with a command-line tool or a very small web page (for example FastAPI or Streamlit). The full dashboard comes later.
• Each result shows the date, report type, the matching line and its neighbouring lines for context.
Done when: you can type a keyword or equipment tag and see every historical hit with context in under a second.
AI prompt: Write a Python search tool over the report_lines table using PostgreSQL full-text search (simple configuration) and pg_trgm, with highlighted snippets, filters for report type, date range and equipment, and neighbouring lines as context. Provide a CLI and a minimal Streamlit page.
Phase 5: Outlook live importer
Goal: new reports arrive in the database automatically.
• Connector interface with two implementations: Microsoft Graph and pywin32. Folder name comes from settings.
• Poll at the configured interval, download Excel attachments, hash, store the raw file, parse and insert. Mark emails as processed.
• Detect the report type from subject, attachment name or content.
• Retry on temporary errors. Failed attachments go to the failed queue and raise a warning.
• On first start, record alerts_active_from; emails already in the folder before that time are imported without alerts.
Done when: a test email dropped in the folder appears in the database within one interval, and sending it twice creates one report.
AI prompt: Write a Python worker with a MailSource interface and two implementations (Microsoft Graph and pywin32). It reads a configured folder, downloads .xls, .xlsx, .xlsm and .xlsb attachments, hashes them, saves them to a raw storage folder, parses them, and stores results idempotently. Interval and folder come from the app_settings table. Document how to run it as a scheduled task in the user session for pywin32.
Phase 6: Alerts and missing-report detection
Goal: be told about important keywords in new reports, and about reports that did not arrive.
• Alert engine scans only lines (or raw cells, if parsing failed) of newly imported Outlook reports received on or after alerts_active_from.
• Matching: word boundaries, phrases, optional regex. Every alert shows the matched text with its surrounding lines.
• Negation handling (such as "no leak detected") is a convenience only. For Critical keywords, prefer a false alarm over a missed alert, and let the context line show you the difference.
• Severity: Critical sends immediately, Warning goes into a daily digest, Info is logged only.
• De-duplication so one issue does not send many messages. Status: new, sent, acknowledged, with an optional action note.
• Senders: email first, Teams Workflows webhook optional.
• Missing-report check: compare expected_reports against received reports and warn when a report is later than the grace period.
• Preview tool that tests a keyword against history as a read-only search, without notifications.
Done when: a new test report with a critical keyword sends one message, the same keyword in a historical file sends nothing, and a skipped 12H report triggers a missing-report warning.
AI prompt: Write a Python alert service that, after each live import, scans the new report_lines (or raw_cells when parsing failed) against active keyword_rules using regex word boundaries, writes alert_events with a dedupe key and context lines, sends Critical alerts by email immediately, queues Warnings for a daily digest, ignores any report received before alerts_active_from, and checks expected_reports for missing reports.
Phase 7: .NET 8 API
Goal: one clean API for the dashboard now and the company dashboard later.
• GET /api/report-types
• GET /api/reports?type=&from=&to=&page=
• GET /api/reports/{id} returns the header and all lines
• GET /api/search?q=&type=&from=&to=&page= returns highlighted snippets
• GET /api/trends?equipment=&metric=&from=&to=
• GET/POST/PUT/DELETE /api/keywords
• GET /api/alerts, POST /api/alerts/{id}/acknowledge (with note)
• GET/PUT /api/settings
• POST /api/import/historical/start, pause, GET /api/import/status
• GET /api/failed-files, POST /api/failed-files/{id}/retry
• POST /api/backup/run, GET /api/backup/history
• Swagger, paging everywhere, Windows (Active Directory) authentication, CORS for the dashboard.
• Roles (viewer, admin) only when more than one person uses it. For a single user, one admin role is enough.
• Runs as a Windows Service, listening on an internal address only.
Done when: every endpoint appears in Swagger and has integration tests.
AI prompt: Write a .NET 8 Web API with EF Core (database-first, scaffolded from the existing PostgreSQL schema) for the endpoints listed above, using PostgreSQL full-text search for search, DTOs instead of entities, paging, Swagger and Windows authentication. Configure it to run as a Windows Service.
Phase 8: Dashboard UI
Goal: a modern, fast interface.
• Report type selector at the top of the Reports page, driven by report_types.
• Report view: each line is its own table row, with section, label, value, unit and remarks in separate cells. Sticky header, virtualization for large reports, export to Excel or CSV.
• Search page: one box, filters, highlighted hits, click through to the full report.
• Alerts page: severity, acknowledge button with note, filters.
• Keywords page: add, edit, enable or disable, severity.
• Failed files and Status pages.
• Trends page: charts for downtime, vibration, throughput and pressures (can be a later increment).
• Settings page with all items from section 6.
• Dark and light mode: header toggle, defaults to system, remembers the choice, colors from CSS variables.
Done when: you can switch report type, open a report and read each line in its own cell, search 13 years of data in under a second, and flip between dark and light mode.
AI prompt: Build a React + TypeScript dashboard with Tailwind, shadcn/ui and TanStack Table that consumes the API above. Include a report type tab bar, a report detail table with one row per line, a search page with highlighted hits, an alerts page with acknowledge and note, a settings page, and a dark/light theme toggle using CSS variables.
Phase 9: Backup, scheduling and deployment without containers
Goal: everything configurable from the app and easy to run.
• Scheduler in the workers reads intervals from settings each cycle.
• Backup service: scheduled pg_dump plus copy of the raw file store to the configured location, retention by count, a verification step, and a restore guide in docs/. Fail loudly if the target is not writable.
• Status panel: last import, last alert check, last backup, failures.
• Windows deployment: PowerShell scripts that install the workers as services (NSSM) or scheduled tasks, install the API as a Windows Service, and start everything after a reboot. Include an uninstall script.
• Structured logging: structlog for Python, Serilog for .NET, to rolling files.
• Basic hardening: least-privilege database user, API bound to the internal network, secrets outside the repository.
• Integration with the company dashboard: embed the UI or call the same API.
• CI/CD is optional. If the company allows it, add lint and unit tests on pull requests. Deployment stays a script run by you.
Done when: after a reboot everything starts by itself, changing an interval or backup folder in the UI takes effect without restarting anything, and a test restore works.
AI prompt: Write a Python backup service that runs on an interval from app_settings, creates a PostgreSQL dump and copies the raw file folder to the configured location, keeps the latest N backups, records each run in job_runs, and fails loudly if the target is not writable. Also write PowerShell scripts that install the Python workers and the .NET API as Windows services, with uninstall.
8. How to use this plan with AI assistants
• Work one phase at a time and give the assistant three things: the phase text, docs/schema.md, and synthetic or anonymized sample files for the relevant era.
• Keep the report contract (section 5) and the table list (section 4) unchanged between phases. Change them in the plan first, then in the code.
• Ask for tests with every module and run them before moving on.
9. Suggested start
Begin with Phases 0 and 1. Then build one vertical slice: one era of 12H reports parsed, imported and searchable (Phases 2 to 4). After that, add Outlook import and alerts (Phases 5 and 6). The API and dashboard come last, once the assistant is already useful to you day to day.
