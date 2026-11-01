"""Server-rendered HTML pages. Plain forms + redirects; no JavaScript needed."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from carscout.core.config import get_settings, load_sources
from carscout.core.db import get_session
from carscout.models import Listing, SavedSearch, ScrapeRun, Source, Vehicle
from carscout.sources.base import SearchParams
from carscout.web.queries import (
    SORTS,
    ListingFilters,
    car_price_history,
    comparables,
    facet_values,
    next_scheduled_run,
    query_listings,
    same_car,
    source_views,
)
from carscout.worker.queue import enqueue_search

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
DB = Annotated[Session, Depends(get_session)]

PRICE_TYPES = {"drive_away": "Drive away", "egc": "Excl. gov. charges",
               "negotiable": "Negotiable", "unknown": ""}


def _money(v: int | None) -> str:
    return "—" if v is None else f"${v:,}"


def _km(v: int | None) -> str:
    return "—" if v is None else f"{v:,} km"


def _ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    secs = int((datetime.now(UTC) - dt).total_seconds())
    if secs < 0:
        secs = -secs
        for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
            if secs >= n:
                k = secs // n
                return f"in {k} {unit}{'s' if k != 1 else ''}"
        return "in under a minute"
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= n:
            k = secs // n
            return f"{k} {unit}{'s' if k != 1 else ''} ago"
    return "just now"


def _local(dt: datetime | None, fmt: str = "%d %b %Y %H:%M") -> str:
    """Show times in SCHEDULE_TZ (the app's local time), not UTC."""
    if dt is None:
        return "—"
    return dt.astimezone(ZoneInfo(get_settings().schedule_tz)).strftime(fmt)


templates.env.filters.update(money=_money, km=_km, ago=_ago, local=_local,
                             price_type=lambda v: PRICE_TYPES.get(v, ""))


def _labels() -> dict[str, str]:
    return {name: c.label for name, c in load_sources().items()}


def _redirect(path: str, msg: str | None = None) -> RedirectResponse:
    url = f"{path}?{urlencode({'msg': msg})}" if msg else path
    return RedirectResponse(url, status_code=303)


def _render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("source_labels", _labels())
    return templates.TemplateResponse(request, name, ctx)


@router.get("/", response_class=HTMLResponse)
def listings_page(request: Request, db: DB) -> HTMLResponse:
    f = ListingFilters.from_query(dict(request.query_params))
    items, total = query_listings(db, f)
    pages = max(1, -(-total // f.per_page))
    base = {k: v for k, v in request.query_params.items() if k not in ("page", "msg") and v}
    return _render(request, "listings.html", items=items, total=total, f=f, pages=pages,
                   facets=facet_values(db), sorts=list(SORTS), others=same_car(db, items),
                   page_url=lambda p: "/?" + urlencode({**base, "page": p}))


@router.get("/listings/{listing_id}", response_class=HTMLResponse)
def listing_page(listing_id: int, request: Request, db: DB) -> HTMLResponse:
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404, "Listing not found")
    vehicle = db.get(Vehicle, listing.vehicle_id) if listing.vehicle_id else None
    return _render(request, "listing.html", l=listing, vehicle=vehicle,
                   others=same_car(db, [listing]).get(listing.id, []),
                   history=car_price_history(db, listing), comps=comparables(db, listing))


@router.get("/searches", response_class=HTMLResponse)
def searches_page(request: Request, db: DB) -> HTMLResponse:
    searches = list(db.scalars(select(SavedSearch).order_by(SavedSearch.id)))
    runs = list(db.scalars(select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(30)))
    return _render(request, "searches.html", searches=searches, runs=runs,
                   next_run=next_scheduled_run(),
                   states=["ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"])


@router.post("/searches")
def create_search(
    db: DB,
    name: Annotated[str, Form()],
    make: Annotated[str, Form()] = "",
    model: Annotated[str, Form()] = "",
    year_min: Annotated[str, Form()] = "",
    year_max: Annotated[str, Form()] = "",
    km_max: Annotated[str, Form()] = "",
    price_max: Annotated[str, Form()] = "",
    states: Annotated[list[str] | None, Form()] = None,
    schedule_enabled: Annotated[str, Form()] = "",
) -> RedirectResponse:
    def num(v: str) -> int | None:
        v = v.replace(",", "").replace("$", "").strip()
        return int(v) if v.isdigit() else None

    params = SearchParams(make=make.strip() or None, model=model.strip() or None,
                          year_min=num(year_min), year_max=num(year_max), km_max=num(km_max),
                          price_max=num(price_max), states=states or [])
    db.add(SavedSearch(name=name.strip()[:200] or "Untitled", params=params.to_dict(),
                       schedule_enabled=bool(schedule_enabled)))
    db.commit()
    return _redirect("/searches", f"Saved “{name.strip()}”.")


@router.post("/searches/{search_id}/run")
def run_search(search_id: int, db: DB) -> RedirectResponse:
    search = db.get(SavedSearch, search_id)
    if not search:
        raise HTTPException(404)
    runs = enqueue_search(db, search)
    db.commit()
    if not runs:
        return _redirect("/searches", "Nothing queued: no enabled sources, or it's already running.")
    return _redirect("/searches", f"Queued {len(runs)} source(s): {', '.join(r.source for r in runs)}.")


@router.post("/searches/{search_id}/delete")
def delete_search(search_id: int, db: DB) -> RedirectResponse:
    search = db.get(SavedSearch, search_id)
    if search:
        db.delete(search)
        db.commit()
    return _redirect("/searches", "Search deleted. Its listings are kept.")


@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, db: DB) -> HTMLResponse:
    return _render(request, "sources.html", sources=source_views(db), next_run=next_scheduled_run())


@router.post("/sources/{name}/toggle")
def toggle_source(name: str, db: DB) -> RedirectResponse:
    views = {v.name: v for v in source_views(db)}
    if name not in views or not views[name].toggle_allowed:
        return _redirect("/sources", f"{name} can't be toggled here; see sources.yaml.")
    row = db.get(Source, name)
    row.enabled = not row.enabled
    db.commit()
    return _redirect("/sources", f"{views[name].label} turned {'on' if row.enabled else 'off'}.")
