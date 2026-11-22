"""Phase 4: search function, service, highlighting, JSON API and CLI.

Test data is dated 2999 and uses ZQX tags, so results stay deterministic even when the
database already holds real reports. Everything is rolled back (CLI test: deleted again)."""
import datetime as dt
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from database import seed
from database.db import get_engine, session_scope
from database.models import Report, ReportType, SourceFile
from search_engine import api as api_module
from search_engine import cli
from search_engine.api import app, get_session
from search_engine.highlight import highlight_html, query_terms
from search_engine.service import (
    SearchFilters, get_context, get_report, get_stats, list_reports, search,
)
from database.models import ReportLine

FROM = dt.date(2999, 1, 1)

A_LINES = [
    ("Compressors", "ZQX-101", "Vibration high on compressor ZQX-101"),
    ("Compressors", "ZQX-102", "Vibration normal"),
    ("Safety", "Gas detectors", "No leak detected"),
    ("Safety", "Flare", "Leak detected at flare header, 100% shutdown <script>alert(1)</script>"),
    ("Remarks", "Note", "Standby unit ZQX-103 started"),
]
B_LINES = [("Summary", "ZQX-101", "Overhaul completed on ZQX-101"), ("Summary", "Downtime", "Planned downtime 1.5 h")]
C_LINES = [("Misc", "Item", "Compresor inspection due"), ("Misc", "ZQX_Under", "underscore_literal value")]


@pytest.fixture(scope="module", autouse=True)
def _seed():
    seed.run()


def make_report(session, rtype, date, lines, shift="Day"):
    rt = session.scalar(select(ReportType).where(ReportType.name == rtype))
    sf = SourceFile(origin="archive", file_name="zqx_search_test.xlsx", sha256=uuid.uuid4().hex * 2, path="zqx://test")
    session.add(sf)
    session.flush()
    rep = Report(report_type_id=rt.id, source_file_id=sf.id, report_date=date, shift=shift,
                 header={"superintendent": "Test Person", "_parser": {"warnings": ["demo warning"]}})
    rep.lines = [ReportLine(line_no=i, section=s, label=l, text=t) for i, (s, l, t) in enumerate(lines, 1)]
    session.add(rep)
    session.flush()
    return rep


@pytest.fixture()
def db():
    conn = get_engine().connect()
    trans = conn.begin()
    s = Session(bind=conn, expire_on_commit=False)
    s.A = make_report(s, "12H", dt.date(2999, 1, 1), A_LINES)
    s.B = make_report(s, "Monthly", dt.date(2999, 2, 1), B_LINES, shift=None)
    s.C = make_report(s, "12H", dt.date(2999, 3, 1), C_LINES, shift="Night")
    yield s
    s.close()
    trans.rollback()
    conn.close()


def find(db, q="", **kw):
    kw.setdefault("date_from", FROM)
    return search(db, SearchFilters(query=q, **kw), limit=100)


def keys(page):  # (report letter-less id, line_no) pairs in order
    return [(h.report_id, h.line_no) for h in page.hits]


# ---------------------------------------------------------------- full text

def test_equipment_tag_matches_exactly_and_not_its_neighbours(db):
    page = find(db, "ZQX-101")
    assert keys(page) == [(db.B.id, 1), (db.A.id, 1)]  # newest report first; ZQX-102/103 are not hits


def test_phrase_exclusion_and_or(db):
    assert keys(find(db, '"no leak detected"')) == [(db.A.id, 3)]
    assert keys(find(db, "leak -no")) == [(db.A.id, 4)]
    assert {k for k in keys(find(db, "overhaul OR standby"))} == {(db.B.id, 1), (db.A.id, 5)}


def test_fulltext_needs_whole_words_but_partial_finds_parts_and_typos(db):
    assert find(db, "101").hits == []                       # 'ZQX-101' is the token, '101' alone is not
    assert {k for k in keys(find(db, "101", mode="partial"))} == {(db.B.id, 1), (db.A.id, 1)}
    assert find(db, "vibraton").hits == []                  # typo, full text
    assert {k for k in keys(find(db, "vibraton", mode="partial"))} == {(db.A.id, 1), (db.A.id, 2)}
    comp = {k for k in keys(find(db, "compres", mode="partial"))}
    assert (db.A.id, 1) in comp and (db.C.id, 1) in comp and (db.A.id, 2) in comp  # incl. section text and 'Compresor'


def test_percent_and_underscore_are_literal_characters(db):
    assert keys(find(db, "100%", mode="partial")) == [(db.A.id, 4)]
    assert keys(find(db, "%", mode="partial")) == [(db.A.id, 4)]          # not "match everything"
    assert keys(find(db, "_", mode="partial")) == [(db.C.id, 2)]


# ---------------------------------------------------------------- filters, ordering, paging

def test_filters_type_dates_and_equipment(db):
    assert {h.report_type for h in find(db, "ZQX-101", report_type="Monthly").hits} == {"Monthly"}
    assert keys(find(db, "ZQX-101", date_to=dt.date(2999, 1, 31))) == [(db.A.id, 1)]
    assert keys(find(db, "", equipment="ZQX-101")) == [(db.B.id, 1), (db.A.id, 1)]  # blank query + equipment filter


def test_blank_query_browses_newest_first_and_pages_without_gaps_or_repeats(db):
    first = search(db, SearchFilters(date_from=FROM), limit=4, offset=0)
    assert first.has_more and first.hits[0].report_date == dt.date(2999, 3, 1)
    seen = keys(first)
    offset = 4
    while True:
        page = search(db, SearchFilters(date_from=FROM), limit=4, offset=offset)
        seen += keys(page)
        offset += 4
        if not page.has_more:
            break
    assert len(seen) == len(set(seen)) == len(A_LINES) + len(B_LINES) + len(C_LINES)


def test_bad_arguments_are_rejected(db):
    with pytest.raises(ValueError):
        search(db, SearchFilters(query="x", mode="regex"))
    with pytest.raises(ValueError):
        search(db, SearchFilters(date_from=dt.date(2999, 2, 1), date_to=dt.date(2999, 1, 1)))


# ---------------------------------------------------------------- safe highlighting

def test_query_terms_and_highlight_escape_everything():
    assert query_terms('"no leak" K-101 -skip or Flare') == ["no leak", "Flare", "K-101"]
    out = highlight_html("Leak <b>bold</b> & leak", ["leak"])
    assert out == "<mark>Leak</mark> &lt;b&gt;bold&lt;/b&gt; &amp; <mark>leak</mark>"
    assert highlight_html(None, ["x"]) == "" and highlight_html("a<b", []) == "a&lt;b"


def test_hits_carry_safe_html(db):
    hit = next(h for h in find(db, "leak").hits if h.line_no == 4)
    assert "<mark>Leak</mark>" in hit.text_html and "<script>" not in hit.text_html and "&lt;script&gt;" in hit.text_html


# ---------------------------------------------------------------- reports, context, numbers

def test_get_report_context_and_listing(db):
    rep = get_report(db, db.A.id)
    assert [l["line_no"] for l in rep["lines"]] == [1, 2, 3, 4, 5]
    assert rep["header"] == {"superintendent": "Test Person"} and rep["warnings"] == ["demo warning"]
    assert rep["source"]["file_name"] == "zqx_search_test.xlsx"
    assert get_report(db, 2_000_000_000) is None
    line3 = rep["lines"][2]["id"]
    ctx = get_context(db, line3, 1)
    assert [c["line_no"] for c in ctx] == [2, 3, 4] and [c["is_hit"] for c in ctx] == [False, True, False]
    assert len(get_context(db, line3, 0)) == 1 and get_context(db, -1) == []
    lst = list_reports(db, report_type="Monthly", date_from=FROM)
    assert [(r["id"], r["lines"]) for r in lst["reports"]] == [(db.B.id, 2)]


def test_stats_count_what_exists(db):
    s = get_stats(db)
    assert s["reports"] >= 3 and s["lines"] >= 9 and s["last_report_date"] == dt.date(2999, 3, 1)
    assert {t["name"] for t in s["by_type"]} >= {"12H", "Biweekly", "Monthly"}


# ---------------------------------------------------------------- JSON API

@pytest.fixture()
def client(db):
    app.dependency_overrides[get_session] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_api_search_and_validation(client, db):
    r = client.get("/api/search", params={"q": "ZQX-101", "from": "2999-01-01"})
    body = r.json()
    assert r.status_code == 200 and [h["line_no"] for h in body["hits"]] == [1, 1] and body["has_more"] is False
    assert set(body["hits"][0]) >= {"line_id", "report_id", "report_date", "text_html", "label_html", "rank"}
    assert client.get("/api/search", params={"q": "x", "mode": "bogus"}).status_code == 422
    assert client.get("/api/search", params={"from": "2999-02-01", "to": "2999-01-01"}).status_code == 422
    assert client.get("/api/search", params={"limit": 0}).status_code == 422
    assert client.get("/api/search", params={"from": "not-a-date"}).status_code == 422


def test_api_html_fields_never_contain_raw_markup(client):
    """`text` is plain data (the page inserts it with textContent); every *_html field is escaped."""
    hits = client.get("/api/search", params={"q": "leak", "from": "2999-01-01"}).json()["hits"]
    flare = next(h for h in hits if h["line_no"] == 4)
    assert "<script>" in flare["text"]                     # raw data, for clients that escape themselves
    for h in hits:
        for field in ("section_html", "label_html", "text_html"):
            assert "<script" not in h[field] and "</" not in h[field].replace("</mark>", "")
    assert "&lt;script&gt;" in flare["text_html"]


def test_api_reports_context_stats_types_health(client, db):
    r = client.get(f"/api/reports/{db.A.id}")
    assert r.status_code == 200 and len(r.json()["lines"]) == 5 and r.json()["report_date"] == "2999-01-01"
    assert client.get("/api/reports/2000000000").status_code == 404
    line = r.json()["lines"][2]["id"]
    assert [c["line_no"] for c in client.get(f"/api/lines/{line}/context", params={"n": 1}).json()] == [2, 3, 4]
    assert client.get("/api/reports", params={"type": "Monthly", "from": "2999-01-01"}).json()["reports"][0]["id"] == db.B.id
    assert client.get("/api/stats").json()["reports"] >= 3
    assert {t["name"] for t in client.get("/api/report-types").json()} >= {"12H", "Monthly"}
    assert client.get("/api/health").json() == {"status": "ok"}


def test_web_page_and_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "Report Intelligence" in page.text
    assert client.get("/static/app.js").status_code == 200 and client.get("/static/app.css").status_code == 200


# ---------------------------------------------------------------- command line (needs committed data)

@pytest.fixture()
def committed():
    with session_scope() as s:
        make_report(s, "12H", dt.date(2999, 1, 1), A_LINES)
    yield
    with session_scope() as s:
        ids = s.scalars(select(SourceFile.id).where(SourceFile.path == "zqx://test")).all()
        s.execute(delete(Report).where(Report.source_file_id.in_(ids)))
        s.execute(delete(SourceFile).where(SourceFile.id.in_(ids)))


def test_cli_prints_context_and_json(committed, capsys):
    assert cli.main(["ZQX-101", "--from", "2999-01-01", "--context", "1"]) == 0
    out = capsys.readouterr().out
    assert "ZQX-101" in out and "ZQX-102" in out and "1 shown" in out
    assert cli.main(["ZQX-101", "--from", "2999-01-01", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["hits"][0]["label"] == "ZQX-101"
    assert cli.main(["nothing-matches-this-zzz", "--from", "2999-01-01"]) == 0
    assert "No matches." in capsys.readouterr().out
    assert cli.main(["x", "--from", "2999-02-01", "--to", "2999-01-01"]) == 2
