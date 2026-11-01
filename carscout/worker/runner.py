"""Executes one scrape run (one saved search on one source) and stores the results.

Isolation rule: whatever a plugin does, this function records the outcome on the run and the
source row and returns normally. One broken or blocked source never stops the others.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

from typing import Any

from sqlalchemy import String, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from carscout.core.config import Settings, get_settings, load_sources
from carscout.core.logging import get_logger
from carscout.dedupe.store import dedupe
from carscout.valuation.store import revalue
from carscout.models import (
    Listing,
    PriceHistory,
    RawPayload,
    RunStatus,
    ScrapeRun,
    SearchListing,
    Source,
    SourceStatus,
)
from carscout.sources.base import (
    KnownListing,
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
_MAX_LEN = {
    c.name: c.type.length
    for c in Listing.__table__.columns
    if isinstance(c.type, String) and c.type.length and c.name in _LISTING_FIELDS
}
GONE_CHECKS_PER_RUN = 50  # cap on confirm_gone requests per run


def _fit(values: dict[str, Any]) -> dict[str, Any]:
    """Trim strings to their column length, so one long source value can't fail the insert."""
    for k, n in _MAX_LEN.items():
        v = values.get(k)
        if isinstance(v, str) and len(v) > n:
            values[k] = v[:n]
    return values


def latest_payload(sm: sessionmaker[Session], source: str, source_listing_id: str) -> Any | None:
    with sm() as s:
        return s.scalar(
            select(RawPayload.payload_json)
            .join(Listing, Listing.latest_raw_id == RawPayload.id)
            .where(Listing.source == source, Listing.source_listing_id == source_listing_id)
        )


def link_search(session: Session, search_id: int, listing_id: int, run_id: int, now: datetime) -> None:
    stmt = pg_insert(SearchListing).values(
        saved_search_id=search_id, listing_id=listing_id, first_seen=now, last_seen=now,
        last_run_id=run_id,
    )
    session.execute(stmt.on_conflict_do_update(
        index_elements=["saved_search_id", "listing_id"],
        set_={"last_seen": now, "last_run_id": run_id},
    ))


async def mark_disappeared(
    sm: sessionmaker[Session], plugin: SourcePlugin, http: PoliteClient,
    search_id: int, run_id: int, run_started: datetime,
) -> int:
    """After a complete run: listings this search found before but not now, and that no other
    run has seen since this one started, are checked with the plugin and marked inactive if gone."""
    with sm() as s:
        missing = s.execute(
            select(Listing.id, Listing.source_listing_id, Listing.url)
            .join(SearchListing, SearchListing.listing_id == Listing.id)
            .where(
                SearchListing.saved_search_id == search_id,
                Listing.source == plugin.name,
                Listing.is_active.is_(True),
                or_(SearchListing.last_run_id.is_(None), SearchListing.last_run_id != run_id),
                Listing.last_seen < run_started,
            )
            .order_by(Listing.last_seen)
            .limit(GONE_CHECKS_PER_RUN)
        ).all()
    gone = 0
    for listing_id, source_listing_id, url in missing:
        try:
            verdict = await plugin.confirm_gone(KnownListing(source_listing_id, url), http)
        except SourceError as e:
            log.warning("gone_check_stopped", source=plugin.name, error=str(e))
            break
        if verdict:
            with sm.begin() as s:
                s.get(Listing, listing_id).is_active = False
            gone += 1
    return gone


def store_listing(
    session: Session, plugin: SourcePlugin, raw: RawListing, now: datetime
) -> tuple[str, int]:
    """Upsert one listing. Returns ('new' | 'updated' | 'unchanged', listing id)."""
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
    values = _fit({k: parsed[k] for k in _LISTING_FIELDS})

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
        return "new", listing.id

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
    return ("updated" if changed else "unchanged"), listing.id


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
        search_id = run.saved_search_id
        params = SearchParams.from_dict(run.saved_search.params if run.saved_search else {})
        source_row = s.get(Source, source_name)
        cfg = load_sources(settings).get(source_name)
        runnable = plugin is not None or is_runnable(cfg, source_row)

    bound = log.bind(run_id=run_id, source=source_name)
    status, error = RunStatus.OK, None
    found = new = updated = gone = 0
    truncated = False
    blocks: set[tuple[str | None, str | None]] = set()  # (make, model) touched, for dedupe
    http: PoliteClient | None = None
    run_started = datetime.now(UTC)

    try:
        if not runnable:
            raise SourceError("source is disabled or not implemented")
        plugin = plugin or load_plugin(cfg)  # type: ignore[arg-type]
        http = make_client(plugin, settings)
        plugin.previous = lambda sid, _p=plugin: latest_payload(sm, _p.name, sid)
        bound.info("run_started", params=params.to_dict())
        async for raw in plugin.search(params, http):
            now = datetime.now(UTC)
            with sm.begin() as s:
                result, listing_id = store_listing(s, plugin, raw, now)
                if search_id is not None:
                    link_search(s, search_id, listing_id, run_id, now)
                stored = s.get(Listing, listing_id)
                blocks.add((stored.make, stored.model))
            found += 1
            new += result == "new"
            updated += result == "updated"
            if found >= params.max_results:
                truncated = True
                bound.info("max_results_reached", max_results=params.max_results)
                break
        truncated = truncated or plugin.truncated
        if search_id is not None and not truncated:
            gone = await mark_disappeared(sm, plugin, http, search_id, run_id, run_started)
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
        run.gone = gone
        src = s.get(Source, source_name)
        if src is not None and runnable:
            src.last_run_at = now
            src.last_count = found
            src.last_error = error
            src.status = SourceStatus.OK if status == RunStatus.OK else SourceStatus.DEGRADED
            if status == RunStatus.OK:
                src.last_ok_at = now
    if blocks:
        # Post-processing for the makes/models this run touched. Trouble here must not turn a
        # good scrape into a failure; each step logs and the next run tries again.
        for step, fn in (("dedupe", dedupe), ("valuation", revalue)):
            try:
                with sm.begin() as s:
                    fn(s, blocks)
            except Exception:
                bound.exception(f"{step}_failed")
    bound.info("run_finished", status=status, found=found, new=new, updated=updated,
               gone=gone, truncated=truncated, requests=requests, error=error)
    return status
