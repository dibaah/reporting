"""search function shared by every client (Python today, the .NET Report plugin later)

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SEARCH_FUNCTION = r"""
CREATE OR REPLACE FUNCTION search_report_lines(
    p_query       text    DEFAULT NULL,
    p_mode        text    DEFAULT 'fulltext',   -- 'fulltext' | 'partial'
    p_report_type text    DEFAULT NULL,
    p_date_from   date    DEFAULT NULL,
    p_date_to     date    DEFAULT NULL,
    p_equipment   text    DEFAULT NULL,
    p_limit       integer DEFAULT 50,
    p_offset      integer DEFAULT 0
)
RETURNS TABLE (
    line_id       bigint,
    report_id     integer,
    report_date   date,
    report_type   text,
    shift         text,
    line_no       integer,
    section       text,
    label         text,
    text          text,
    numeric_value numeric,
    unit          text,
    rank          real
)
LANGUAGE sql STABLE
AS $$
WITH q AS (
    SELECT NULLIF(btrim(p_query), '') AS raw,
           websearch_to_tsquery('simple', coalesce(p_query, '')) AS tsq,
           '%' || replace(replace(replace(btrim(coalesce(p_query, '')), '\', '\\'), '%', '\%'), '_', '\_') || '%' AS pat,
           CASE WHEN NULLIF(btrim(p_equipment), '') IS NULL THEN NULL
                ELSE '%' || replace(replace(replace(btrim(p_equipment), '\', '\\'), '%', '\%'), '_', '\_') || '%'
           END AS eq_pat
)
SELECT l.id, r.id, r.report_date, t.name::text, r.shift::text, l.line_no,
       l.section, l.label, l.text, l.numeric_value, l.unit::text,
       CASE WHEN q.raw IS NULL THEN 0::real
            WHEN p_mode = 'partial' THEN greatest(word_similarity(q.raw, coalesce(l.text, '')),
                                                  word_similarity(q.raw, coalesce(l.label, '')))::real
            ELSE ts_rank(l.search_vector, q.tsq)
       END AS rank
FROM q, report_lines l
JOIN reports r      ON r.id = l.report_id
JOIN report_types t ON t.id = r.report_type_id
WHERE (p_report_type IS NULL OR t.name = p_report_type)
  AND (p_date_from IS NULL OR r.report_date >= p_date_from)
  AND (p_date_to   IS NULL OR r.report_date <= p_date_to)
  AND (q.eq_pat IS NULL OR l.label ILIKE q.eq_pat OR l.text ILIKE q.eq_pat)
  AND (q.raw IS NULL
       OR (p_mode <> 'partial' AND l.search_vector @@ q.tsq)
       OR (p_mode = 'partial' AND (l.text ILIKE q.pat OR l.label ILIKE q.pat OR l.section ILIKE q.pat
                                   OR q.raw <% l.text OR q.raw <% l.label)))
ORDER BY rank DESC, r.report_date DESC, r.id DESC, l.line_no
LIMIT least(greatest(p_limit, 1), 500) OFFSET greatest(p_offset, 0)
$$;
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(SEARCH_FUNCTION)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS search_report_lines(text, text, text, date, date, text, integer, integer)"
    )
