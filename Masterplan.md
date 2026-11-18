Oil & Gas Report Intelligence: Master Plan v2
Revised plan for the oil-gas-report-intelligence repository. Each phase is self-contained, so it can be given to an AI coding assistant on its own.
1. Requirements
#	Requirement
R1	Import historical Excel reports from 2013 onward from a folder tree (year, then month, about 60 files per month), in any Excel format and any old layout
R2	Import new reports automatically from a dedicated Outlook folder (your mail rules already route them there)
R3	Support 12H (daily), biweekly and monthly reports, and make adding another report type easy
R4	Keyword search across all reports since 2013
R5	Keyword notifications for new reports only, from the day alerts are switched on; never for historical files
R6	Dashboard where you choose the report type, and where every line of a report appears in its own row and cells (a 12-line report shows as 12 rows, not one big cell)
R7	Settings screen: historical folder path, Outlook folder, backup location, intervals (import, backup, keyword check) and the keyword list
R8	Dark and light mode
R9	Later: plug into the company .NET Core dashboard
2. Architecture
Archive folders (2013...) --+
                            +--> Python workers --> Parser engine --> PostgreSQL <-- .NET 8 API <-- Dashboard UI
Outlook folder -------------+                                              |
                                                                  Alert service --> Teams / Email
Area	Choice	Why
Database	PostgreSQL 16 (SQL Server is fine if it is your company standard)	Full-text search, trigram matching and JSONB are built in. About 9,500 12H reports is small data, so no Elasticsearch is needed
Schema owner	EF Core migrations in database/	One source of truth, and the .NET API reuses the same models. Python reads and writes through SQLAlchemy but never creates tables
Workers	Python 3.12, pandas, openpyxl, xlrd, APScheduler	openpyxl for .xlsx/.xlsm, xlrd for .xls, LibreOffice headless conversion as a fallback for odd or damaged files
API	.NET 8 Web API, EF Core, Swagger	Matches your company stack
Dashboard	React + TypeScript (Vite), Tailwind, shadcn/ui, TanStack Table, ECharts	Fast tables and charts, dark mode through CSS variables. Blazor is a valid alternative if your team prefers C#, because the API stays the same
Settings	Stored in an app_settings table and edited in the UI	Workers re-read settings every cycle, so changes apply without restarts
Outlook access	Microsoft Graph (needs an IT app registration) or pywin32 on a PC with desktop Outlook, both behind one interface	Start with whichever IT approves first
Alerts	Teams Workflows webhook and SMTP email	The old Office 365 connector webhooks have been retired
3. Repository structure
oil-gas-report-intelligence/
├── docs/              architecture, sample-file findings, schema, runbook
├── database/          EF Core entities, migrations, seed data, full-text SQL
├── parser_engine/     template registry, era detectors, one parser per template
├── ingestion_engine/  folder importer, Outlook importer, scheduler, hashing
├── alert_service/     keyword matcher, dedupe, Teams and email senders
├── backup_service/    scheduled database dump and raw file copy
├── api_gateway/       .NET 8 Web API
├── dashboard_ui/      React + TypeScript app
├── deploy/            docker-compose, env templates
└── tests/             unit tests plus sample files for each era
4. Data model
Principle: keep the original file, keep a raw copy of the extracted cells, then normalize on top. A parsing bug can then be fixed by re-parsing, without going back to Outlook or the archive.
Table	Purpose
report_types	12H, Biweekly, Monthly and any future type (name, frequency, is_active). Adding a type is a new row, not a schema change
report_templates	One row per layout version of a report type (era, valid from and to, detection rules, parser name)
source_files	Every file ever seen: path or email id, SHA-256 hash (unique), origin (archive or outlook), size, status, received_at, ingested_at
reports	One row per report: type, template, report date, shift, superintendent, train id, source file
report_lines	One row per line of a report: report_id, section, line_no, label, text, numeric value, unit, extra JSONB. This is what lets a 12-line report show as 12 rows. It has a generated tsvector column with a GIN index, plus a trigram index
report_metrics	Numeric readings (gas throughput, vibration, lube oil pressure, downtime hours) by equipment and time, for trend charts
raw_cells	Raw cell grid per sheet, so reports can be re-parsed
failed_parse_queue	Files that failed, with reason, error detail, retry count and resolved flag
keyword_rules	keyword, match type (word, phrase, regex), severity, channel, is_active
alert_events	Rule, report line, matched text, status (new, sent, acknowledged), dedupe key, sent_at
app_settings	Key/value settings from section 6
job_runs	History of imports, alert checks and backups, for the status page
5. Rules for every phase
•	Alerts are forward-only. An alert is created only for reports with origin = outlook and received_at on or after the alerts_active_from setting. Historical import never creates alerts, even if a keyword matches.
•	Idempotent imports. Check the file hash before inserting anything, so a file is never loaded twice.
•	Dates come from the report content. Folder names (year, month) are only hints.
•	Nothing is silently dropped. A file that cannot be parsed goes to failed_parse_queue with a reason.
•	Secrets (webhook URLs, mail credentials, database password) live in environment variables, never in the repo.
•	Report contract. Every parser returns the same structure: header, list of lines, list of metrics, warnings. The UI and API depend only on this.
6. Settings screen
Setting	Default	What it does
Historical archive root folder	empty	Folder the historical importer scans (for example the 2013 folder with its month subfolders)
Historical import controls	buttons	Start, pause, resume, dry run, progress bar, list of failed files
Outlook folder	empty	Mailbox folder the live importer reads
Live import interval	5 minutes	How often Outlook is checked for new reports
Keyword check interval	5 minutes	How often new reports are scanned; also the time of the daily digest
Alerts active from	set when alerts are enabled	Forward-only cutoff
Keyword list	managed on the Keywords page	Keyword, severity, channel, on/off
Backup location	empty	Folder or network share for backups
Backup interval and retention	daily, keep 14	How often to back up and how many copies to keep
Notification targets	empty	Teams webhook and email recipients
Theme	system	Light, dark or follow system
7. Phases
Phase 0: Discovery (before any code)
Goal: know what the Excel files really look like.
•	Write an inventory script that walks the archive and reports files per year, month and extension, sheet names, and a fingerprint of the header rows.
•	Group fingerprints into eras. Pick 3 to 5 sample files per era for the tests folder, removing sensitive data if policy requires.
•	List the fields to extract for each report type (header fields, equipment metrics, remark sections).
•	Confirm with IT: Graph app registration or desktop Outlook, where the server will run, and whether AI tools may see sample data.
Done when: docs/eras.md lists every layout with sample files and a field mapping.
AI prompt: Write a Python CLI that walks a folder tree of .xls, .xlsx and .xlsm files. For each file output year and month (from the path), extension, sheet names and a fingerprint hash of the first 30 non-empty rows (normalized text). Save a CSV, plus a summary of distinct fingerprints with counts and example paths.
Phase 1: Foundation
Goal: repository, database and settings in place.
•	Create the repo structure, Docker Compose for PostgreSQL only, and environment templates.
•	Build EF Core entities and the first migration for all tables in section 4, plus the full-text and trigram indexes.
•	Seed report_types (12H, Biweekly, Monthly) and default app_settings.
•	Add a small shared Python database layer (SQLAlchemy) that maps to the same tables.
Done when: a fresh database can be created with one command, and Python and .NET can both read the tables.
AI prompt: Act as a senior database architect. Using EF Core 8 and PostgreSQL, create entities and a migration for report_types, report_templates, source_files, reports, report_lines (with a generated tsvector column, GIN index and pg_trgm index), report_metrics, raw_cells, failed_parse_queue, keyword_rules, alert_events, app_settings and job_runs. Include seed data.
Phase 2: Parser engine
Goal: turn any report file, from any era, into the standard structure.
•	Template registry: each template has a detector (sheet names, header text, cell positions) and a parser. Adding a new report type or era means adding one parser.
•	Support .xls, .xlsx and .xlsm. Handle merged cells, hidden sheets, several sheets per file and mixed date formats.
•	Output: header, lines (each line with section, label, text, value, unit) and metrics. Cells that cannot be mapped go into the line extra JSONB, so nothing is lost.
•	Validate mandatory fields. On failure, write to failed_parse_queue with a clear reason.
•	Golden tests: for each sample file, the expected output is stored and compared.
Done when: all sample files from Phase 0 parse correctly and unknown files fail cleanly into the queue.
AI prompt: Write a Python module with a template registry. Each template provides detect(workbook) returning a confidence score, and parse(workbook) returning a ParsedReport dataclass (header, lines, metrics, warnings). Support .xls through xlrd and .xlsx through openpyxl. Include a resolver that picks the best template and unit tests using sample files.
Phase 3: Historical importer
Goal: load the 2013 to present archive safely.
•	CLI and background job that walks the archive root recursively (year folder, month subfolder, any depth).
•	SHA-256 hash for each file, skip if already in source_files. Resumable after a crash.
•	Dry-run mode that reports what would be imported and what would fail.
•	Progress and failures recorded in job_runs and failed_parse_queue.
•	Alerts are disabled for this importer. Archive path comes from settings.
Done when: a full month folder (about 60 files) imports twice with zero duplicates, and a damaged file lands in the failed queue.
AI prompt: Write a Python importer that recursively scans a configured root folder for Excel files, computes SHA-256, skips known hashes, parses with the parser registry, saves reports and lines in one transaction per file, records failures, supports dry-run and resume, and never triggers alerts.
Phase 4: Outlook live importer
Goal: new reports arrive in the database automatically.
•	Connector interface with two implementations: Microsoft Graph and pywin32. The Outlook folder name comes from settings.
•	Poll at the configured interval, download Excel attachments, hash, store the raw file, parse and insert. Mark emails as processed so they are not read again.
•	Detect the report type from subject, attachment name or file content.
•	Retry on temporary errors; failed attachments go to the failed queue.
Done when: a test email dropped in the Outlook folder appears in the database within one interval, and sending it twice creates one report.
AI prompt: Write a Python worker with a MailSource interface and two implementations (Microsoft Graph and pywin32). It reads a configured folder, downloads .xls, .xlsx and .xlsm attachments, hashes them, saves them to a raw storage folder, parses them, and stores results idempotently. The interval and folder come from the app_settings table.
Phase 5: Keyword search and alerts
Goal: fast search over everything, and notifications for new reports only.
•	Search: PostgreSQL full-text search plus trigram for typos, with highlighted snippets, filters by report type, date range and equipment.
•	Alert engine: scans only the lines of newly imported Outlook reports (received on or after alerts_active_from). Matching uses word boundaries, phrases and optional regex, with simple negation handling such as no leak detected.
•	Severity: Critical sends immediately, Warning goes into a daily digest, Info is logged only.
•	De-duplication so one issue does not send ten messages. Each alert has a status (new, sent, acknowledged).
•	Senders: Teams Workflows webhook and SMTP email.
•	A preview tool that tests a keyword against history as a read-only search, with no notifications.
Done when: a new test report containing a critical keyword sends one Teams message, and importing the same keyword in a historical file sends nothing.
AI prompt: Write a Python alert service that, after each live import, scans the new report_lines against active keyword_rules using regex word boundaries and negation checks, writes alert_events with a dedupe key, sends Critical alerts to a Teams webhook as JSON, queues Warnings for a daily email digest, and ignores any report older than alerts_active_from.
Phase 6: .NET 8 API
Goal: one clean API for the dashboard now and for your company dashboard later.
•	GET /api/report-types
•	GET /api/reports?type=&from=&to=&page=
•	GET /api/reports/{id} returns the header and all lines
•	GET /api/search?q=&type=&from=&to=&page= returns highlighted snippets
•	GET /api/trends?equipment=&metric=&from=&to=
•	GET/POST/PUT/DELETE /api/keywords
•	GET /api/alerts and POST /api/alerts/{id}/acknowledge
•	GET/PUT /api/settings
•	POST /api/import/historical/start, pause, and GET /api/import/status
•	GET /api/failed-files and POST /api/failed-files/{id}/retry
•	POST /api/backup/run and GET /api/backup/history
•	Swagger documentation, paging everywhere, Windows (Active Directory) authentication with roles (viewer, admin), CORS for the dashboard.
Done when: every endpoint is visible in Swagger and covered by integration tests.
AI prompt: Write a .NET 8 Web API with EF Core for the endpoints listed above, using PostgreSQL full-text search for the search endpoint, DTOs instead of exposing entities, paging, Swagger and Windows authentication with viewer and admin roles.
Phase 7: Dashboard UI
Goal: a modern, fast interface.
•	Report type selector at the top of the Reports page: tabs for 12H (daily), Biweekly, Monthly and any type added later, driven by report_types.
•	Report view: each line of a report is its own table row, with section, label, value, unit and remarks in separate cells. A 12-line report shows 12 rows. Sticky header, virtualized for large reports, export to Excel or CSV.
•	Search page: one search box, filters (type, dates, equipment), highlighted keyword hits, click through to the full report.
•	Trends page: charts for downtime, vibration, throughput and pressures.
•	Alerts page: list with severity, acknowledge button and filters.
•	Keywords page: add, edit, enable or disable keywords and severity.
•	Failed files page: reason, retry button.
•	Settings page: all items from section 6.
•	Dark and light mode: toggle in the header, defaults to the system setting and remembers the choice. Colors come from CSS variables so both modes stay consistent.
•	Responsive layout, keyboard shortcuts for search, accessible contrast.
Done when: you can switch report type, open a report and read each line in its own cell, search 13 years of data in under a second, and flip between dark and light mode.
AI prompt: Build a React + TypeScript dashboard with Tailwind, shadcn/ui and TanStack Table that consumes the API above. Include a report type tab bar, a report detail table with one row per line, a search page with highlighted hits, a settings page, and a dark/light theme toggle using CSS variables.
Phase 8: Settings, scheduling and backup
Goal: everything configurable from the app.
•	Scheduler in the workers reads intervals from settings each cycle.
•	Backup service: scheduled database dump plus copy of the raw file store to the configured location, retention by count, a verification step and a restore guide in docs/.
•	Status panel showing last import, last alert check, last backup and any failures.
•	Setting alerts_active_from is stamped when alerts are first enabled.
Done when: changing an interval or backup folder in the UI takes effect without restarting anything, and a test restore works.
AI prompt: Write a Python backup service that runs on an interval from app_settings, creates a PostgreSQL dump and copies the raw file folder to the configured location, keeps the latest N backups, records each run in job_runs, and fails loudly if the target is not writable.
Phase 9: Packaging, logging and CI/CD
Goal: easy, auditable deployment.
•	Docker Compose with PostgreSQL, Python workers, .NET API and the UI. Volumes for raw files and backups.
•	Structured logging: structlog for Python and Serilog for .NET, to console and rolling files.
•	GitHub Actions: lint and unit tests on pull requests; build images on merge.
•	Security review: least-privilege database users, secrets handling, access roles, retention policy.
•	Integration with the company dashboard: embed the UI, or have their dashboard call the same API.
Done when: a clean server can be brought up from the repo with one command and the CI pipeline is green.
8. How to use this plan with AI assistants
•	Work one phase at a time and give the assistant three things: the phase text, docs/schema.md, and the sample files for the relevant era.
•	Keep the report contract (section 5) and the table list (section 4) unchanged between phases. Change them in the plan first, then in the code.
•	Ask the assistant for tests with every module, and run them before moving on.
9. Suggested start
Begin with Phase 0 and Phase 1 together. Then build one vertical slice: one era of 12H reports parsed, imported and searchable in the UI. After that, widen to the other eras and add Outlook and alerts.
