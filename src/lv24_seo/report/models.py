"""Report model shared by builders, renderers and delivery.

A report is pure data: builders produce it, renderers format it, delivery sends it.
Nothing in a report may contain a token, a raw provider payload or a personal identifier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ReportSection:
    """One block of a report: a headline sentence plus optional rows/notes.

    ``total``/``truncated``/``data_age`` are optional bounded-response metadata.
    They stay ``None`` for every historical section, so the serialized shape of
    an existing report is unchanged; a section that caps its rows (the change
    digest) sets them and they are then part of the payload.
    """

    key: str
    title: str
    summary: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    total: int | None = None
    truncated: bool | None = None
    data_age: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.key,
            "title": self.title,
            "summary": self.summary,
            "rows": self.rows,
            "notes": self.notes,
        }
        if self.total is not None:
            payload["total"] = self.total
        if self.truncated is not None:
            payload["truncated"] = self.truncated
        if self.data_age is not None:
            payload["data_age"] = self.data_age
        return payload


@dataclass(slots=True)
class Report:
    kind: str  # "weekly" | "indexing" | "opportunities" | "audit"
    property_id: str
    period_start: str  # ISO date
    period_end: str
    headline: str
    sections: list[ReportSection] = field(default_factory=list)
    generated_at: str = ""
    limitations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def section(self, key: str) -> ReportSection | None:
        return next((s for s in self.sections if s.key == key), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "property_id": self.property_id,
            "period": {"start": self.period_start, "end": self.period_end},
            "generated_at": self.generated_at,
            "headline": self.headline,
            "sections": [s.to_dict() for s in self.sections],
            "limitations": self.limitations,
            "warnings": self.warnings,
        }
