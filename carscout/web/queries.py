"""Queries shared by the JSON API and the HTML pages."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from carscout.core.config import get_settings, load_sources
from carscout.models import Listing, RunStatus, ScrapeRun, Source
from carscout.worker.queue import sync_sources

SORTS = {
    "newest": (Listing.first_seen.desc(),),
    "price_asc": (Listing.price.asc().nulls_last(),),
    "price_desc": (Listing.price.desc().nulls_last(),),
    "km_asc": (Listing.odometer_km.asc().nulls_last(),),
    "year_desc": (Listing.year.desc().nulls_last(),),
}


class ListingFilters(BaseModel):
    q: str | None = None
    make: str | None = None
    model: str | None = None
    year_min: int | None = None
    year_max: int | None = None
    price_min: int | None = None
    price_max: int | None = None
    km_max: int | None = None
    state: str | None = None
    source: str | None = None
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
        return cls(**clean)


def _apply(stmt: Select[Any], f: ListingFilters) -> Select[Any]:
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
    if f.state:
        stmt = stmt.where(Listing.state == f.state.upper())
    if f.source:
        stmt = stmt.where(Listing.source == f.source)
    return stmt


def query_listings(session: Session, f: ListingFilters) -> tuple[list[Listing], int]:
    total = session.scalar(_apply(select(func.count(Listing.id)), f)) or 0
    order = SORTS.get(f.sort, SORTS["newest"])
    stmt = _apply(select(Listing), f).order_by(*order, Listing.id.desc())
    items = list(session.scalars(stmt.limit(f.per_page).offset((f.page - 1) * f.per_page)))
    return items, total


def facet_values(session: Session) -> dict[str, list[str]]:
    def distinct(col: Any) -> list[str]:
        return [v for v in session.scalars(select(col).distinct().where(col.is_not(None)).order_by(col))]

    return {"makes": distinct(Listing.make), "sources": distinct(Listing.source),
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
            note=cfg.unavailable,
        ))
    return out


def next_scheduled_run() -> datetime | None:
    s = get_settings()
    trigger = CronTrigger.from_crontab(s.scrape_cron, timezone=s.schedule_tz)
    return trigger.get_next_fire_time(None, datetime.now(UTC).astimezone(trigger.timezone))
