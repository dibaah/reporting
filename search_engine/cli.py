"""Search from the command line.

  python -m search_engine.cli "vibration K-101"
  python -m search_engine.cli "leak" --type 12H --from 2024-01-01 --to 2024-12-31 --context 2
  python -m search_engine.cli "compres" --partial          # substrings and typos
  python -m search_engine.cli "\"no leak\" -detector"      # phrase and exclusion
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from database.db import session_scope

from .service import SearchFilters, get_context, search


def _date(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="search_engine.cli", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("--type", dest="report_type")
    ap.add_argument("--from", dest="date_from", type=_date)
    ap.add_argument("--to", dest="date_to", type=_date)
    ap.add_argument("--equipment")
    ap.add_argument("--partial", action="store_true", help="substring and typo matching instead of whole words")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--context", type=int, default=0, help="also show N lines before and after each hit")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    f = SearchFilters(a.query, a.report_type, a.date_from, a.date_to, a.equipment, "partial" if a.partial else "fulltext")
    try:
        with session_scope() as s:
            page = search(s, f, a.limit, a.offset)
            ctx = {h.line_id: get_context(s, h.line_id, a.context) for h in page.hits} if a.context else {}
    except ValueError as e:
        print(f"error: {e}")
        return 2
    if a.json:
        print(json.dumps(page.to_dict(), indent=2, default=str, ensure_ascii=False))
        return 0
    if not page.hits:
        print("No matches.")
        return 0
    for h in page.hits:
        print(f"{h.report_date}  {h.report_type:<8} {h.shift or '':<6} #{h.report_id}/{h.line_no}  "
              f"[{h.section or '-'}] {h.label or ''}: {h.text or ''}")
        for c in ctx.get(h.line_id, []):
            if not c["is_hit"]:
                print(f"{'':>10}   · {c['line_no']:>3} {c['label'] or ''}: {c['text'] or ''}")
    print(f"\n{len(page.hits)} shown" + (f" (more available: --offset {page.offset + page.limit})" if page.has_more else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
