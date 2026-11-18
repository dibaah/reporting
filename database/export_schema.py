"""Regenerate docs/schema.md from the models (give that file to AI assistants)."""
from pathlib import Path

from database.models import Base

out = ["# Database schema (generated)\n",
       "Generated from `database/models.py`. Regenerate with `python -m database.export_schema`.\n"]
for t in Base.metadata.sorted_tables:
    out.append(f"\n## {t.name}\n\n| Column | Type | Notes |\n|---|---|---|")
    for c in t.columns:
        notes = []
        if c.primary_key:
            notes.append("PK")
        if c.foreign_keys:
            notes.append("FK -> " + ", ".join(sorted(f.target_fullname for f in c.foreign_keys)))
        if c.unique:
            notes.append("unique")
        if not c.nullable and not c.primary_key:
            notes.append("required")
        if c.computed is not None:
            notes.append("generated")
        out.append(f"| {c.name} | {c.type} | {', '.join(notes)} |")
Path(__file__).resolve().parents[1].joinpath("docs", "schema.md").write_text(
    "\n".join(out) + "\n", encoding="utf-8")
