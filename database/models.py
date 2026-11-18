"""Database models for Oil & Gas Report Intelligence.

Alembic migrations (database/migrations) own the schema. These models are used
by the Python workers to read and write data; never call create_all() in
production code.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class ReportType(Base):
    """12H, Biweekly, Monthly and any future type. A new type is a new row."""

    __tablename__ = "report_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    frequency: Mapped[str] = mapped_column(String(30))  # 12h | biweekly | monthly | custom
    # Optional description of when a report is expected, used for missing-report
    # detection (Phase 6). NULL = no check yet.
    expected_schedule: Mapped[dict | None] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")

    templates: Mapped[list["ReportTemplate"]] = relationship(back_populates="report_type")


class ReportTemplate(Base):
    """One row per layout version (era) of a report type."""

    __tablename__ = "report_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_type_id: Mapped[int] = mapped_column(ForeignKey("report_types.id"))
    name: Mapped[str] = mapped_column(String(100))
    era: Mapped[str | None] = mapped_column(String(50))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    detection_rules: Mapped[dict | None] = mapped_column(JSONB)
    parser_name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")

    report_type: Mapped[ReportType] = relationship(back_populates="templates")

    __table_args__ = (UniqueConstraint("report_type_id", "name"),)


class SourceFile(Base):
    """Every file ever seen. The SHA-256 hash makes imports idempotent."""

    __tablename__ = "source_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    origin: Mapped[str] = mapped_column(String(10))  # archive | outlook
    path: Mapped[str | None] = mapped_column(Text)  # archive path or stored raw path
    email_id: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(10), server_default="pending")
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = _ts()

    __table_args__ = (
        CheckConstraint("origin IN ('archive','outlook')", name="ck_source_files_origin"),
        CheckConstraint(
            "status IN ('pending','parsed','failed','skipped')", name="ck_source_files_status"
        ),
        Index("ix_source_files_origin_received", "origin", "received_at"),
    )


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_type_id: Mapped[int] = mapped_column(ForeignKey("report_types.id"))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("report_templates.id"))
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id"))
    report_date: Mapped[date] = mapped_column(Date)
    shift: Mapped[str | None] = mapped_column(String(20))
    header: Mapped[dict | None] = mapped_column(JSONB)  # header fields from the report
    created_at: Mapped[datetime] = _ts()

    lines: Mapped[list["ReportLine"]] = relationship(
        back_populates="report", cascade="all, delete-orphan", order_by="ReportLine.line_no"
    )

    __table_args__ = (
        Index("ix_reports_type_date", "report_type_id", "report_date"),
        Index("ix_reports_source_file", "source_file_id"),
    )


class ReportLine(Base):
    """One row per line of a report: a 12-line report is 12 rows."""

    __tablename__ = "report_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"))
    section: Mapped[str | None] = mapped_column(Text)
    line_no: Mapped[int] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    numeric_value: Mapped[Decimal | None] = mapped_column(Numeric)
    unit: Mapped[str | None] = mapped_column(String(50))
    extra: Mapped[dict | None] = mapped_column(JSONB)  # unmapped cells, nothing is lost
    # 'simple' configuration: no stemming, so equipment tags (K-101) are kept intact.
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(section,'') || ' ' || coalesce(label,'')"
            " || ' ' || coalesce(text,''))",
            persisted=True,
        ),
    )

    report: Mapped[Report] = relationship(back_populates="lines")

    __table_args__ = (
        Index("ix_report_lines_report_line", "report_id", "line_no"),
        Index("ix_report_lines_search_vector", "search_vector", postgresql_using="gin"),
        # The trigram index is NOT created here: it is built after the historical
        # import (database/sql/post_import_indexes.sql) because it is large.
    )


class ReportMetric(Base):
    __tablename__ = "report_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"))
    equipment: Mapped[str | None] = mapped_column(String(100))
    metric: Mapped[str] = mapped_column(String(100))
    value: Mapped[Decimal | None] = mapped_column(Numeric)
    unit: Mapped[str | None] = mapped_column(String(50))
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_report_metrics_lookup", "equipment", "metric", "measured_at"),
        Index("ix_report_metrics_report", "report_id"),
    )


class RawCells(Base):
    """Raw grid of one sheet. Allows re-parsing and keyword scanning when parsing fails."""

    __tablename__ = "raw_cells"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id", ondelete="CASCADE"))
    sheet_name: Mapped[str] = mapped_column(Text)
    sheet_index: Mapped[int] = mapped_column(Integer)
    is_hidden: Mapped[bool] = mapped_column(Boolean, server_default="false")
    grid: Mapped[list] = mapped_column(JSONB)  # list of rows, each a list of cell values
    text_dump: Mapped[str | None] = mapped_column(Text)  # flattened text for keyword scans

    __table_args__ = (
        UniqueConstraint("source_file_id", "sheet_index"),
    )


class FailedParseQueue(Base):
    __tablename__ = "failed_parse_queue"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0")
    resolved: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = _ts()
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_failed_parse_unresolved", "resolved"),)


class KeywordRule(Base):
    __tablename__ = "keyword_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    keyword: Mapped[str] = mapped_column(Text)
    match_type: Mapped[str] = mapped_column(String(10), server_default="word")
    severity: Mapped[str] = mapped_column(String(10), server_default="warning")
    channel: Mapped[str] = mapped_column(String(10), server_default="email")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = _ts()

    __table_args__ = (
        CheckConstraint("match_type IN ('word','phrase','regex')", name="ck_keyword_match_type"),
        CheckConstraint("severity IN ('critical','warning','info')", name="ck_keyword_severity"),
        CheckConstraint("channel IN ('email','teams','both')", name="ck_keyword_channel"),
    )


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("keyword_rules.id"))
    report_id: Mapped[int | None] = mapped_column(ForeignKey("reports.id", ondelete="SET NULL"))
    report_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("report_lines.id", ondelete="SET NULL")
    )
    source_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_files.id", ondelete="SET NULL")
    )
    raw_ref: Mapped[dict | None] = mapped_column(JSONB)  # sheet/row/col when matched in raw cells
    matched_text: Mapped[str] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(Text)  # surrounding lines
    status: Mapped[str] = mapped_column(String(15), server_default="new")
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = _ts()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    action_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status IN ('new','sent','acknowledged')", name="ck_alert_status"),
        Index("ix_alert_events_status", "status", "created_at"),
    )


class ExpectedReport(Base):
    """Expected arrivals, for missing-report detection."""

    __tablename__ = "expected_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_type_id: Mapped[int] = mapped_column(ForeignKey("report_types.id"))
    expected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_report_id: Mapped[int | None] = mapped_column(
        ForeignKey("reports.id", ondelete="SET NULL")
    )
    warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("report_type_id", "expected_at"),)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, server_default="")
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(30))  # historical_import | live_import | alerts | backup
    status: Mapped[str] = mapped_column(String(10), server_default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status IN ('running','success','failed')", name="ck_job_runs_status"),
        Index("ix_job_runs_type_started", "job_type", "started_at"),
    )
