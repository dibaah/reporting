"""Template base class and registry.

Adding a new report type or a new layout (era) = add one Template subclass in
parser_engine/templates/ and decorate it with @register.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .contract import ParsedReport
from .workbook import Workbook


class Template(ABC):
    name: str  # unique, stored in report_templates.parser_name
    report_type: str  # must exist in report_types (for example "12H")
    era: str | None = None
    valid_from: str | None = None  # informational, ISO date
    valid_to: str | None = None

    @abstractmethod
    def detect(self, wb: Workbook) -> float:
        """Confidence 0.0 to 1.0 that this workbook has this layout. Must not raise."""

    @abstractmethod
    def parse(self, wb: Workbook) -> ParsedReport:
        """Return the standard structure. Raise only for real surprises: the pipeline turns
        exceptions into failed_parse_queue entries."""


@dataclass
class Resolution:
    template: Template | None
    confidence: float
    scores: dict[str, float] = field(default_factory=dict)
    ambiguous: bool = False


class Registry:
    def __init__(self, min_confidence: float = 0.6, tie_margin: float = 0.05):
        self._templates: dict[str, Template] = {}
        self.min_confidence = min_confidence
        self.tie_margin = tie_margin

    def add(self, template: Template) -> Template:
        if template.name in self._templates:
            raise ValueError(f"duplicate template name {template.name!r}")
        self._templates[template.name] = template
        return template

    def templates(self) -> list[Template]:
        return list(self._templates.values())

    def get(self, name: str) -> Template | None:
        return self._templates.get(name)

    def resolve(self, wb: Workbook) -> Resolution:
        scores: dict[str, float] = {}
        for t in self._templates.values():
            try:
                scores[t.name] = max(0.0, min(1.0, float(t.detect(wb))))
            except Exception:
                scores[t.name] = 0.0
        if not scores:
            return Resolution(None, 0.0, scores)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best_name, best = ranked[0]
        if best < self.min_confidence:
            return Resolution(None, best, scores)
        ambiguous = len(ranked) > 1 and ranked[1][1] >= self.min_confidence and best - ranked[1][1] <= self.tie_margin
        return Resolution(self._templates[best_name], best, scores, ambiguous)


# Registry used by the workers. Templates add themselves with @register.
default_registry = Registry()


def register(cls):
    """Class decorator: instantiate the template and add it to the default registry."""
    default_registry.add(cls())
    return cls
