-- OPTIONAL but recommended once real reports are loaded: a separate database for the automated tests,
-- so tests can never touch real reports, keywords or alert state.
--
--   psql -U postgres -f database/sql/create_test_database.sql
--
-- Then, in PowerShell from the repository folder (use your own passwords):
--   $env:MIGRATION_DATABASE_URL = "postgresql+psycopg://report_owner:OWNERPW@localhost:5432/report_intel_test"
--   .\.venv\Scripts\python -m alembic upgrade head
--   $env:TEST_DATABASE_URL = "postgresql+psycopg://report_app:APPPW@localhost:5432/report_intel_test"
--   .\.venv\Scripts\python -m pytest -q tests
-- (the test database holds only test data; the roles report_owner and report_app already exist)

CREATE DATABASE report_intel_test OWNER report_owner ENCODING 'UTF8';
REVOKE ALL ON DATABASE report_intel_test FROM PUBLIC;
GRANT CONNECT ON DATABASE report_intel_test TO report_app;

\connect report_intel_test

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE SCHEMA report AUTHORIZATION report_owner;
GRANT USAGE ON SCHEMA report TO report_app;
ALTER DEFAULT PRIVILEGES FOR ROLE report_owner IN SCHEMA report
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO report_app;
ALTER DEFAULT PRIVILEGES FOR ROLE report_owner IN SCHEMA report
    GRANT USAGE, SELECT ON SEQUENCES TO report_app;
