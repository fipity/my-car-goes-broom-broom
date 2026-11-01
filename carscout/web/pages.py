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
from carscout.geo.postcodes import postcode_centre
from carscout.web.charts import price_chart
from carscout.web.queries import (
    SORTS,
    ListingFilters,
    car_price_history,
    comparables,
    facet_values,
    more_filters_set,
    search_changes,
    search_listings,
    update_search,
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
    f = ListingFilters.from_query(request.query_params)
    items, total = query_listings(db, f)
    pages = max(1, -(-total // f.per_page))
    base = [(k, v) for k, v in request.query_params.multi_items() if k not in ("page", "msg") and v]
    return _render(request, "listings.html", items=items, total=total, f=f, pages=pages,
                   facets=facet_values(db), sorts=[*SORTS, "distance"], others=same_car(db, items),
                   more_open=more_filters_set(f),
                   geo_ok=bool(f.postcode and postcode_centre(db, f.postcode)),
                   page_url=lambda p: "/?" + urlencode([*base, ("page", p)]))


@router.get("/listings/{listing_id}", response_class=HTMLResponse)
def listing_page(listing_id: int, request: Request, db: DB) -> HTMLResponse:
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404, "Listing not found")
    vehicle = db.get(Vehicle, listing.vehicle_id) if listing.vehicle_id else None
    others = same_car(db, [listing]).get(listing.id, [])
    history = car_price_history(db, listing)
    return _render(request, "listing.html", l=listing, vehicle=vehicle, others=others,
                   history=history, chart=price_chart(history, [listing, *others]),
                   comps=comparables(db, listing))


STATES = ["ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"]


def _source_choices() -> list[tuple[str, str]]:
    """Sources a search can be limited to: working real ones first, then demo."""
    cfgs = load_sources().values()
    return [(c.name, c.label) for c in sorted(cfgs, key=lambda c: c.synthetic)
            if c.enabled and c.implemented]


def _search_from_form(form: Any) -> tuple[str, SearchParams, list[str] | None, bool]:
    def num(name: str) -> int | None:
        v = str(form.get(name) or "").replace(",", "").replace("$", "").strip()
        return int(v) if v.isdigit() else None

    def text(name: str) -> str | None:
        return str(form.get(name) or "").strip() or None

    params = SearchParams(
        make=text("make"), model=text("model"), variant=text("variant"), body_type=text("body_type"),
        transmission=text("transmission"), year_min=num("year_min"), year_max=num("year_max"),
        km_max=num("km_max"), price_min=num("price_min"), price_max=num("price_max"),
        states=[x for x in form.getlist("states") if x in STATES],
    )
    known = {n for n, _ in _source_choices()}
    sources = [x for x in form.getlist("sources") if x in known] or None
    return (text("name") or "Untitled")[:200], params, sources, bool(form.get("schedule_enabled"))


@router.get("/searches", response_class=HTMLResponse)
def searches_page(request: Request, db: DB) -> HTMLResponse:
    searches = list(db.scalars(select(SavedSearch).order_by(SavedSearch.id)))
    runs = list(db.scalars(select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(30)))
    return _render(request, "searches.html", searches=searches, runs=runs,
                   changes={s.id: search_changes(db, s) for s in searches},
                   next_run=next_scheduled_run(), states=STATES, source_choices=_source_choices())


@router.post("/searches")
async def create_search(request: Request, db: DB) -> RedirectResponse:
    name, params, sources, scheduled = _search_from_form(await request.form())
    search = SavedSearch(name=name, params=params.to_dict(), sources=sources, schedule_enabled=scheduled)
    db.add(search)
    db.commit()
    return _redirect(f"/searches/{search.id}", f"Saved “{name}”. Press Run now to fetch listings.")


@router.get("/searches/{search_id}", response_class=HTMLResponse)
def search_page(search_id: int, request: Request, db: DB, view: str = "", sort: str = "deal") -> HTMLResponse:
    search = db.get(SavedSearch, search_id)
    if not search:
        raise HTTPException(404, "Search not found")
    ch = search_changes(db, search)
    if view not in ("new", "drops", "all"):
        view = "new" if ch.new else "drops" if ch.drops else "all"
    items = search_listings(db, search, sort) if view == "all" else ch.new if view == "new" else [d.listing for d in ch.drops]
    runs = list(db.scalars(select(ScrapeRun).where(ScrapeRun.saved_search_id == search_id)
                           .order_by(ScrapeRun.id.desc()).limit(10)))
    return _render(request, "search.html", s=search, ch=ch, view=view, sort=sort, items=items,
                   drops={d.listing.id: d for d in ch.drops}, others=same_car(db, items), runs=runs,
                   states=STATES, source_choices=_source_choices())


@router.post("/searches/{search_id}/edit")
async def edit_search(search_id: int, request: Request, db: DB) -> RedirectResponse:
    search = db.get(SavedSearch, search_id)
    if not search:
        raise HTTPException(404)
    name, params, sources, scheduled = _search_from_form(await request.form())
    changed = update_search(db, search, name=name, params=params.to_dict(), sources=sources,
                            schedule_enabled=scheduled)
    db.commit()
    msg = "Search updated." + (" Its criteria changed, so press Run now to refresh the matches." if changed else "")
    return _redirect(f"/searches/{search_id}", msg)


@router.post("/searches/{search_id}/seen")
def mark_seen(search_id: int, db: DB) -> RedirectResponse:
    search = db.get(SavedSearch, search_id)
    if not search:
        raise HTTPException(404)
    search.seen_at = datetime.now(UTC)
    db.commit()
    return _redirect(f"/searches/{search_id}?view=all", "Marked as seen. New listings and price drops from now on will show here.")


@router.post("/searches/{search_id}/run")
def run_search(search_id: int, db: DB) -> RedirectResponse:
    search = db.get(SavedSearch, search_id)
    if not search:
        raise HTTPException(404)
    runs = enqueue_search(db, search)
    db.commit()
    back = f"/searches/{search_id}"
    if not runs:
        return _redirect(back, "Nothing queued: no enabled sources, or it's already running.")
    return _redirect(back, f"Queued {len(runs)} source(s): {', '.join(r.source for r in runs)}. Refresh in a minute or two.")


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
