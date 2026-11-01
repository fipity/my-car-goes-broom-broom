"""CarsGuide (carsguide.com.au). Full search results with rich listing data.

How it works (checked 2026-11-01):
- Search pages are paths: /buy-a-car/<make>/<model>[/<state>][/<years>][/<price>], paged with
  ?page=N, 26 listings a page. Years: "2015-2020", "over-2014" (2014 on), "under-2009";
  price: "under-$25000", "over-$50000". Segment order matters: a wrong order silently redirects
  to all cars, so the final URL is checked.
- robots.txt (Crawl-delay 1) disallows sorting, distance, dealer and keyword queries, and any
  /buy-a-car URL containing "_" (so ?year_from= style filters). Odometer has no path form; it's
  filtered here along with everything else (sources/filters.py).
- Listing data is in __NUXT_DATA__ (pinia.results): price with drive-away flag, private/dealer,
  suburb with coordinates, colour, series, seller comment.
- A removed listing's page redirects to /buy-a-car?removed=true.
- Autotrader (autotrader.com.au) runs on the same platform with the same stock, so it isn't a
  separate source.
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

from carscout.core.logging import get_logger
from carscout.normalise.mapping import (
    normalise_make,
    normalise_model,
    normalise_model_variant,
    normalise_spec,
    normalise_state,
)
from carscout.sources.base import (
    KnownListing,
    NormalisedListing,
    RawListing,
    SearchParams,
    SourceChanged,
    SourcePlugin,
    SourceUnavailable,
)
from carscout.sources.filters import matches
from carscout.sources.http import PoliteClient
from carscout.sources.nextjs import nuxt_data

log = get_logger(__name__)

BASE = "https://www.carsguide.com.au"
IMG = "https://images.autotrader.com.au/t_listing_main_cg/"
STATES = ("ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA")

# Fields that change with every request (search position, distance from the visitor, ad
# products) or duplicate others; dropped so an unchanged listing stores no new raw payload.
_DROP = {"dataLayer", "searchPosition", "distanceFromMe", "isTopAd", "isEnhanced", "package",
         "badges", "dealerLogo", "dealerPhone", "perfRating", "moveIsKombi", "products"}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _filter_list(results: dict[str, Any]) -> dict[str, str]:
    raw = ((results.get("odinEventData") or {}).get("filterList")) or ""
    return {k: v for k, v in re.findall(r"(\w+):([^|]*)", raw) if v != "null"}


class Page:
    def __init__(self, html: str, final_path: str):
        root = nuxt_data(html)
        results = ((root or {}).get("pinia") or {}).get("results") if isinstance(root, dict) else None
        if not isinstance(results, dict) or "listings" not in results:
            raise SourceChanged("CarsGuide page has no pinia.results data: the layout has changed")
        self.listings: list[dict[str, Any]] = [l for l in results.get("listings") or [] if isinstance(l, dict)]
        self.total = int(results.get("total") or 0)
        self.last_page = int(results.get("lastPage") or 1)
        self.filters = _filter_list(results)
        self.final_path = final_path


class Plugin(SourcePlugin):
    name = "carsguide"
    parser_version = 1

    # -- paths -----------------------------------------------------------------------------
    @staticmethod
    def year_segment(lo: int | None, hi: int | None) -> tuple[str | None, dict[str, str]]:
        """Path segment plus the filters CarsGuide should then report applying."""
        if lo and hi:
            return f"{lo}-{hi}", {"year_from": str(lo), "year_to": str(hi)}
        if lo:
            return f"over-{lo}", {"year_from": str(lo)}
        if hi:
            return f"under-{hi}", {"year_to": str(hi)}
        return None, {}

    @staticmethod
    def price_segment(hi: int | None) -> tuple[str | None, dict[str, str]]:
        return (f"under-${hi}", {"price_to": str(hi)}) if hi else (None, {})

    def paths(self, params: SearchParams, model_slug: str | None, narrow: bool = True
              ) -> list[tuple[str, dict[str, str]]]:
        """[(path, expected filters)], one per state (or one national)."""
        make = normalise_make(params.make)
        parts = ["buy-a-car"] + ([slug(make)] if make else []) + ([model_slug] if make and model_slug else [])
        tail, expect = [], {}
        if narrow:
            for seg, exp in (self.year_segment(params.year_min, params.year_max),
                             self.price_segment(params.price_max)):
                if seg:
                    tail.append(seg)
                    expect |= exp
        states = [s for s in params.states if s in STATES] or [None]
        return [("/" + "/".join(parts + ([s.lower()] if s else []) + tail), expect) for s in states]

    async def _page(self, http: PoliteClient, path: str, page: int = 1) -> Page | None:
        url = BASE + path + (f"?page={page}" if page > 1 else "")
        resp = await http.get(url)
        if resp.status_code in (404, 410):
            return None
        if resp.status_code != 200:
            raise SourceUnavailable(f"HTTP {resp.status_code} for {path}")
        return Page(resp.text, urlsplit(str(resp.url)).path)

    async def _first(self, http: PoliteClient, path: str, expect: dict[str, str]) -> Page | None:
        """Page 1 of a path, or None if CarsGuide didn't take the path as given (redirected
        elsewhere, or applied filters other than the ones we asked for)."""
        page = await self._page(http, path)
        if page is None or page.final_path.rstrip("/") != path.rstrip("/"):
            log.info("carsguide_path_not_taken", path=path, landed=page.final_path if page else None)
            return None
        wrong = {k: v for k, v in expect.items() if page.filters.get(k) != v}
        if wrong:
            log.info("carsguide_filters_differ", path=path, expected=wrong,
                     got={k: page.filters.get(k) for k in wrong})
            return None
        return page

    # -- search ----------------------------------------------------------------------------
    async def search(self, params: SearchParams, http: PoliteClient) -> AsyncIterator[RawListing]:
        budget = int(self.config.options.get("max_pages", 20))
        make = normalise_make(params.make)
        model = normalise_model(make, params.model)
        model_slugs = [slug(model)] if model else [None]
        if model and "-" in slug(model):
            model_slugs.append(slug(model).replace("-", ""))

        # Find path forms CarsGuide accepts: narrowed by year/price if possible, else plainer.
        plan: list[tuple[str, Page]] = []
        for model_slug in model_slugs:
            for narrow in (True, False):
                paths = self.paths(params, model_slug, narrow)
                first = await self._first(http, *paths[0])
                budget -= 1
                if first is not None:
                    plan.append((paths[0][0], first))
                    for path, expect in paths[1:]:  # the other states, same form
                        page = await self._first(http, path, expect)
                        budget -= 1
                        if page is not None:
                            plan.append((path, page))
                    break
                if not (params.year_min or params.year_max or params.price_max):
                    break  # nothing to un-narrow
            if plan:
                break
        if not plan:
            raise SourceUnavailable(
                f"CarsGuide has no page for {make or ''} {model or ''} "
                f"({self.paths(params, model_slugs[0], False)[0][0]}). Check the make/model spelling.")

        seen: set[str] = set()
        for path, first in plan:
            log.info("carsguide_search", path=path, total=first.total, pages=first.last_page)
            page, n = first, 1
            while True:
                for raw in self._emit(page, params, seen):
                    yield raw
                if n >= page.last_page:
                    break
                if budget <= 0:
                    self.truncated = True
                    log.warning("carsguide_page_cap", path=path, read=n, pages=page.last_page)
                    break
                n += 1
                nxt = await self._page(http, path, n)
                budget -= 1
                if nxt is None or not nxt.listings:
                    break
                page = nxt

    def _emit(self, page: Page, params: SearchParams, seen: set[str]):
        for item in page.listings:
            lid = str(item.get("id") or "")
            if not lid or lid in seen or item.get("isSold") or item.get("deleted_at"):
                continue
            seen.add(lid)
            payload = {k: v for k, v in item.items() if k not in _DROP}
            payload["photos"] = [p.get("image_path") for p in item.get("photos") or []
                                 if isinstance(p, dict) and p.get("image_path")]
            url = f"{BASE}/{(item.get('url') or f'car/{lid}').lstrip('/')}"
            raw = RawListing(source_listing_id=lid, url=url, payload=payload)
            if matches(params, self.parse(raw)):
                yield raw

    async def confirm_gone(self, listing: KnownListing, http: PoliteClient) -> bool | None:
        resp = await http.get(listing.url)
        if resp.status_code in (404, 410):
            return True
        if resp.status_code != 200:
            return None
        final = urlsplit(str(resp.url))
        if "removed=true" in final.query or not final.path.startswith("/car/"):
            return True
        return False

    # -- parse -----------------------------------------------------------------------------
    def parse(self, raw: RawListing) -> NormalisedListing:
        p: dict[str, Any] = raw.payload
        make = normalise_make(p.get("make"))
        model, variant = normalise_model_variant(make, p.get("model"), p.get("variant"))
        price = p.get("advertisedPrice") if isinstance(p.get("advertisedPrice"), int) else None
        # CarsGuide shows "Drive Away" when flagged, and "Excl. Gov. Charges" otherwise.
        price_type = "unknown" if not price else "drive_away" if p.get("isDriveaway") else "egc"
        private = bool(p.get("isPrivate")) or str(p.get("adTypeStyle") or "").lower().startswith("private")
        year = p.get("year") if isinstance(p.get("year"), int) else None
        colour = (p.get("colourBase") or p.get("colourBody") or "").strip() or None
        comment = (p.get("comment") or "").strip() or None
        lat, lon = p.get("lat"), p.get("lon")
        return NormalisedListing(
            source_listing_id=raw.source_listing_id,
            url=raw.url,
            title=" ".join(str(x) for x in (year, make, model, variant) if x) or f"CarsGuide {raw.source_listing_id}",
            make=make,
            model=model,
            variant=variant,
            year=year,
            odometer_km=p.get("odometer") if isinstance(p.get("odometer"), int) and p.get("odometer") > 0 else None,
            price=price,
            price_type=price_type,
            body_type=normalise_spec("body_type", p.get("bodyType")),
            transmission=normalise_spec("transmission", p.get("transmission")),
            fuel_type=normalise_spec("fuel_type", p.get("fuelType")),
            engine=f"{p['engineSize']}L" if p.get("engineSize") else None,
            drivetrain=normalise_spec("drivetrain", p.get("driveType")),
            colour=colour.title() if colour else None,
            seller_type="private" if private else "dealer" if p.get("isDealer") else "unknown",
            suburb=(p.get("city") or "").strip().title() or None,
            state=normalise_state(p.get("state")),
            postcode=str(p["dealerPostcode"])[:4] if p.get("dealerPostcode") else None,
            lat=lat if isinstance(lat, (int, float)) else None,
            lon=lon if isinstance(lon, (int, float)) else None,
            images=[IMG + path for path in p.get("photos") or []][:40],
            description=comment,
            rego=None,
            vin=None,
        )
