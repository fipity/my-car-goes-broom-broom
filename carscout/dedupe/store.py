"""Write dedupe results to the database: every listing belongs to exactly one `vehicles` row.

Recomputed per make/model block (cheap: candidate pairs come from an odometer sweep), after each
scrape run for the blocks it touched, or for everything with `python -m carscout.cli dedupe`.
Vehicle ids stay stable across recomputes where possible, so links to a car keep working.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from carscout.core.logging import get_logger
from carscout.dedupe.match import Car, MatchConfig, cluster
from carscout.models import Listing, Vehicle

log = get_logger(__name__)
_LOCK_KEY = 0x0CA25C07  # pg advisory lock id: one dedupe at a time


def _block_key(make: str | None, model: str | None) -> tuple[str, str]:
    return ((make or "").lower(), (model or "").lower())


def _car(l: Listing) -> Car:
    return Car(id=l.id, source=l.source, make=l.make, model=l.model, variant=l.variant, year=l.year,
               odometer_km=l.odometer_km, price=l.price, state=l.state, body_type=l.body_type,
               transmission=l.transmission, fuel_type=l.fuel_type, drivetrain=l.drivetrain,
               colour=l.colour, vin=l.vin, rego=l.rego, is_active=l.is_active, last_seen=l.last_seen)


def dedupe(session: Session, blocks: Iterable[tuple[str | None, str | None]] | None = None,
           cfg: MatchConfig = MatchConfig()) -> dict[str, int]:
    """Recompute vehicles for the given (make, model) blocks, or all listings when None.
    Returns counts: listings, vehicles, multi (vehicles with more than one listing)."""
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    stmt = select(Listing)
    if blocks is not None:
        keys = {_block_key(*b) for b in blocks}
        if not keys:
            return {"listings": 0, "vehicles": 0, "multi": 0}
        makes = {k[0] for k in keys}
        stmt = stmt.where(func.lower(func.coalesce(Listing.make, "")).in_(makes))
    listings = list(session.scalars(stmt))
    by_block: dict[tuple[str, str], list[Listing]] = {}
    for l in listings:
        k = _block_key(l.make, l.model)
        if blocks is None or k in keys:
            by_block.setdefault(k, []).append(l)

    claimed: set[int] = set()
    stats = Counter()
    for block in by_block.values():
        by_id = {l.id: l for l in block}
        for cl in cluster([_car(l) for l in block], cfg):
            members = [by_id[c.id] for c in cl.cars]
            # Reuse the vehicle id most members already have, if no other cluster took it.
            current = Counter(m.vehicle_id for m in members if m.vehicle_id is not None)
            vid = next((v for v, _ in current.most_common() if v not in claimed), None)
            vehicle = session.get(Vehicle, vid) if vid is not None else None
            if vehicle is None:
                vehicle = Vehicle()
                session.add(vehicle)
                session.flush()
            claimed.add(vehicle.id)
            vehicle.vin, vehicle.rego = cl.vin, cl.rego
            vehicle.canonical_listing_id = cl.canonical().id
            vehicle.match_method, vehicle.confidence = cl.method, cl.confidence
            for m in members:
                m.vehicle_id = vehicle.id
            stats["vehicles"] += 1
            stats["listings"] += len(members)
            stats["multi"] += len(members) > 1
    session.flush()
    # Vehicles nothing points at any more (their listings joined another car).
    session.execute(delete(Vehicle).where(~select(Listing.id).where(Listing.vehicle_id == Vehicle.id).exists()))
    log.info("dedupe_done", blocks="all" if blocks is None else len(by_block), **stats)
    return dict(stats)
