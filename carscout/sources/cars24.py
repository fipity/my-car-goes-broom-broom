"""Cars24 (cars24.com.au): a used-car retailer, so every listing is a dealer listing.

How it works (checked 2026-11-01):
- Search pages are SEO paths: /buy-used-<make>-<model>/, /buy-used-<make>/ and
  /buy-used-cars-australia/, paged with ?page=N (20 cars a page, Next.js App Router).
  Their RSC flight data holds a card per car (make, model, variant, year, km, price, hub) and
  a pagination object (hasMore, totalElements).
- Filtering by query string (?f=, ?sf=) and /api/ are disallowed in robots.txt, so year, km,
  price and state are filtered here, after fetching the make/model page.
- Detail pages (/buy-used-<make>-<model>-car-<id>/) embed window.__PRELOADED_STATE__ with
  the VIN, suburb, state, colour, body, drivetrain and engine. Sold cars return HTTP 410.
- Detail pages cost one request per car, so a stored detail is reused while the card's price,
  km and status are unchanged and it is younger than options.detail_max_age_days.

Stored payload: {"card": ..., "detail": ... | null, "detail_fetched_at": iso | null}, with
bulky or per-request fields (finance quotes, "similar cars", view counters, signed URLs,
staff emails) removed.
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from carscout.core.logging import get_logger
from carscout.normalise.mapping import normalise_make, normalise_model, normalise_spec, normalise_state
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
from carscout.sources.nextjs import assigned_json, find_objects, flight_rows, flight_text

log = get_logger(__name__)

BASE = "https://www.cars24.com.au"
IMG_BASE = "https://fastly-production.24c.in/"
PAGE_SIZE = 20

# Cars24 sells from hubs; the card only names the hub city.
HUB_STATES = {"melbourne": "VIC", "sydney": "NSW", "brisbane": "QLD", "gold coast": "QLD",
              "perth": "WA", "adelaide": "SA", "canberra": "ACT", "hobart": "TAS"}

_CARD_DROP = {"emiDetails", "platformImage", "footerBackground", "isWishlisted",
              "showWishlistIcon", "businessTagV2", "usp", "isEmiPrimary"}
_DETAIL_DROP = {"similarModelCars", "similarBudgetCars", "moreCars", "emiDetails",
                "emiDetailsCustomDeposit", "carTourData", "numberViewed", "numberShortlisted",
                "fomoText", "ppsrReportLink", "topTechnicianUserId", "topRefurbJobs", "distance",
                "wishlisted", "buyWithConfidence", "highlightsGallery", "galleryV2",
                "serviceHistoryImages", "ewDetails", "wearAndTearGuideLink", "shareUrl",
                "features", "premiumFeatures"}

_GONE = object()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _get(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def _int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except ValueError:
        return None


def _fingerprint(card: dict[str, Any]) -> tuple[Any, ...]:
    return (_get(card, "listingPrice", "value"), _get(card, "odometer", "value"), card.get("status"))


class Plugin(SourcePlugin):
    name = "cars24"
    parser_version = 1

    # -- search ----------------------------------------------------------------------------
    def search_paths(self, params: SearchParams) -> list[str]:
        """Most specific listing page first; later ones are fallbacks when a slug 404s."""
        make = normalise_make(params.make)
        model = normalise_model(make, params.model)
        paths = []
        if make and model:
            paths.append(f"/buy-used-{slug(make)}-{slug(model)}/")
        if make:
            paths.append(f"/buy-used-{slug(make)}/")
        paths.append("/buy-used-cars-australia/")
        return paths

    async def _listing_page(self, http: PoliteClient, url: str) -> tuple[list[dict], dict] | None:
        resp = await http.get(url)
        if resp.status_code in (404, 410):
            return None
        if resp.status_code != 200:
            raise SourceUnavailable(f"HTTP {resp.status_code} for {url}")
        rows = flight_rows(flight_text(resp.text))
        cards = list(find_objects(rows, "appointmentId", "listingPrice", "make"))
        paging = next(find_objects(rows, "hasMore", "totalElements"), None)
        if paging is None and not cards:
            raise SourceChanged(f"no listing data found on {url}: the page layout has changed")
        return cards, paging or {"hasMore": False, "totalElements": len(cards)}

    async def search(self, params: SearchParams, http: PoliteClient) -> AsyncIterator[RawListing]:
        max_pages = int(self.config.options.get("max_pages", 15))
        max_age = timedelta(days=float(self.config.options.get("detail_max_age_days", 7)))

        base, first = None, None
        for path in self.search_paths(params):
            first = await self._listing_page(http, BASE + path)
            if first is not None:
                base = BASE + path
                break
            log.info("cars24_path_missing", path=path)
        if first is None or base is None:
            raise SourceUnavailable("no Cars24 listing page found for this search")

        seen: set[str] = set()
        page, (cards, paging) = 1, first
        log.info("cars24_search", url=base, total=paging.get("totalElements"))
        while True:
            for card in cards:
                cid = str(card["appointmentId"])
                if cid in seen:
                    continue
                seen.add(cid)
                raw = await self._build(card, params, http, max_age)
                if raw is None:
                    continue
                if matches(params, self.parse(raw)):
                    yield raw
            if not paging.get("hasMore"):
                break
            if page >= max_pages:
                self.truncated = True
                log.warning("cars24_page_cap", max_pages=max_pages, total=paging.get("totalElements"))
                break
            page += 1
            result = await self._listing_page(http, f"{base}?page={page}")
            if result is None:
                break
            cards, paging = result

    async def _build(
        self, card: dict[str, Any], params: SearchParams, http: PoliteClient, max_age: timedelta
    ) -> RawListing | None:
        card = {k: v for k, v in card.items() if k not in _CARD_DROP}
        cid = str(card["appointmentId"])
        url = card.get("deepLinkUrl") or f"{BASE}/{card.get('cdpRelativeUrl', '')}"
        raw = RawListing(source_listing_id=cid, url=url,
                         payload={"card": card, "detail": None, "detail_fetched_at": None})

        # Cheap pre-filter on the card before spending a request on the detail page.
        if not matches(params, self.parse(raw)):
            return None

        prev = self.previous(cid)
        if isinstance(prev, dict) and prev.get("detail") and prev.get("detail_fetched_at"):
            fetched = datetime.fromisoformat(prev["detail_fetched_at"])
            if (_fingerprint(prev.get("card") or {}) == _fingerprint(card)
                    and datetime.now(UTC) - fetched < max_age):
                raw.payload["detail"] = prev["detail"]
                raw.payload["detail_fetched_at"] = prev["detail_fetched_at"]
                return raw

        detail = await self._detail(url, http)
        if isinstance(detail, dict) and str(detail.get("appointmentId")) != cid:
            log.warning("cars24_detail_mismatch", id=cid, got=detail.get("appointmentId"))
            detail = None
        if detail is _GONE:
            log.info("cars24_gone_before_detail", id=cid)
            return None
        if detail is not None:
            raw.payload["detail"] = detail
            raw.payload["detail_fetched_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        return raw

    async def _detail(self, url: str, http: PoliteClient) -> Any:
        resp = await http.get(url)
        if resp.status_code in (404, 410):
            return _GONE
        if resp.status_code != 200:
            raise SourceUnavailable(f"HTTP {resp.status_code} for {url}")
        content = _get(assigned_json(resp.text, "window.__PRELOADED_STATE__"), "carDetails", "content")
        if not isinstance(content, dict) or "appointmentId" not in content:
            log.warning("cars24_detail_unparsed", url=url)  # keep the card-only listing
            return None
        return {k: v for k, v in content.items() if k not in _DETAIL_DROP}

    async def confirm_gone(self, listing: KnownListing, http: PoliteClient) -> bool | None:
        resp = await http.get(listing.url)
        if resp.status_code in (404, 410):
            return True
        if resp.status_code != 200:
            return None
        content = _get(assigned_json(resp.text, "window.__PRELOADED_STATE__"), "carDetails", "content")
        if not isinstance(content, dict):
            return None
        return content.get("listingActive") is False

    # -- parse -----------------------------------------------------------------------------
    def parse(self, raw: RawListing) -> NormalisedListing:
        p = raw.payload
        card: dict[str, Any] = p.get("card") or {}
        d: dict[str, Any] = p.get("detail") or {}

        make = normalise_make(d.get("make") or card.get("make"))
        model = normalise_model(make, d.get("model") or card.get("model"))
        variant = (d.get("variant") or card.get("variant") or card.get("subtitle") or "").strip() or None
        year = _int(d.get("year") or card.get("year"))

        # Card alt text: "2022 - Toyota Corolla - Hatchback - Hybrid - Automatic"
        alt = [s.strip() for s in (_get(card, "listingImage", "altText") or "").split(" - ")]
        alt_body = alt[2] if len(alt) == 5 else None
        alt_trans = alt[4] if len(alt) == 5 else None
        badges = [b.get("text", "") for b in card.get("carInfoBadges") or [] if isinstance(b, dict)]
        badge_trans = next((b for b in badges if b.lower() in ("automatic", "manual")), None)

        hub = (_get(card, "location", "hubName") or d.get("city") or "").strip()
        state_code = (d.get("stateCode") or "").removeprefix("AU_")
        state = normalise_state(state_code) or HUB_STATES.get(hub.lower())

        highlights = {h.get("key"): h.get("value") for h in d.get("highlights") or [] if isinstance(h, dict)}

        images = [f"{IMG_BASE}{img['path']}?w=1024" for img in (d.get("exteriorImages") or []) + (d.get("interiorImages") or [])
                  if isinstance(img, dict) and img.get("path")]
        if not images and _get(card, "listingImage", "uri"):
            images = [card["listingImage"]["uri"]]

        history = [v.get("value") for v in _get(d, "carHistory", "values") or [] if isinstance(v, dict) and v.get("value")]
        description = ("Cars24 history check: " + "; ".join(history) + ".") if history else None

        price = _int(_get(card, "listingPrice", "value")) or _int(d.get("price"))
        title = " ".join(str(x) for x in (year, make, model, variant) if x) or f"Cars24 {raw.source_listing_id}"

        return NormalisedListing(
            source_listing_id=raw.source_listing_id,
            url=raw.url,
            title=title,
            make=make,
            model=model,
            variant=variant,
            year=year,
            odometer_km=_int(d.get("odometerReading")) or _int(_get(card, "odometer", "value")),
            price=price,
            price_type="drive_away" if d.get("dapPriceEnabled") else ("egc" if price else "unknown"),
            body_type=normalise_spec("body_type", d.get("bodyType") or alt_body),
            transmission=normalise_spec("transmission", d.get("transmissionType") or alt_trans or badge_trans),
            fuel_type=normalise_spec("fuel_type", d.get("fuelType") or card.get("fuelType")),
            engine=highlights.get("engine"),
            drivetrain=normalise_spec("drivetrain", d.get("driveType") or highlights.get("driveType")),
            colour=(d.get("color") or "").strip().title() or None,
            seller_type="dealer",
            suburb=(d.get("suburb") or "").strip() or None,
            state=state,
            postcode=None,
            images=images[:40],
            description=description,
            rego=None,
            vin=(d.get("vin") or "").strip().upper() or None,
        )
