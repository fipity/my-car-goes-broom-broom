"""Compute and save valuations. Runs per make/model block after each scrape (after dedupe), or for
everything with `python -m carscout.cli revalue`."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from carscout.core.config import synthetic_sources
from carscout.core.logging import get_logger
from carscout.models import Listing, Valuation, Vehicle
from carscout.valuation.config import ValuationConfig, load_valuation_config
from carscout.valuation.junk import junk_reason
from carscout.valuation.model import Item, fit_group, value

log = get_logger(__name__)
_LOCK_KEY = 0x0CA25C08


def _item(l: Listing) -> Item:
    return Item(id=l.id, vehicle_id=l.vehicle_id, make=l.make, model=l.model, variant=l.variant,
                year=l.year, odometer_km=l.odometer_km, price=l.price, price_type=l.price_type,
                state=l.state, transmission=l.transmission, body_type=l.body_type,
                seller_type=l.seller_type, is_active=l.is_active, last_seen=l.last_seen)


def _key(make: str | None, model: str | None) -> tuple[str, str]:
    return ((make or "").lower(), (model or "").lower())


def revalue(session: Session, blocks: Iterable[tuple[str | None, str | None]] | None = None,
            cfg: ValuationConfig | None = None, now: datetime | None = None) -> dict[str, int]:
    """Mark junk and (re)compute valuations for listings in the given (make, model) blocks, or all."""
    cfg = cfg or load_valuation_config()
    now = now or datetime.now(UTC)
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    stmt = select(Listing)
    keys = None
    if blocks is not None:
        keys = {_key(*b) for b in blocks}
        if not keys:
            return {}
        stmt = stmt.where(func.lower(func.coalesce(Listing.make, "")).in_({k[0] for k in keys}))
    canonical = dict(session.execute(select(Vehicle.id, Vehicle.canonical_listing_id)).all())

    by_block: dict[tuple[str, str], list[Listing]] = {}
    for l in session.scalars(stmt):
        k = _key(l.make, l.model)
        if keys is None or k in keys:
            by_block.setdefault(k, []).append(l)

    stats: Counter[str] = Counter()
    fake = synthetic_sources()  # demo data is valued, but never used as a comparable
    sold_cutoff = now - timedelta(days=cfg.sold_max_age_days)
    for (make, model), listings in by_block.items():
        for l in listings:
            l.excluded_reason = junk_reason(l.price, [l.title, l.variant, l.description], cfg.junk)
        usable = [l for l in listings if l.excluded_reason is None and make and model]

        # One comparable per physical car: its canonical listing if usable, else the freshest active.
        def car(l: Listing) -> int:
            return l.vehicle_id if l.vehicle_id is not None else -l.id

        recent = [l for l in usable if (l.is_active or l.last_seen >= sold_cutoff) and l.source not in fake]
        recent.sort(key=lambda l: (l.id != canonical.get(car(l)), not l.is_active, -l.last_seen.timestamp()))
        per_car: dict[int, Listing] = {}
        for l in recent:
            per_car.setdefault(car(l), l)
        pool = [_item(l) for l in per_car.values()]
        group = fit_group(pool, cfg, ref_year=now.year)

        rows = []
        for l in usable:
            r = value(_item(l), pool, cfg, group)
            rows.append(dict(listing_id=l.id, estimate=r.estimate, p25=r.p25, p75=r.p75,
                             diff_abs=r.diff_abs, diff_pct=r.diff_pct, label=r.label,
                             confidence=r.confidence, method=r.method, n_comps=r.n_comps,
                             comp_ids=r.comp_ids, details=r.details, computed_at=now))
            stats[r.method] += 1
        if rows:
            stmt_ins = pg_insert(Valuation).values(rows)
            session.execute(stmt_ins.on_conflict_do_update(
                index_elements=["listing_id"],
                set_={c: stmt_ins.excluded[c] for c in rows[0] if c != "listing_id"}))
        junk_ids = [l.id for l in listings if l.excluded_reason is not None]
        if junk_ids:
            session.execute(delete(Valuation).where(Valuation.listing_id.in_(junk_ids)))
        stats["excluded"] += len(junk_ids)
        stats["blocks"] += 1
        if group is not None:
            log.info("valuation_model", make=make, model=model, n=group.n, r2=round(group.r2, 3))
    session.flush()
    log.info("revalue_done", **stats)
    return dict(stats)
