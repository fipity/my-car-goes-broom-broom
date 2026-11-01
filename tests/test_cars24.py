"""Cars24 plugin against saved real pages (tests/fixtures/cars24, captured 2026-11-01).
No network: a fake transport serves the fixtures."""
import gzip
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from carscout.core.config import SourceConfig
from carscout.sources.base import KnownListing, RawListing, SearchParams, SourceChanged
from carscout.sources.cars24 import Plugin
from carscout.sources.http import PoliteClient

FIX = Path(__file__).parent / "fixtures/cars24"
P1 = gzip.open(FIX / "search_toyota_corolla_p1.html.gz", "rt").read()
P2 = gzip.open(FIX / "search_toyota_corolla_p2.html.gz", "rt").read()
DETAIL = gzip.open(FIX / "detail_23606515385984.html.gz", "rt").read()
ROBOTS = (FIX / "robots.txt").read_text()
DETAIL_ID = "23606515385984"
HTML = {"content-type": "text/html; charset=utf-8"}


class FakeSite:
    """Serves the Corolla pages; every car's detail page is the fixture with its id swapped in."""

    def __init__(self, extra=None):
        self.calls: list[str] = []
        self.extra = extra or {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path, page = req.url.path, req.url.params.get("page")
        self.calls.append(path + (f"?page={page}" if page else ""))
        if path in self.extra:
            return self.extra[path]
        if path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if path == "/buy-used-toyota-corolla/":
            return httpx.Response(200, text=P2 if page == "2" else P1, headers=HTML)
        m = re.fullmatch(r"/buy-used-[a-z0-9-]+-car-(\d+)/", path)
        if m:
            return httpx.Response(200, text=DETAIL.replace(DETAIL_ID, m.group(1)), headers=HTML)
        return httpx.Response(404, text="<html>not found</html>", headers=HTML)

    @property
    def detail_calls(self) -> list[str]:
        return [c for c in self.calls if "-car-" in c]


def make(site: FakeSite, **options) -> tuple[Plugin, PoliteClient]:
    async def no_sleep(_s):
        pass

    cfg = SourceConfig(name="cars24", label="Cars24", delay=0, jitter=0, concurrency=1, options=options)
    http = PoliteClient("cars24", delay=0, jitter=0, transport=httpx.MockTransport(site), sleep=no_sleep)
    return Plugin(cfg), http


async def collect(plugin, http, **params) -> list[RawListing]:
    return [r async for r in plugin.search(SearchParams(**params), http)]


def card_only(raw: RawListing) -> RawListing:
    return RawListing(raw.source_listing_id, raw.url, {"card": raw.payload["card"]})


async def test_search_pages_and_details():
    site = FakeSite()
    plugin, http = make(site)
    out = await collect(plugin, http, make="Toyota", model="Corolla")
    assert len(out) == 21 and len({r.source_listing_id for r in out}) == 21
    listing_pages = [c for c in site.calls if "-car-" not in c]
    assert listing_pages == ["/robots.txt", "/buy-used-toyota-corolla/", "/buy-used-toyota-corolla/?page=2"]
    assert len(site.detail_calls) == 21
    assert not plugin.truncated
    # payload is trimmed: no finance quotes / similar cars / staff emails
    p = out[0].payload
    assert "emiDetails" not in p["card"] and "similarModelCars" not in p["detail"]
    assert "topTechnicianUserId" not in p["detail"] and p["detail_fetched_at"]


async def test_parse_full_listing():
    site = FakeSite()
    plugin, http = make(site)
    raw = next(r for r in await collect(plugin, http, make="toyota", model="corolla")
               if r.source_listing_id == DETAIL_ID)
    l = plugin.parse(raw)
    assert (l.make, l.model, l.variant, l.year) == ("Toyota", "Corolla", "Ascent Sport Hybrid", 2022)
    assert l.title == "2022 Toyota Corolla Ascent Sport Hybrid"
    assert (l.price, l.price_type, l.odometer_km) == (26190, "egc", 50735)
    assert (l.body_type, l.transmission, l.fuel_type, l.drivetrain) == ("Hatch", "Automatic", "Hybrid", "FWD")
    assert l.engine == "4cyl 1.8L Inline 4 Hybrid" and l.colour == "Jasper Red"
    assert (l.suburb, l.state, l.seller_type) == ("Altona", "VIC", "dealer")
    assert l.vin == "JTNKE3BE803503547"
    assert len(l.images) > 10 and l.images[0].startswith("https://fastly-production.24c.in/")
    assert "No write offs on record" in l.description
    assert l.url == f"https://www.cars24.com.au/buy-used-toyota-corolla-car-{DETAIL_ID}/"


async def test_parse_card_only():
    site = FakeSite()
    plugin, http = make(site)
    raw = (await collect(plugin, http, make="toyota", model="corolla"))[0]
    l = plugin.parse(card_only(raw))
    assert l.make == "Toyota" and l.model == "Corolla" and l.year and l.price and l.odometer_km
    assert l.body_type == "Hatch" and l.transmission == "Automatic" and l.state in ("VIC", "NSW", "QLD")
    assert l.vin is None and l.suburb is None and len(l.images) == 1


async def test_card_prefilter_saves_detail_requests():
    site = FakeSite()
    plugin, http = make(site)
    out = await collect(plugin, http, make="Toyota", model="Corolla", price_max=20000)
    assert out and all(plugin.parse(r).price <= 20000 for r in out)
    assert len(site.detail_calls) == len(out) < 21  # no detail fetch for cars over budget


async def test_state_filter_uses_hub():
    site = FakeSite()
    plugin, http = make(site)
    everything = await collect(plugin, http, make="Toyota", model="Corolla")
    qld_hub = {r.source_listing_id for r in everything if plugin.parse(card_only(r)).state == "QLD"}
    assert qld_hub and len(qld_hub) < 21  # fixture has a Brisbane car on page 2

    site2 = FakeSite()
    plugin2, http2 = make(site2)
    out = await collect(plugin2, http2, make="Toyota", model="Corolla", states=["QLD"])
    # Only QLD-hub cards get a detail fetch. The fixture detail page says VIC for every car,
    # so the post-detail filter then drops them: both filter stages are exercised.
    assert len(site2.detail_calls) == len(qld_hub)
    assert out == []


async def test_reuses_stored_detail_when_card_unchanged():
    site = FakeSite()
    plugin, http = make(site)
    first = {r.source_listing_id: r.payload for r in await collect(plugin, http, make="Toyota", model="Corolla")}

    site2 = FakeSite()
    plugin2, http2 = make(site2)
    plugin2.previous = first.get
    again = await collect(plugin2, http2, make="Toyota", model="Corolla")
    assert len(again) == 21 and site2.detail_calls == []
    assert [r.payload for r in again] == [first[r.source_listing_id] for r in again]  # stable => raw dedupe works

    # A changed price, or a detail older than detail_max_age_days, means fetching it again.
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
    changed_id, *rest = list(first)
    prev = {k: {**first[k], "detail_fetched_at": old} for k in rest}
    prev[changed_id] = {**first[changed_id], "card": {**first[changed_id]["card"], "listingPrice": {"value": 1}}}
    site3 = FakeSite()
    plugin3, http3 = make(site3)
    plugin3.previous = prev.get
    await collect(plugin3, http3, make="Toyota", model="Corolla")
    assert len(site3.detail_calls) == 21


async def test_unknown_model_falls_back_to_make_page():
    site = FakeSite(extra={"/buy-used-toyota/": httpx.Response(200, text=P2, headers=HTML)})
    plugin, http = make(site)
    out = await collect(plugin, http, make="Toyota", model="Zzzz")
    assert "/buy-used-toyota-zzzz/" in site.calls and "/buy-used-toyota/" in site.calls
    assert out == [] and site.detail_calls == []  # cards are Corollas: filtered out by model


async def test_gone_before_detail_is_skipped():
    site = FakeSite(extra={f"/buy-used-toyota-corolla-car-{DETAIL_ID}/": httpx.Response(410, text="gone", headers=HTML)})
    plugin, http = make(site)
    out = await collect(plugin, http, make="Toyota", model="Corolla")
    assert len(out) == 20 and DETAIL_ID not in {r.source_listing_id for r in out}


async def test_page_cap_marks_truncated():
    site = FakeSite()
    plugin, http = make(site, max_pages=1)
    out = await collect(plugin, http, make="Toyota", model="Corolla")
    assert len(out) == 20 and plugin.truncated


async def test_layout_change_is_reported():
    site = FakeSite(extra={"/buy-used-toyota-corolla/": httpx.Response(200, text="<html>new site</html>", headers=HTML)})
    plugin, http = make(site)
    with pytest.raises(SourceChanged):
        await collect(plugin, http, make="Toyota", model="Corolla")


async def test_confirm_gone():
    gone_url = "https://www.cars24.com.au/buy-used-toyota-corolla-car-1/"
    live_url = f"https://www.cars24.com.au/buy-used-toyota-corolla-car-{DETAIL_ID}/"
    site = FakeSite(extra={"/buy-used-toyota-corolla-car-1/": httpx.Response(410, text="gone", headers=HTML)})
    plugin, http = make(site)
    assert await plugin.confirm_gone(KnownListing("1", gone_url), http) is True
    assert await plugin.confirm_gone(KnownListing(DETAIL_ID, live_url), http) is False


def test_search_paths():
    plugin, _ = make(FakeSite())
    assert plugin.search_paths(SearchParams(make="mercedes", model="glc")) == [
        "/buy-used-mercedes-benz-glc/", "/buy-used-mercedes-benz/", "/buy-used-cars-australia/"]
    assert plugin.search_paths(SearchParams(make="Toyota", model="landcruiser prado"))[0] == \
        "/buy-used-toyota-landcruiser-prado/"
    assert plugin.search_paths(SearchParams())[0] == "/buy-used-cars-australia/"
