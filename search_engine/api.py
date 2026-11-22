"""Preview server: JSON API + a small web page for searching and viewing reports.

  python -m search_engine.api            # http://127.0.0.1:8080

For now it has NO login. It listens on this computer only. Do not expose it to the network:
the real dashboard (Phase 7, .NET host with Windows authentication) will replace it. The JSON
contract below is what the Report plugin will offer inside that dashboard.
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from database.db import get_engine

from .service import (
    SearchFilters, get_context, get_report, get_report_types, get_stats, list_reports, search,
)

WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="Report Intelligence", version="0.4.0")


def get_session() -> Iterator[Session]:
    session = Session(bind=get_engine(), expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()


@app.get("/api/health")
def health(s: Session = Depends(get_session)) -> dict:
    get_stats(s)  # proves the database answers
    return {"status": "ok"}


@app.get("/api/stats")
def stats(s: Session = Depends(get_session)) -> dict:
    return get_stats(s)


@app.get("/api/report-types")
def report_types(s: Session = Depends(get_session)) -> list[dict]:
    return get_report_types(s)


@app.get("/api/search")
def api_search(
    q: str = "",
    type: str | None = None,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    equipment: str | None = None,
    mode: str = "fulltext",
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    s: Session = Depends(get_session),
) -> dict:
    try:
        page = search(s, SearchFilters(q, type, date_from, date_to, equipment, mode), limit, offset)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return page.to_dict()


@app.get("/api/reports")
def api_reports(
    type: str | None = None,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    s: Session = Depends(get_session),
) -> dict:
    return list_reports(s, type, date_from, date_to, limit, offset)


@app.get("/api/reports/{report_id}")
def api_report(report_id: int, s: Session = Depends(get_session)) -> dict:
    rep = get_report(s, report_id)
    if rep is None:
        raise HTTPException(404, "report not found")
    return rep


@app.get("/api/lines/{line_id}/context")
def api_context(line_id: int, n: int = Query(2, ge=0, le=20), s: Session = Depends(get_session)) -> list[dict]:
    return get_context(s, line_id, n)


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


def main(argv: list[str] | None = None) -> None:
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(prog="search_engine.api")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args(argv)
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: this preview has no login. Anyone who can reach this address can read all reports.")
    uvicorn.run(app, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
