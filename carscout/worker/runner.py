"""Executes one scrape run (one saved search on one source) and stores the results.

Isolation rule: whatever a plugin does, this function records the outcome on the run and the
source row and returns normally. One broken or blocked source never stops the others.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from carscout.core.config import Settings, get_settings, load_sources
from carscout.core.logging import get_logger
from carscout.models import (
    Listing,
    PriceHistory,
    RawPayload,
    RunStatus,
    ScrapeRun,
    Source,
    SourceStatus,
)
from carscout.sources.base import (
    NormalisedListing,
    RawListing,
    SearchParams,
    SourceError,
    SourcePlugin,
    load_plugin,
)
from carscout.sources.http import PoliteClient
from carscout.worker.queue import is_runnable

log = get_logger(__name__)

_LISTING_FIELDS = [f for f in NormalisedListing.__dataclass_fields__ if f != "source_listing_id"]


def store_listing(session: Session, plugin: SourcePlugin, raw: RawListing, now: datetime) -> str:
    """Upsert one listing. Returns 'new', 'updated' or 'unchanged'."""
    listing = session.scalars(
        select(Listing).where(
            Listing.source == plugin.name, Listing.source_listing_id == raw.source_listing_id
        )
    ).first()

    # Keep the raw payload, unless it's identical to the one we already have.
    latest = session.get(RawPayload, listing.latest_raw_id) if listing and listing.latest_raw_id else None
    is_json = raw.content_type == "json"
    same = latest is not None and (
        (latest.payload_json if is_json else latest.payload_text) == raw.payload
    )
    if not same:
        latest = RawPayload(
            source=plugin.name,
            source_listing_id=raw.source_listing_id,
            url=raw.url,
            content_type=raw.content_type,
            payload_json=raw.payload if is_json else None,
            payload_text=None if is_json else raw.payload,
            parser_version=plugin.parser_version,
        )
        session.add(latest)
        session.flush()

    parsed = asdict(plugin.parse(raw))
    values = {k: parsed[k] for k in _LISTING_FIELDS}

    if listing is None:
        listing = Listing(
            source=plugin.name,
            source_listing_id=raw.source_listing_id,
            first_seen=now,
            last_seen=now,
            is_active=True,
            latest_raw_id=latest.id,
            **values,
        )
        session.add(listing)
        session.flush()
        session.add(PriceHistory(listing_id=listing.id, price=listing.price,
                                 price_type=listing.price_type, observed_at=now))
        return "new"

    price_changed = (listing.price, listing.price_type) != (values["price"], values["price_type"])
    changed = any(getattr(listing, k) != v for k, v in values.items()) or not listing.is_active
    for k, v in values.items():
        setattr(listing, k, v)
    listing.last_seen = now
    listing.is_active = True
    listing.latest_raw_id = latest.id
    if price_changed:
        session.add(PriceHistory(listing_id=listing.id, price=listing.price,
                                 price_type=listing.price_type, observed_at=now))
    return "updated" if changed else "unchanged"


def make_client(plugin: SourcePlugin, settings: Settings) -> PoliteClient:
    c = plugin.config
    return PoliteClient(
        plugin.name,
        delay=c.delay,
        jitter=c.jitter,
        concurrency=c.concurrency,
        max_retries=settings.max_retries,
        backoff_base=settings.backoff_base_seconds,
        respect_robots=settings.respect_robots,
        user_agent=settings.user_agent,
    )


async def execute_run(
    sm: sessionmaker[Session],
    run_id: int,
    settings: Settings | None = None,
    plugin: SourcePlugin | None = None,
) -> str:
    """Run a claimed scrape_runs row to completion. Never raises (except on DB failure)."""
    settings = settings or get_settings()
    with sm() as s:
        run = s.get(ScrapeRun, run_id)
        assert run is not None, run_id
        source_name = run.source
        params = SearchParams.from_dict(run.saved_search.params if run.saved_search else {})
        source_row = s.get(Source, source_name)
        cfg = load_sources(settings).get(source_name)
        runnable = plugin is not None or is_runnable(cfg, source_row)

    bound = log.bind(run_id=run_id, source=source_name)
    status, error = RunStatus.OK, None
    found = new = updated = 0
    http: PoliteClient | None = None

    try:
        if not runnable:
            raise SourceError("source is disabled or not implemented")
        plugin = plugin or load_plugin(cfg)  # type: ignore[arg-type]
        http = make_client(plugin, settings)
        bound.info("run_started", params=params.to_dict())
        async for raw in plugin.search(params, http):
            with sm.begin() as s:
                result = store_listing(s, plugin, raw, datetime.now(UTC))
            found += 1
            new += result == "new"
            updated += result == "updated"
            if found >= params.max_results:
                bound.info("max_results_reached", max_results=params.max_results)
                break
    except SourceError as e:
        status, error = RunStatus.DEGRADED, str(e)
        bound.warning("source_degraded", error=error)
    except Exception as e:  # a plugin bug must not take the worker down
        status, error = RunStatus.FAILED, f"{type(e).__name__}: {e}"
        bound.exception("run_failed")
    finally:
        requests = http.requests if http else 0
        if http:
            await http.aclose()

    now = datetime.now(UTC)
    with sm.begin() as s:
        run = s.get(ScrapeRun, run_id)
        run.status, run.error, run.finished_at = status, error, now
        run.found, run.new, run.updated, run.requests = found, new, updated, requests
        src = s.get(Source, source_name)
        if src is not None and runnable:
            src.last_run_at = now
            src.last_count = found
            src.last_error = error
            src.status = SourceStatus.OK if status == RunStatus.OK else SourceStatus.DEGRADED
            if status == RunStatus.OK:
                src.last_ok_at = now
    bound.info("run_finished", status=status, found=found, new=new, updated=updated,
               requests=requests, error=error)
    return status
