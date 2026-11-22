"""Safe keyword highlighting. All text is HTML-escaped BEFORE <mark> tags are added, so report
content (which comes from Excel files) can never inject markup into a page."""
from __future__ import annotations

import html
import re

_TOKEN = re.compile(r'"([^"]+)"|(\S+)')


def query_terms(query: str | None) -> list[str]:
    """Words and "quoted phrases" of a web-style query. Skips `or` and -excluded words."""
    terms: set[str] = set()
    for m in _TOKEN.finditer(query or ""):
        phrase, word = m.groups()
        if phrase:
            terms.add(phrase.strip())
        elif word and not word.startswith("-") and word.lower() != "or":
            terms.add(word.strip('"'))
    return sorted((t for t in terms if t), key=len, reverse=True)  # longest first: phrases win


def highlight_html(text: str | None, terms: list[str]) -> str:
    if not text:
        return ""
    if not terms:
        return html.escape(text)
    rx = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    out, pos = [], 0
    for m in rx.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        out.append("<mark>" + html.escape(m.group()) + "</mark>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)
