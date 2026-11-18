-- Run ONCE as the PostgreSQL superuser (postgres), for example:
--   psql -U postgres -v owner_pw="..." -v app_pw="..." -f database/sql/create_database.sql
-- Creates two roles: report_owner (runs migrations) and report_app (runtime, no schema rights).

CREATE ROLE report_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE report_app   LOGIN PASSWORD :'app_pw';

CREATE DATABASE report_intel OWNER report_owner ENCODING 'UTF8';
REVOKE ALL ON DATABASE report_intel FROM PUBLIC;
GRANT CONNECT ON DATABASE report_intel TO report_app;

\connect report_intel

-- pg_trgm ships with PostgreSQL (contrib). Created here because it needs superuser.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

GRANT USAGE ON SCHEMA public TO report_app;
-- Tables created later by report_owner automatically become usable by report_app.
ALTER DEFAULT PRIVILEGES FOR ROLE report_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO report_app;
ALTER DEFAULT PRIVILEGES FOR ROLE report_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO report_app;
