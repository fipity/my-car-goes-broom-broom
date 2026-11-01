"""Australian postcode/suburb coordinates (GeoNames, CC BY 4.0) for radius search.

Loaded into the `postcodes` table on first start (api entrypoint) or with `cli load-postcodes`.
Listings get coordinates from the source when it gives them (Cars24) or from suburb + state.
Carsales only gives a state, so its listings have no coordinates; the radius filter treats them
as "somewhere in that state" (see web/queries.py).
"""
from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

from sqlalchemy import ColumnElement, func, insert, select
from sqlalchemy.orm import Session

from carscout.models import Postcode

DATA = Path(__file__).parent / "data" / "au_postcodes.tsv.gz"
EARTH_KM = 6371.0


def load_postcodes(session: Session, force: bool = False) -> int:
    """Fill the postcodes table if it's empty (or always, with force). Returns rows loaded."""
    if not force and session.scalar(select(func.count(Postcode.id))):
        return 0
    if force:
        session.query(Postcode).delete()
    rows = []
    with gzip.open(DATA, "rt", encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            pc, suburb, state, lat, lon = line.rstrip("\n").split("\t")
            rows.append({"postcode": pc, "suburb": suburb, "state": state, "lat": float(lat), "lon": float(lon)})
    for i in range(0, len(rows), 5000):
        session.execute(insert(Postcode), rows[i:i + 5000])
    return len(rows)


def locate_suburb(session: Session, suburb: str | None, state: str | None) -> tuple[str, float, float] | None:
    """(postcode, lat, lon) for a suburb in a state, or None."""
    if not suburb or not state:
        return None
    row = session.execute(
        select(Postcode.postcode, Postcode.lat, Postcode.lon)
        .where(func.lower(Postcode.suburb) == suburb.strip().lower(), Postcode.state == state)
        .order_by(Postcode.postcode).limit(1)
    ).first()
    return (row[0], row[1], row[2]) if row else None


def postcode_centre(session: Session, postcode: str) -> tuple[float, float, str] | None:
    """(lat, lon, state) at the middle of a postcode's suburbs, or None if unknown."""
    row = session.execute(
        select(func.avg(Postcode.lat), func.avg(Postcode.lon), func.min(Postcode.state))
        .where(Postcode.postcode == postcode.strip())
    ).first()
    return (row[0], row[1], row[2]) if row and row[0] is not None else None


def distance_km(lat_col: Any, lon_col: Any, lat: float, lon: float) -> ColumnElement[float]:
    """Great-circle distance in SQL (haversine), no PostGIS needed."""
    dlat = func.radians(lat_col - lat) / 2
    dlon = func.radians(lon_col - lon) / 2
    a = func.power(func.sin(dlat), 2) + func.cos(func.radians(lat)) * func.cos(func.radians(lat_col)) * func.power(func.sin(dlon), 2)
    return 2 * EARTH_KM * func.asin(func.sqrt(func.least(1.0, a)))
