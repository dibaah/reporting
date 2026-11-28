# Database schema (generated)

All Report Intelligence tables live in the PostgreSQL `report` schema.

Generated from `database/models.py`. Regenerate with `python -m database.export_schema`.


## app_settings

| Column | Type | Notes |
|---|---|---|
| key | VARCHAR(100) | PK |
| value | TEXT | required |
| description | TEXT |  |
| updated_at | DATETIME | required |

## job_runs

| Column | Type | Notes |
|---|---|---|
| id | BIGINT | PK |
| job_type | VARCHAR(30) | required |
| status | VARCHAR(10) | required |
| started_at | DATETIME | required |
| finished_at | DATETIME |  |
| details | JSONB |  |
| error | TEXT |  |

## keyword_rules

| Column | Type | Notes |
|---|---|---|
| id | INTEGER | PK |
| keyword | TEXT | required |
| match_type | VARCHAR(10) | required |
| severity | VARCHAR(10) | required |
| channel | VARCHAR(10) | required |
| is_active | BOOLEAN | required |
| created_at | DATETIME | required |

## report_types

| Column | Type | Notes |
|---|---|---|
| id | INTEGER | PK |
| name | VARCHAR(50) | unique, required |
| frequency | VARCHAR(30) | required |
| expected_schedule | JSONB |  |
| is_active | BOOLEAN | required |

## source_files

| Column | Type | Notes |
|---|---|---|
| id | INTEGER | PK |
| origin | VARCHAR(10) | required |
| path | TEXT |  |
| email_id | TEXT |  |
| file_name | TEXT | required |
| sha256 | VARCHAR(64) | unique, required |
| size_bytes | BIGINT |  |
| status | VARCHAR(10) | required |
| received_at | DATETIME |  |
| ingested_at | DATETIME | required |
| alert_scanned_at | DATETIME |  |

## failed_parse_queue

| Column | Type | Notes |
|---|---|---|
| id | INTEGER | PK |
| source_file_id | INTEGER | FK -> source_files.id, required |
| reason | TEXT | required |
| error_detail | TEXT |  |
| retry_count | INTEGER | required |
| resolved | BOOLEAN | required |
| created_at | DATETIME | required |
| last_attempt_at | DATETIME |  |
| notified_at | DATETIME |  |

## raw_cells

| Column | Type | Notes |
|---|---|---|
| id | BIGINT | PK |
| source_file_id | INTEGER | FK -> source_files.id, required |
| sheet_name | TEXT | required |
| sheet_index | INTEGER | required |
| is_hidden | BOOLEAN | required |
| grid | JSONB | required |
| text_dump | TEXT |  |

## report_templates

| Column | Type | Notes |
|---|---|---|
| id | INTEGER | PK |
| report_type_id | INTEGER | FK -> report_types.id, required |
| name | VARCHAR(100) | required |
| era | VARCHAR(50) |  |
| valid_from | DATE |  |
| valid_to | DATE |  |
| detection_rules | JSONB |  |
| parser_name | VARCHAR(100) | required |
| is_active | BOOLEAN | required |

## reports

| Column | Type | Notes |
|---|---|---|
| id | INTEGER | PK |
| report_type_id | INTEGER | FK -> report_types.id, required |
| template_id | INTEGER | FK -> report_templates.id |
| source_file_id | INTEGER | FK -> source_files.id, required |
| report_date | DATE | required |
| shift | VARCHAR(20) |  |
| header | JSONB |  |
| created_at | DATETIME | required |

## expected_reports

| Column | Type | Notes |
|---|---|---|
| id | INTEGER | PK |
| report_type_id | INTEGER | FK -> report_types.id, required |
| expected_at | DATETIME | required |
| received_report_id | INTEGER | FK -> reports.id |
| warned_at | DATETIME |  |

## report_lines

| Column | Type | Notes |
|---|---|---|
| id | BIGINT | PK |
| report_id | INTEGER | FK -> reports.id, required |
| section | TEXT |  |
| line_no | INTEGER | required |
| label | TEXT |  |
| text | TEXT |  |
| numeric_value | NUMERIC |  |
| unit | VARCHAR(50) |  |
| extra | JSONB |  |
| search_vector | TSVECTOR | required, generated |

## report_metrics

| Column | Type | Notes |
|---|---|---|
| id | BIGINT | PK |
| report_id | INTEGER | FK -> reports.id, required |
| equipment | VARCHAR(100) |  |
| metric | VARCHAR(100) | required |
| value | NUMERIC |  |
| unit | VARCHAR(50) |  |
| measured_at | DATETIME |  |

## alert_events

| Column | Type | Notes |
|---|---|---|
| id | BIGINT | PK |
| rule_id | INTEGER | FK -> keyword_rules.id, required |
| report_id | INTEGER | FK -> reports.id |
| report_line_id | BIGINT | FK -> report_lines.id |
| source_file_id | INTEGER | FK -> source_files.id |
| raw_ref | JSONB |  |
| matched_text | TEXT | required |
| context | TEXT |  |
| status | VARCHAR(15) | required |
| dedupe_key | VARCHAR(200) | unique, required |
| created_at | DATETIME | required |
| sent_at | DATETIME |  |
| acknowledged_at | DATETIME |  |
| action_note | TEXT |  |
| repeat_key | VARCHAR(64) |  |
| repeat_count | INTEGER | required |
