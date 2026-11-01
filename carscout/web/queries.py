"""Queries shared by the JSON API and the HTML pages."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field
from sqlalchemy import Select, and_, case, delete, func, or_, select
from sqlalchemy.orm import Session

from carscout.core.config import get_settings, load_sources, synthetic_sources
from carscout.geo.postcodes import distance_km, postcode_centre
from carscout.models import (
    Listing,
    PriceHistory,
    RunStatus,
    SavedSearch,
    ScrapeRun,
    SearchListing,
    Source,
    Valuation,
    Vehicle,
)
from carscout.worker.queue import sync_sources

SORTS = {
    "newest": (Listing.first_seen.desc(),),
    "price_asc": (Listing.price.asc().nulls_last(),),
    "price_desc": (Listing.price.desc().nulls_last(),),
    "km_asc": (Listing.odometer_km.asc().nulls_last(),),
    "year_desc": (Listing.year.desc().nulls_last(),),
    "deal": (Valuation.diff_pct.asc().nulls_last(),),
}


class ListingFilters(BaseModel):
    q: str | None = None
    make: str | None = None
    model: str | None = None
    variant: str | None = None
    year_min: int | None = None
    year_max: int | None = None
    price_min: int | None = None
    price_max: int | None = None
    km_min: int | None = None
    km_max: int | None = None
    state: str | None = None
    postcode: str | None = None
    radius_km: int | None = None
    body_type: str | None = None
    transmission: str | None = None
    fuel_type: str | None = None
    seller_type: str | None = None
    source: str | None = None
    deal: list[str] = Field(default_factory=list)  # deal labels, any of
    include_inactive: bool = False
    sort: str = "newest"
    page: int = Field(1, ge=1)
    per_page: int = Field(24, ge=1, le=200)

    @classmethod
    def from_query(cls, qp: dict[str, str]) -> ListingFilters:
        """Lenient parsing for HTML forms: empty fields and junk numbers are ignored."""
        clean: dict[str, Any] = {}
        for name, f in cls.model_fields.items():
            v = qp.get(name, "").strip()
            if not v:
                continue
            if f.annotation in (int, int | None):
                v = v.replace(",", "").replace("$", "")
                if not v.isdigit():
                    continue
            clean[name] = v
        if "include_inactive" in clean:
            clean["include_inactive"] = clean["include_inactive"] in ("1", "true", "on")
        getlist = getattr(qp, "getlist", None)
        deals = getlist("deal") if getlist else ([qp["deal"]] if qp.get("deal") else [])
        clean["deal"] = [d for d in deals if d]
        return cls(**clean)




_MAIN_FILTERS = {"q", "make", "model", "price_max", "state", "sort", "page", "per_page"}


def more_filters_set(f: ListingFilters) -> int:
    """How many of the filters under "More filters" are in use (0 keeps the panel closed)."""
    return sum(1 for k, v in f.model_dump().items() if k not in _MAIN_FILTERS and v not in (None, "", [], False))


Geo = tuple[float, float, str]  # centre lat, lon, state


def _apply(stmt: Select[Any], f: ListingFilters, geo: Geo | None = None) -> Select[Any]:
    if not f.include_inactive:
        stmt = stmt.where(Listing.is_active.is_(True))
    if f.q:
        stmt = stmt.where(Listing.title.ilike(f"%{f.q}%"))
    if f.make:
        stmt = stmt.where(Listing.make.ilike(f.make))
    if f.model:
        stmt = stmt.where(Listing.model.ilike(f.model))
    if f.year_min:
        stmt = stmt.where(Listing.year >= f.year_min)
    if f.year_max:
        stmt = stmt.where(Listing.year <= f.year_max)
    if f.price_min:
        stmt = stmt.where(Listing.price >= f.price_min)
    if f.price_max:
        stmt = stmt.where(Listing.price <= f.price_max)
    if f.km_max:
        stmt = stmt.where(Listing.odometer_km <= f.km_max)
    if f.km_min:
        stmt = stmt.where(Listing.odometer_km >= f.km_min)
    if f.variant:
        stmt = stmt.where(Listing.variant.ilike(f"%{f.variant}%"))
    for col, val in ((Listing.body_type, f.body_type), (Listing.transmission, f.transmission),
                     (Listing.fuel_type, f.fuel_type), (Listing.seller_type, f.seller_type)):
        if val:
            stmt = stmt.where(col.ilike(val))
    if f.deal:
        stmt = stmt.where(Listing.id.in_(select(Valuation.listing_id).where(Valuation.label.in_(f.deal))))
    if geo and f.radius_km:
        lat, lon, centre_state = geo
        # Listings with coordinates: by distance. Listings that only give a state (Carsales):
        # kept when they're in the postcode's state, since they might be close.
        stmt = stmt.where(or_(
            and_(Listing.lat.is_not(None), distance_km(Listing.lat, Listing.lon, lat, lon) <= f.radius_km),
            and_(Listing.lat.is_(None), Listing.state == centre_state),
        ))
    if f.state:
        stmt = stmt.where(Listing.state == f.state.upper())
    if f.source:
        stmt = stmt.where(Listing.source == f.source)
    else:
        fake = synthetic_sources()  # demo data only shows when its source is picked explicitly
        if fake:
            stmt = stmt.where(Listing.source.not_in(fake))
    return stmt


_CAR = func.coalesce(Listing.vehicle_id, -Listing.id)  # listings not deduped yet count alone


def query_listings(session: Session, f: ListingFilters) -> tuple[list[Listing], int]:
    """One row per car: of each car's listings that pass the filters, the canonical one if it
    does, else the freshest active one. Total counts cars, not listings."""
    geo = postcode_centre(session, f.postcode) if f.postcode and f.radius_km else None
    total = session.scalar(_apply(select(func.count(func.distinct(_CAR))), f, geo)) or 0
    ranked = (
        _apply(select(
            Listing.id.label("id"),
            func.row_number().over(
                partition_by=_CAR,
                order_by=(case((Listing.id == Vehicle.canonical_listing_id, 0), else_=1),
                          Listing.is_active.desc(), Listing.last_seen.desc(), Listing.id.desc()),
            ).label("rn"),
        ).outerjoin(Vehicle, Vehicle.id == Listing.vehicle_id), f, geo)
    ).subquery()
    if f.sort == "distance" and geo:
        order = (distance_km(Listing.lat, Listing.lon, geo[0], geo[1]).asc().nulls_last(),)
    else:
        order = SORTS.get(f.sort, SORTS["newest"])
    stmt = (select(Listing).join(ranked, ranked.c.id == Listing.id).where(ranked.c.rn == 1)
            .outerjoin(Valuation, Valuation.listing_id == Listing.id)
            .order_by(*order, Listing.id.desc()))
    items = list(session.scalars(stmt.limit(f.per_page).offset((f.page - 1) * f.per_page)))
    return items, total


def same_car(session: Session, listings: list[Listing]) -> dict[int, list[Listing]]:
    """listing id -> the *other* listings of the same car (any source, active first)."""
    vids = {l.vehicle_id for l in listings if l.vehicle_id is not None}
    if not vids:
        return {}
    rows = list(session.scalars(
        select(Listing).where(Listing.vehicle_id.in_(vids))
        .order_by(Listing.is_active.desc(), Listing.source, Listing.id)
    ))
    return {l.id: [o for o in rows if o.vehicle_id == l.vehicle_id and o.id != l.id] for l in listings}


@dataclass
class PriceDrop:
    listing: Listing
    was: int
    now: int
    at: datetime

    @property
    def amount(self) -> int:
        return self.was - self.now


@dataclass
class SearchChanges:
    since: datetime
    new: list[Listing]
    drops: list[PriceDrop]
    active: int


def _one_per_car(listings: list[Listing]) -> list[Listing]:
    seen: set[int] = set()
    out = []
    for l in listings:
        key = l.vehicle_id if l.vehicle_id is not None else -l.id
        if key not in seen:
            seen.add(key)
            out.append(l)
    return out


def search_changes(session: Session, search: SavedSearch) -> SearchChanges:
    """What's happened in a saved search since it was last marked seen (or created):
    listings new to CarScout, and price drops. Active listings only, one per car."""
    since = search.seen_at or search.created_at
    in_search = select(SearchListing.listing_id).where(SearchListing.saved_search_id == search.id)
    active = (select(Listing).where(Listing.id.in_(in_search), Listing.is_active.is_(True)))
    new = _one_per_car(list(session.scalars(
        active.where(Listing.first_seen > since).order_by(Listing.first_seen.desc()))))

    steps = select(
        PriceHistory.listing_id, PriceHistory.price, PriceHistory.observed_at,
        func.lag(PriceHistory.price).over(partition_by=PriceHistory.listing_id,
                                          order_by=PriceHistory.observed_at).label("prev"),
    ).where(PriceHistory.listing_id.in_(in_search)).subquery()
    was: dict[int, int] = {}        # price before the first change in the window
    last_change: dict[int, datetime] = {}
    for lid, _price, at, prev in session.execute(
        select(steps).where(steps.c.observed_at > since, steps.c.prev.is_not(None))
        .order_by(steps.c.observed_at)
    ):
        was.setdefault(lid, prev)
        last_change[lid] = at
    drops = [PriceDrop(l, was[l.id], l.price, last_change[l.id])
             for l in session.scalars(active.where(Listing.id.in_(was)))
             if l.price is not None and was[l.id] is not None and l.price < was[l.id]]
    drops.sort(key=lambda d: -d.amount)
    by_car: dict[int, PriceDrop] = {}
    for d in drops:  # biggest drop per car
        by_car.setdefault(d.listing.vehicle_id if d.listing.vehicle_id is not None else -d.listing.id, d)
    drops = list(by_car.values())
    total = session.scalar(select(func.count(func.distinct(_CAR))).where(Listing.id.in_(in_search),
                                                                          Listing.is_active.is_(True))) or 0
    return SearchChanges(since=since, new=new, drops=drops, active=total)


def search_listings(session: Session, search: SavedSearch, sort: str = "deal") -> list[Listing]:
    """Everything a saved search currently matches (active, one per car)."""
    in_search = select(SearchListing.listing_id).where(SearchListing.saved_search_id == search.id)
    order = SORTS.get(sort, SORTS["deal"])
    return _one_per_car(list(session.scalars(
        select(Listing).where(Listing.id.in_(in_search), Listing.is_active.is_(True))
        .outerjoin(Valuation, Valuation.listing_id == Listing.id).order_by(*order, Listing.id.desc()))))


def comparables(session: Session, listing: Listing) -> list[Listing]:
    """The listings the valuation compared against, closest first."""
    ids = listing.valuation.comp_ids if listing.valuation else []
    if not ids:
        return []
    found = {l.id: l for l in session.scalars(select(Listing).where(Listing.id.in_(ids)))}
    return [found[i] for i in ids if i in found]


def car_price_history(session: Session, listing: Listing) -> list[tuple[PriceHistory, str]]:
    """Price changes of every listing of this car, newest first, with the source of each."""
    ids = [listing.id] + [o.id for o in same_car(session, [listing]).get(listing.id, [])]
    rows = session.execute(
        select(PriceHistory, Listing.source).join(Listing, Listing.id == PriceHistory.listing_id)
        .where(PriceHistory.listing_id.in_(ids)).order_by(PriceHistory.observed_at.desc())
    ).all()
    return [(p, src) for p, src in rows]


def facet_values(session: Session) -> dict[str, list[str]]:
    def distinct(col: Any) -> list[str]:
        return [v for v in session.scalars(select(col).distinct().where(col.is_not(None)).order_by(col))]

    return {"makes": distinct(Listing.make), "sources": distinct(Listing.source),
            "bodies": distinct(Listing.body_type), "transmissions": distinct(Listing.transmission),
            "fuels": distinct(Listing.fuel_type),
            "deals": ["Great deal", "Good deal", "Fair", "Above market", "Overpriced"],
            "states": ["ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"]}


@dataclass
class SourceView:
    name: str
    label: str
    state: str          # ok | degraded | never_run | off | disabled_config | not_built
    state_label: str
    toggle_allowed: bool
    enabled: bool
    last_run_at: datetime | None
    last_ok_at: datetime | None
    last_count: int | None
    last_error: str | None
    use_browser: bool
    queued: int
    note: str | None = None
    active_listings: int = 0
    recent_runs: list[ScrapeRun] = field(default_factory=list)


def source_views(session: Session) -> list[SourceView]:
    configs = load_sources()
    sync_sources(session, configs)
    session.commit()
    rows = {s.name: s for s in session.scalars(select(Source))}
    queued = dict(
        session.execute(
            select(ScrapeRun.source, func.count())
            .where(ScrapeRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
            .group_by(ScrapeRun.source)
        ).all()
    )
    active = dict(session.execute(
        select(Listing.source, func.count()).where(Listing.is_active.is_(True)).group_by(Listing.source)).all())
    recent: dict[str, list[ScrapeRun]] = {}
    for run in session.scalars(select(ScrapeRun).where(ScrapeRun.finished_at.is_not(None))
                               .order_by(ScrapeRun.id.desc()).limit(200)):
        if len(recent.setdefault(run.source, [])) < 8:
            recent[run.source].append(run)
    out = []
    for name, cfg in configs.items():
        row = rows[name]
        if not cfg.implemented and cfg.unavailable:
            state, label = "not_built", "Unavailable"
        elif not cfg.implemented:
            state, label = "not_built", "Not built yet"
        elif not cfg.enabled:
            state, label = "disabled_config", "Disabled in config"
        elif not row.enabled:
            state, label = "off", "Off"
        else:
            state = row.status
            label = {"ok": "OK", "degraded": "Degraded", "never_run": "Not run yet"}.get(state, state)
        out.append(SourceView(
            name=name, label=cfg.label, state=state, state_label=label,
            toggle_allowed=cfg.implemented and cfg.enabled, enabled=row.enabled,
            last_run_at=row.last_run_at, last_ok_at=row.last_ok_at, last_count=row.last_count,
            last_error=row.last_error, use_browser=cfg.use_browser, queued=queued.get(name, 0),
            note=cfg.unavailable, active_listings=active.get(name, 0),
            recent_runs=recent.get(name, []),
        ))
    return out


def next_scheduled_run() -> datetime | None:
    s = get_settings()
    trigger = CronTrigger.from_crontab(s.scrape_cron, timezone=s.schedule_tz)
    return trigger.get_next_fire_time(None, datetime.now(UTC).astimezone(trigger.timezone))


def update_search(session: Session, search: SavedSearch, **fields: Any) -> bool:
    """Apply edits. Returns True when the criteria (params or sources) changed; then the search's
    tracked listings are cleared, since "no longer returned" would otherwise be misread as sold."""
    criteria_changed = (fields.get("params", search.params) != search.params
                        or fields.get("sources", search.sources) != search.sources)
    for k, v in fields.items():
        setattr(search, k, v)
    if criteria_changed:
        session.execute(delete(SearchListing).where(SearchListing.saved_search_id == search.id))
    return criteria_changed
