"""Keyword matching: whole words, phrases and regular expressions, plus simple negation.

word / phrase  case-insensitive, whole words only ("leak" does not match "leakage" or "leaks";
               use a regex such as  leak(s|age|ing)?  for word families). Words in a phrase may be
               separated by any amount of whitespace. Tags like K-101 work: K-1010 does not match.
regex          Python regular expression, case-insensitive. Must not match an empty string.

Negation (Warning and Info keywords only): "no leak detected" and "leak: none" are ignored.
Critical keywords are NEVER suppressed: a false alarm is better than a missed one, and the alert
shows the surrounding lines so you can see it at a glance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_LINE_CHARS = 2000
MAX_MATCHES_PER_LINE = 5

NEGATORS = {"no", "not", "without", "none", "nil", "zero", "never", "non", "neither", "nor", "absent", "negative", "nothing"}
_BEFORE_BOUNDARY = re.compile(r"[.;:!?,|\n]")
_AFTER_BOUNDARY = re.compile(r"[.;!?,|\n]")  # ':' is allowed after the keyword: "leak: none"
_WORD = re.compile(r"[\w'-]+")


class RuleError(ValueError):
    """The keyword rule cannot be compiled (empty, or an invalid / too broad regex)."""


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    text: str


@dataclass
class CompiledRule:
    id: int | None
    keyword: str
    match_type: str
    severity: str
    channel: str
    regex: re.Pattern

    def find(self, text: str) -> list[Match]:
        out = []
        for m in self.regex.finditer(text[:MAX_LINE_CHARS]):
            if m.end() > m.start():
                out.append(Match(m.start(), m.end(), m.group()))
                if len(out) >= MAX_MATCHES_PER_LINE:
                    break
        return out


def build_pattern(keyword: str, match_type: str) -> re.Pattern:
    kw = (keyword or "").strip()
    if not kw:
        raise RuleError("keyword is empty")
    if match_type == "regex":
        if len(kw) > 200:
            raise RuleError("regex is longer than 200 characters")
        try:
            rx = re.compile(kw, re.IGNORECASE)
        except re.error as e:
            raise RuleError(f"invalid regex: {e}")
        if rx.search("") is not None:
            raise RuleError("regex matches an empty string, so it would match every line")
        return rx
    if match_type not in ("word", "phrase"):
        raise RuleError(f"unknown match type {match_type!r}")
    body = r"\s+".join(re.escape(tok) for tok in kw.split())
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def compile_rule(rule) -> CompiledRule:
    """`rule` is a KeywordRule row (or anything with the same attributes)."""
    return CompiledRule(getattr(rule, "id", None), rule.keyword, rule.match_type, rule.severity,
                        rule.channel, build_pattern(rule.keyword, rule.match_type))


def is_negated(text: str, match: Match) -> bool:
    """True when the hit is negated in the same clause: a negator within 3 words before it
    ("no sign of leak") or within 2 words after it ("leak not detected", "leak: none")."""
    before = text[: match.start]
    cut = list(_BEFORE_BOUNDARY.finditer(before))
    clause_before = before[cut[-1].end():] if cut else before
    if any(w in NEGATORS for w in _WORD.findall(clause_before.lower())[-3:]):
        return True
    after = text[match.end:]
    cut2 = _AFTER_BOUNDARY.search(after)
    clause_after = after[: cut2.start()] if cut2 else after
    return any(w in NEGATORS for w in _WORD.findall(clause_after.lower())[:2])
