"""The report contract: every template returns this structure, nothing else.

The API and the UI depend only on this shape. Change it in the Master Plan first.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any


@dataclass
class ParsedLine:
    line_no: int
    section: str | None = None
    label: str | None = None
    text: str | None = None
    numeric_value: Decimal | float | int | None = None
    unit: str | None = None
    extra: dict[str, Any] | None = None  # cells that could not be mapped: nothing is lost


@dataclass
class ParsedMetric:
    metric: str
    value: Decimal | float | int | None = None
    equipment: str | None = None
    unit: str | None = None
    measured_at: datetime | None = None


@dataclass
class ParsedReport:
    report_type: str  # must match report_types.name (for example "12H")
    template_name: str
    report_date: date | None = None  # taken from the report content, never from the folder
    shift: str | None = None
    header: dict[str, Any] = field(default_factory=dict)
    lines: list[ParsedLine] = field(default_factory=list)
    metrics: list[ParsedMetric] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form (used by golden tests and the CLI)."""

        def conv(v: Any) -> Any:
            if isinstance(v, (date, datetime)):
                return v.isoformat()
            if isinstance(v, Decimal):
                return float(v)
            if isinstance(v, dict):
                return {k: conv(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [conv(x) for x in v]
            return v

        return conv(asdict(self))


# Failure reason codes written to failed_parse_queue.reason
REASON_UNREADABLE = "unreadable_file"
REASON_PASSWORD = "password_protected"
REASON_UNSUPPORTED = "unsupported_format"
REASON_NO_TEMPLATE = "no_template_match"
REASON_PARSER_ERROR = "parser_error"
REASON_VALIDATION = "validation_failed"
REASON_UNKNOWN_TYPE = "unknown_report_type"


@dataclass
class ParseFailure:
    reason: str
    detail: str = ""


@dataclass
class RawSheet:
    """One sheet as stored in raw_cells: lets us re-parse later and scan keywords."""

    index: int
    name: str
    hidden: bool
    grid: list[list[Any]]  # JSON-safe values
    text_dump: str


@dataclass
class ParseOutcome:
    file_format: str | None = None
    report: ParsedReport | None = None
    failure: ParseFailure | None = None
    raw_sheets: list[RawSheet] = field(default_factory=list)
    template_name: str | None = None
    confidence: float | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.report is not None and self.failure is None
