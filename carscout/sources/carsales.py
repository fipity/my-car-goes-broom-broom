"""Carsales (carsales.com.au). Card data only, first page of each search path.

What the site allows (checked 2026-11-01):
- SEO search paths load without a challenge: /cars/used/<make>/<model>/[<badge>-badge/]
  [<state>-state/][<region>-region/][<body>-bodystyle/][<trans>-transmission/]. Each page's
  __NEXT_DATA__ holds a server-driven UI tree with ~21 organic + ~8 promoted ListingCards.
- Any query string (?offset= paging, and robots.txt also disallows ?sort) and every detail
  page (/cars/details/...) gets a DataDome captcha. We don't go there, so:
  - only the first page of each path is read. To see more cars, one search fans out over a few
    paths: one per state in the search, then splits (by state, body style, transmission) using
    the values seen on the first page, most common first. Capped at options.max_pages requests;
  - there's no VIN, suburb or description, and no way to confirm a car has sold. A run counts
    as complete (so "gone" detection runs) only when every base path fits on its first page.
- Year, km and price have no path form: they're filtered here (sources/filters.py).

Stored payload: a compact copy of each card (tracking fields that change per request, like
search ids and result positions, are dropped so unchanged listings dedupe) plus the JSON-LD
item, whose name carries the model series ("KF Series").
"""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

from carscout.core.logging import get_logger
from carscout.normalise.mapping import (
    normalise_make,
    normalise_model,
    normalise_model_variant,
    normalise_spec,
    normalise_state,
)
from carscout.sources.base import (
    NormalisedListing,
    RawListing,
    SearchParams,
    SourceChanged,
    SourcePlugin,
    SourceUnavailable,
)
from carscout.sources.filters import matches
from carscout.sources.http import PoliteClient

log = get_logger(__name__)

BASE = "https://www.carsales.com.au"
STATE_SLUGS = {"VIC": "victoria", "NSW": "new-south-wales", "QLD": "queensland",
               "SA": "south-australia", "WA": "western-australia", "TAS": "tasmania",
               "ACT": "australian-capital-territory", "NT": "northern-territory"}
BODY_SLUGS = {"Hatch": "hatch", "Sedan": "sedan", "Wagon": "wagon", "SUV": "suv", "Ute": "ute",
              "Cab Chassis": "cab-chassis", "Van": "van", "Coupe": "coupe",
              "Convertible": "convertible", "People Mover": "people-mover"}
TRANS_SLUGS = {"Automatic": "automatic", "Manual": "manual"}
SEGMENT_ORDER = ("badge", "state", "region", "bodystyle", "transmission")
DEFAULT_SPLITS = ("state", "bodystyle", "transmission")

_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_COUNT = re.compile(r'"listingresultcount":\s*"(\d+)"')
_ID = re.compile(r"/([A-Z]{2,5}-AD-\d+)/")
_TRANS_WORD = re.compile(r"\b(Auto|Automatic|Manual|CVT|DCT|DSG|Constantly Variable|Sports Automatic"
                         r"(?: Dual Clutch)?|Seq Manual Auto-Clutch|Auto Dual Clutch|Auto Seq|Auto-Clutch)\b")
_KM = re.compile(r"^([\d,]+)\s*km$", re.I)
_ENGINE = re.compile(r"\b(\d+cyl|\d\.\d+L|kW|Electric)\b", re.I)

# Per-card tracking fields worth keeping. Everything else in metaData changes between
# requests (search ids, positions, sort) and would defeat raw-payload dedupe.
_META_KEEP = ("networkId", "make", "model", "year", "price", "state", "adtype", "sellerId",
              "type", "categorytype", "listingBadges")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _texts(node: Any) -> list[str]:
    """Visible text values in a UI subtree, in order (skipping actions/tracking)."""
    out: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if o.get("type") in ("Text", "Link") and isinstance(o.get("value"), str) and o["value"].strip():
                out.append(o["value"].strip())
            for k, v in o.items():
                if k not in ("action", "tracking"):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(node)
    return out


def _int(v: Any) -> int | None:
    digits = re.sub(r"[^\d]", "", str(v or ""))
    return int(digits) if digits else None


def compact_card(card: dict[str, Any], ld: dict[str, Any] | None) -> dict[str, Any]:
    meta = _meta(card)
    data = (card.get("action") or {}).get("data") or {}
    gallery = [c.get("url") for c in (card.get("gallery") or {}).get("children") or []
               if isinstance(c, dict) and c.get("url")]
    return {
        "meta": {k: meta[k] for k in _META_KEEP if k in meta},
        "url": (data.get("url") or "").split("?", 1)[0],
        "title": _texts(card.get("title")),
        "keyDetails": list(card.get("keyDetailsTexts") or []),
        "price": _texts(card.get("price")),
        "priceInfo": _texts(card.get("priceInfo")),
        "seller": _texts(card.get("sellerTitle")),
        "badges": _texts(card.get("detailsBadges")),
        "images": gallery,
        "imageCount": ((card.get("galleryBadge") or {}).get("images") or {}).get("count"),
        "ld": {k: ld[k] for k in ("name", "url") if k in ld} | {
            "images": [i.get("url") for i in ld.get("image") or [] if isinstance(i, dict)]
        } if ld else None,
    }


class Page:
    def __init__(self, html: str):
        m = _NEXT.search(html)
        if not m:
            raise SourceChanged("Carsales page has no __NEXT_DATA__: the layout has changed")
        data = json.loads(m.group(1))
        props = (data.get("props") or {}).get("pageProps") or {}
        root = props.get("initialRoot")
        if root is None:
            raise SourceChanged("Carsales __NEXT_DATA__ has no initialRoot: the layout has changed")
        self.cards: list[dict[str, Any]] = []
        self._find_cards(root)
        count = _COUNT.search(m.group(1))
        if count is None and not self.cards:
            raise SourceChanged("no listing data found on the Carsales page: the layout has changed")
        self.total = int(count.group(1)) if count else len(self.cards)
        self.ld = self._ld_items(props)

    def _find_cards(self, o: Any) -> None:
        if isinstance(o, dict):
            if o.get("type") == "ListingCard":
                self.cards.append(o)
                return
            for v in o.values():
                self._find_cards(v)
        elif isinstance(o, list):
            for v in o:
                self._find_cards(v)

    @staticmethod
    def _ld_items(props: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for script in (props.get("scripts") or {}).get("applicationLdScripts") or []:
            try:
                ld = json.loads(script) if isinstance(script, str) else script
            except json.JSONDecodeError:
                continue
            if not isinstance(ld, dict):
                continue
            for node in ld.get("@graph", [ld]):
                entity = node.get("mainEntity") if isinstance(node, dict) else None
                if not isinstance(entity, dict):
                    continue
                for el in entity.get("itemListElement") or []:
                    item = el.get("item") or {}
                    m = _ID.search(item.get("url") or "")
                    if m:
                        out[m.group(1)] = item
        return out

    @property
    def organic(self) -> int:
        return sum(1 for c in self.cards if _result_type(c) != "promoted")


def _meta(card: dict[str, Any]) -> dict[str, Any]:
    try:
        return card["action"]["tracking"]["providers"]["csnInsights"]["metaData"] or {}
    except (KeyError, TypeError):
        return {}


def _result_type(card: dict[str, Any]) -> str | None:
    return _meta(card).get("result_type")


def _network_id(card: dict[str, Any]) -> str | None:
    nid = _meta(card).get("networkId")
    if nid:
        return nid
    m = _ID.search(((card.get("action") or {}).get("data") or {}).get("url") or "")
    return m.group(1) if m else None


class Plugin(SourcePlugin):
    name = "carsales"
    parser_version = 1

    # -- paths -----------------------------------------------------------------------------
    @staticmethod
    def build_path(make: str | None, model: str | None, segments: dict[str, str]) -> str:
        parts = ["cars", "used"]
        if make:
            parts.append(slug(make))
            if model:
                parts.append(slug(model))
        parts += [f"{segments[k]}-{k}" for k in SEGMENT_ORDER if k in segments]
        return "/" + "/".join(parts) + "/"

    def base_segments(self, params: SearchParams, with_badge: bool = True) -> list[dict[str, str]]:
        fixed: dict[str, str] = {}
        if params.variant and with_badge:
            fixed["badge"] = slug(params.variant)
        body = BODY_SLUGS.get(normalise_spec("body_type", params.body_type) or "")
        if body:
            fixed["bodystyle"] = body
        trans = TRANS_SLUGS.get(normalise_spec("transmission", params.transmission) or "")
        if trans:
            fixed["transmission"] = trans
        states = [STATE_SLUGS[s] for s in params.states if s in STATE_SLUGS]
        return [fixed | {"state": s} for s in states] or [fixed]

    def split_candidates(self, page: Page, segments: dict[str, str], kinds: tuple[str, ...]) -> list[dict[str, str]]:
        """Sub-paths worth fetching when a page holds only part of its results. The values come
        from the cards on the page (a Corolla page shows Hatch/Sedan/Wagon, never Ute), most
        common first, so we don't spend requests on paths that would be empty."""
        seen: dict[str, Counter[str]] = {k: Counter() for k in kinds}
        for card in page.cards:
            l = self.parse(RawListing("x", "", compact_card(card, None)))
            if l.state in STATE_SLUGS:
                seen.get("state", Counter())[STATE_SLUGS[l.state]] += 1
            if l.body_type in BODY_SLUGS:
                seen.get("bodystyle", Counter())[BODY_SLUGS[l.body_type]] += 1
            if l.transmission in TRANS_SLUGS:
                seen.get("transmission", Counter())[TRANS_SLUGS[l.transmission]] += 1
        out = []
        for kind in kinds:
            if kind in segments or len(seen.get(kind, ())) < 2:
                continue  # already fixed, or only one value: splitting wouldn't narrow anything
            out += [segments | {kind: value} for value, _ in seen[kind].most_common()]
        return out

    async def _page(self, http: PoliteClient, path: str) -> Page | None:
        resp = await http.get(BASE + path)
        if resp.status_code in (404, 410):
            return None
        if resp.status_code != 200:
            raise SourceUnavailable(f"HTTP {resp.status_code} for {path}")
        return Page(resp.text)

    # -- search ----------------------------------------------------------------------------
    async def search(self, params: SearchParams, http: PoliteClient) -> AsyncIterator[RawListing]:
        budget = int(self.config.options.get("max_pages", 8))
        kinds = tuple(self.config.options.get("split_by") or DEFAULT_SPLITS)
        make = normalise_make(params.make)
        model = normalise_model(make, params.model)

        async def fetch(model_name: str | None, segs: dict[str, str]) -> Page | None:
            nonlocal budget
            budget -= 1
            return await self._page(http, self.build_path(make, model_name, segs))

        # Find a path that exists. Model slugs mostly match ours ("cx-5", "c-class", "hilux");
        # also try without hyphens, and drop an unknown badge rather than fail the search.
        bases = self.base_segments(params)
        attempts = [(model, bases)]
        if model and "-" in model:
            attempts.append((model.replace("-", ""), bases))
        if params.variant:
            attempts.append((model, self.base_segments(params, with_badge=False)))
        first = None
        for model_name, segs_list in attempts:
            first = await fetch(model_name, segs_list[0])
            if first is not None:
                bases = segs_list
                break
            log.info("carsales_path_missing", path=self.build_path(make, model_name, segs_list[0]))
        if first is None:
            raise SourceUnavailable(
                f"Carsales has no page for {make or ''} {model or ''} "
                f"({self.build_path(make, model, bases[0])}). Check the make/model spelling."
            )

        seen: set[str] = set()
        complete = True
        splits: list[dict[str, str]] = []
        pages: list[tuple[dict[str, str], Page]] = [(bases[0], first)]
        for segs in bases[1:]:
            if budget <= 0:
                complete = False  # some states never fetched
                break
            page = await fetch(model_name, segs)
            if page is not None:
                pages.append((segs, page))

        for segs, page in pages:
            log.info("carsales_page", path=self.build_path(make, model_name, segs),
                     total=page.total, cards=len(page.cards))
            if page.total > page.organic:
                complete = False
                splits += self.split_candidates(page, segs, kinds)
            async for raw in self._emit(page, params, seen):
                yield raw

        # Remaining budget goes to split pages, in the order the base pages offered them.
        for segs in splits:
            if budget <= 0:
                break
            page = await fetch(model_name, segs)
            if page is None:
                continue
            log.info("carsales_split", path=self.build_path(make, model_name, segs),
                     total=page.total, cards=len(page.cards))
            async for raw in self._emit(page, params, seen):
                yield raw

        self.truncated = not complete

    async def _emit(self, page: Page, params: SearchParams, seen: set[str]) -> AsyncIterator[RawListing]:
        for card in page.cards:
            nid = _network_id(card)
            if not nid or nid in seen:
                continue
            seen.add(nid)
            compact = compact_card(card, page.ld.get(nid))
            raw = RawListing(source_listing_id=nid, url=BASE + compact["url"], payload=compact)
            if matches(params, self.parse(raw)):
                yield raw

    # -- parse -----------------------------------------------------------------------------
    def parse(self, raw: RawListing) -> NormalisedListing:
        p = raw.payload
        meta: dict[str, Any] = p.get("meta") or {}
        title = p.get("title") or []
        ld = p.get("ld") or {}

        make = normalise_make(meta.get("make"))
        subtitle = title[1] if len(title) > 1 else ""
        # "Ascent Sport Auto F", "GR GTS Manual GR-FOUR", "Touring Auto i-ACTIV AWD":
        # badge, then the transmission word, then the drivetrain.
        badge, trans_word, drive = subtitle, None, None
        m = None
        for m in _TRANS_WORD.finditer(subtitle):
            pass
        if m:
            badge, trans_word, drive = subtitle[: m.start()].strip(), m.group(1), subtitle[m.end():].strip()
        model, variant = normalise_model_variant(make, meta.get("model"), badge or None)

        body = engine = transmission = None
        km = None
        for t in p.get("keyDetails") or []:
            if (km_m := _KM.match(t.strip())):
                km = _int(km_m.group(1))
            elif normalise_spec("transmission", t) in ("Automatic", "Manual"):
                transmission = normalise_spec("transmission", t)
            elif _ENGINE.search(t):
                engine = t
            elif not t.lower().startswith("built"):
                body = normalise_spec("body_type", t)
        transmission = transmission or normalise_spec("transmission", trans_word)

        price = _int((p.get("price") or [None])[0]) or _int(meta.get("price"))
        info = " ".join(p.get("priceInfo") or []).lower()
        if "drive away" in info:
            price_type = "drive_away"
        elif "excl" in info and "gov" in info:
            price_type = "egc"
        elif "neg" in info:
            price_type = "negotiable"
        else:
            price_type = "unknown"
        if not price:
            price_type = "unknown"

        adtype = (meta.get("adtype") or " ".join(p.get("seller") or [])).lower()
        seller_type = "private" if adtype.startswith("private") else "dealer" if adtype.startswith("dealer") else "unknown"
        seller_bits = [s.strip() for s in " ".join(p.get("seller") or []).split("•")]
        state = normalise_state(meta.get("state")) or normalise_state(seller_bits[-1] if seller_bits else None)

        fuel = None
        if engine:
            fuel_m = re.search(r"(Petrol|Diesel|Hybrid|Electric|LPG|Plug-in Hybrid|PHEV)\s*$", engine, re.I)
            fuel = normalise_spec("fuel_type", fuel_m.group(1)) if fuel_m else None

        images = [u for u in (p.get("images") or []) if u] or [u for u in ld.get("images") or [] if u]
        year = _int(meta.get("year")) or _int(title[0][:4] if title else None)
        name = ld.get("name") or " ".join(title)
        return NormalisedListing(
            source_listing_id=raw.source_listing_id,
            url=raw.url,
            title=" ".join(str(x) for x in (year, make, model, variant) if x) or name or raw.source_listing_id,
            make=make,
            model=model,
            variant=variant,
            year=year,
            odometer_km=km,
            price=price,
            price_type=price_type,
            body_type=body,
            transmission=transmission,
            fuel_type=fuel,
            engine=engine,
            drivetrain=normalise_spec("drivetrain", drive) if drive else None,
            colour=None,
            seller_type=seller_type,
            suburb=None,
            state=state,
            postcode=None,
            images=images,
            description=None,
            rego=None,
            vin=None,
        )

