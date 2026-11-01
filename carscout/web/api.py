"""JSON API under /api."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from carscout.core.config import load_sources
from carscout.core.db import get_session
from carscout.models import Listing, SavedSearch, ScrapeRun, Source, Vehicle
from carscout.sources.base import SearchParams
from carscout.web.queries import (
    ListingFilters,
    car_price_history,
    next_scheduled_run,
    query_listings,
    same_car,
    source_views,
)
from carscout.worker.queue import enqueue_search

router = APIRouter(prefix="/api", tags=["api"])
DB = Annotated[Session, Depends(get_session)]


class Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class OtherListing(Orm):
    """The same car listed elsewhere (another site, or a relisting)."""
    id: int
    source: str
    url: str
    price: int | None
    price_type: str
    odometer_km: int | None
    is_active: bool


class ValuationOut(Orm):
    estimate: int | None
    p25: int | None
    p75: int | None
    diff_abs: int | None
    diff_pct: float | None
    label: str | None        # Great deal | Good deal | Fair | Above market | Overpriced
    confidence: str | None   # High | Medium | Low
    method: str | None       # regression | comps | insufficient
    n_comps: int
    comp_ids: list[int]      # comparable listings, closest first
    details: dict[str, Any]
    computed_at: datetime


class ListingOut(Orm):
    id: int
    source: str
    source_listing_id: str
    url: str
    title: str
    make: str | None
    model: str | None
    variant: str | None
    year: int | None
    odometer_km: int | None
    price: int | None
    price_type: str
    body_type: str | None
    transmission: str | None
    fuel_type: str | None
    engine: str | None
    drivetrain: str | None
    colour: str | None
    seller_type: str
    suburb: str | None
    state: str | None
    postcode: str | None
    images: list[str]
    rego: str | None
    vin: str | None
    first_seen: datetime
    last_seen: datetime
    is_active: bool
    vehicle_id: int | None
    excluded_reason: str | None  # junk filter: write_off, wrecking, parts_only, rental, low_price, no_price
    valuation: ValuationOut | None = None
    same_car: list[OtherListing] = []


class PricePoint(Orm):
    price: int | None
    price_type: str
    observed_at: datetime


class CarPricePoint(PricePoint):
    source: str


class ListingDetailOut(ListingOut):
    description: str | None
    price_history: list[PricePoint]
    car_price_history: list[CarPricePoint] = []  # every listing of this car, newest first
    match_method: str | None = None  # vin | rego | fuzzy | single
    match_confidence: float | None = None


class ListingPage(BaseModel):
    total: int
    page: int
    per_page: int
    items: list[ListingOut]


class SearchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    params: dict[str, Any] = Field(default_factory=dict)
    sources: list[str] | None = None
    schedule_enabled: bool = True


class SearchOut(Orm):
    id: int
    name: str
    params: dict[str, Any]
    sources: list[str] | None
    schedule_enabled: bool
    created_at: datetime


class RunOut(Orm):
    id: int
    saved_search_id: int | None
    source: str
    trigger: str
    status: str
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    found: int
    new: int
    updated: int
    requests: int
    error: str | None


class SourcePatch(BaseModel):
    enabled: bool


@router.get("/listings", response_model=ListingPage)
def list_listings(db: DB, f: Annotated[ListingFilters, Query()]) -> Any:
    items, total = query_listings(db, f)
    others = same_car(db, items)
    return ListingPage(total=total, page=f.page, per_page=f.per_page, items=[
        ListingOut.model_validate(i).model_copy(update={
            "same_car": [OtherListing.model_validate(o) for o in others.get(i.id, [])]})
        for i in items])


@router.get("/listings/{listing_id}", response_model=ListingDetailOut)
def get_listing(listing_id: int, db: DB) -> Any:
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404, "listing not found")
    vehicle = db.get(Vehicle, listing.vehicle_id) if listing.vehicle_id else None
    out = ListingDetailOut.model_validate(listing)
    return out.model_copy(update={
        "same_car": [OtherListing.model_validate(o) for o in same_car(db, [listing]).get(listing.id, [])],
        "car_price_history": [CarPricePoint(price=p.price, price_type=p.price_type,
                                            observed_at=p.observed_at, source=src)
                              for p, src in car_price_history(db, listing)],
        "match_method": vehicle.match_method if vehicle else None,
        "match_confidence": vehicle.confidence if vehicle else None,
    })


@router.get("/searches", response_model=list[SearchOut])
def list_searches(db: DB) -> Any:
    return list(db.scalars(select(SavedSearch).order_by(SavedSearch.id)))


def _validate_sources(names: list[str] | None) -> list[str] | None:
    if not names:
        return None
    unknown = set(names) - set(load_sources())
    if unknown:
        raise HTTPException(422, f"unknown sources: {sorted(unknown)}")
    return names


@router.post("/searches", response_model=SearchOut, status_code=201)
def create_search(body: SearchIn, db: DB) -> Any:
    search = SavedSearch(
        name=body.name,
        params=SearchParams.from_dict(body.params).to_dict(),
        sources=_validate_sources(body.sources),
        schedule_enabled=body.schedule_enabled,
    )
    db.add(search)
    db.commit()
    return search


@router.delete("/searches/{search_id}", status_code=204)
def delete_search(search_id: int, db: DB) -> None:
    search = db.get(SavedSearch, search_id)
    if not search:
        raise HTTPException(404, "search not found")
    db.delete(search)
    db.commit()


@router.post("/searches/{search_id}/run", response_model=list[RunOut], status_code=202)
def run_search(search_id: int, db: DB) -> Any:
    search = db.get(SavedSearch, search_id)
    if not search:
        raise HTTPException(404, "search not found")
    runs = enqueue_search(db, search, trigger="manual")
    db.commit()
    return runs


@router.get("/runs", response_model=list[RunOut])
def list_runs(db: DB, limit: int = Query(50, ge=1, le=500), source: str | None = None) -> Any:
    stmt = select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(limit)
    if source:
        stmt = stmt.where(ScrapeRun.source == source)
    return list(db.scalars(stmt))


@router.get("/sources")
def list_sources(db: DB) -> Any:
    nxt = next_scheduled_run()
    return {"next_scheduled_run": nxt.isoformat() if nxt else None,
            "sources": [v.__dict__ for v in source_views(db)]}


@router.patch("/sources/{name}")
def patch_source(name: str, body: SourcePatch, db: DB) -> Any:
    views = {v.name: v for v in source_views(db)}
    if name not in views:
        raise HTTPException(404, "unknown source")
    if not views[name].toggle_allowed:
        raise HTTPException(409, f"{name} is {views[name].state_label.lower()}; change sources.yaml")
    row = db.get(Source, name)
    row.enabled = body.enabled
    db.commit()
    return {v.name: v for v in source_views(db)}[name].__dict__
