-- Run AFTER the historical import (Phase 3) finishes, as report_owner.
-- Trigram index for equipment tags and partial words. It is large, so it is built last.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_report_lines_text_trgm
    ON report_lines USING gin (text gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_report_lines_label_trgm
    ON report_lines USING gin (label gin_trgm_ops);
ANALYZE report_lines;
