"""Apply saved-search criteria to a parsed listing, for sources that can't filter server-side
(or where server-side filtering is off-limits by robots.txt, like Cars24's `?f=`).

Lenient by design: a field the listing doesn't have (None) never excludes it, so a plugin can
pre-filter on a partial parse (search card only) and filter again after fetching details.
"""
from __future__ import annotations

from carscout.normalise.mapping import normalise_make, normalise_model, normalise_spec, same_name
from carscout.sources.base import NormalisedListing, SearchParams


def matches(params: SearchParams, item: NormalisedListing) -> bool:
    make = normalise_make(params.make)
    if make and item.make and not same_name(make, item.make):
        return False
    model = normalise_model(make, params.model)
    if model and item.model and not same_name(model, item.model):
        return False
    if params.variant and item.variant and params.variant.lower() not in item.variant.lower():
        return False
    if item.year is not None:
        if params.year_min and item.year < params.year_min:
            return False
        if params.year_max and item.year > params.year_max:
            return False
    if params.km_max and item.odometer_km is not None and item.odometer_km > params.km_max:
        return False
    if item.price is not None:
        if params.price_min and item.price < params.price_min:
            return False
        if params.price_max and item.price > params.price_max:
            return False
    if params.states and item.state and item.state not in params.states:
        return False
    for fld in ("body_type", "transmission"):
        wanted = normalise_spec(fld, getattr(params, fld))
        have = getattr(item, fld)
        if wanted and have and not same_name(wanted, have):
            return False
    return True
