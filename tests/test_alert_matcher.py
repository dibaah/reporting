"""Phase 6: keyword matching and negation (no database)."""
import pytest

from alert_service.matcher import Match, RuleError, build_pattern, compile_rule, is_negated


def rule(keyword, match_type="word", severity="warning"):
    return compile_rule(type("R", (), dict(id=1, keyword=keyword, match_type=match_type, severity=severity, channel="email")))


@pytest.mark.parametrize("keyword,text,expected", [
    ("leak", "Leak detected at flange", True),
    ("leak", "gas leakage found", False),                 # whole words only
    ("leak", "two leaks", False),
    ("K-101", "K-101 tripped", True),
    ("K-101", "K-1010 tripped", False),
    ("K-101", "XK-101 tripped", False),
    ("K-101", "units (K-101) and K-102", True),
    ("gas leak", "A GAS   LEAK was reported", True),      # phrase: any whitespace, any case
    ("gas leak", "gas and leak", False),
    ("flare", "Flare pilot lit", True),
])
def test_word_and_phrase_matching(keyword, text, expected):
    assert bool(rule(keyword).find(text)) is expected


def test_special_characters_are_literal_in_words():
    assert rule("P&ID (rev.2)").find("see P&ID (rev.2) now")
    assert not rule("a.b").find("axb")


def test_regex_rules():
    r = rule(r"leak(s|age|ing)?", "regex")
    assert [m.text for m in r.find("Leakage and leaking, one leak")] == ["Leakage", "leaking", "leak"]
    assert rule(r"K-10\d", "regex").find("K-104 high")


@pytest.mark.parametrize("keyword,mtype,message", [
    ("", "word", "empty"), ("   ", "phrase", "empty"), ("(unclosed", "regex", "invalid regex"),
    ("a*", "regex", "empty string"), ("x" * 201, "regex", "200"), ("leak", "fuzzy", "unknown match type"),
])
def test_unusable_rules_are_rejected(keyword, mtype, message):
    with pytest.raises(RuleError, match=message):
        build_pattern(keyword, mtype)


def test_match_positions_and_cap():
    ms = rule("a").find("a a a a a a a")
    assert len(ms) == 5 and ms[0] == Match(0, 1, "a")


def neg(text, keyword="leak"):
    m = rule(keyword).find(text)[0]
    return is_negated(text, m)


@pytest.mark.parametrize("text,expected", [
    ("No leak detected", True),
    ("Gas detectors | No leak detected", True),
    ("leak: none", True),
    ("leak not detected", True),
    ("no sign of leak", True),
    ("Result: no leak", True),
    ("without any leak", True),
    ("Heavy leak at flange", False),
    ("no damage, leak at flange", False),                  # a comma starts a new clause
    ("Leak found. No repairs needed", False),               # the negator is in the next sentence
    ("leak detected and no repairs needed", False),         # too far after the keyword
    ("not all units tripped but leak found", False),        # too far before it
    ("small leak, none critical", False),
])
def test_negation(text, expected):
    assert neg(text) is expected
