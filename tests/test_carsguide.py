"""CarsGuide plugin against saved real search pages (tests/fixtures/carsguide, captured 2026-11-01).
No network: a fake transport serves the fixtures."""
import gzip
from pathlib import Path

import httpx
import pytest

from carscout.core.config import SourceConfig
from carscout.sources.base import KnownListing, RawListing, SearchParams, SourceChanged, SourceUnavailable
from carscout.sources.carsguide import Plugin
from carscout.sources.http import PoliteClient
from carscout.sources.robots import Robots

FIX = Path(__file__).parent / "fixtures/carsguide"
P1 = gzip.open(FIX / "search_toyota_corolla_p1.html.gz", "rt").read()
P2 = gzip.open(FIX / "search_toyota_corolla_p2.html.gz", "rt").read()
ROBOTS = (FIX / "robots.txt").read_text()
HTML = {"content-type": "text/html; charset=utf-8"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"


def with_filters(html: str, **filters: str) -> str:
    """The fixture page, but reporting the given filters as applied (as a narrowed path would)."""
    for k, v in filters.items():
        html = html.replace(f"{k}:null", f"{k}:{v}")
    return html


class FakeSite:
    def __init__(self, routes: dict[str, object]):
        self.routes = routes  # exact path -> html, (html_p1, html_p2), or Response
        self.calls: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path, page = req.url.path, req.url.params.get("page")
        self.calls.append(path + (f"?page={page}" if page else ""))
        if path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if path == "/buy-a-car" and req.url.params.get("removed"):
            return httpx.Response(200, text=P1, headers=HTML)
        r = self.routes.get(path)
        if r is None:
            return httpx.Response(404, text="<html>nf</html>", headers=HTML)
        if isinstance(r, httpx.Response):
            return r
        if isinstance(r, tuple):
            r = r[1] if page == "2" else r[0]
        return httpx.Response(200, text=r, headers=HTML)

    @property
    def pages(self) -> list[str]:
        return [c for c in self.calls if c != "/robots.txt"]


def make(site, **options):
    async def no_sleep(_s):
        pass

    cfg = SourceConfig(name="carsguide", label="CarsGuide", delay=0, jitter=0, concurrency=1, options=options)
    http = PoliteClient("carsguide", delay=0, jitter=0, transport=httpx.MockTransport(site), sleep=no_sleep)
    return Plugin(cfg), http


async def collect(plugin, http, **params) -> list[RawListing]:
    return [r async for r in plugin.search(SearchParams(**params), http)]


async def test_pages_and_parse():
    site = FakeSite({"/buy-a-car/toyota/corolla": (P1, P2)})
    plugin, http = make(site, max_pages=2)
    out = await collect(plugin, http, make="Toyota", model="Corolla")
    assert site.pages == ["/buy-a-car/toyota/corolla", "/buy-a-car/toyota/corolla?page=2"]
    assert len(out) == 52 and plugin.truncated  # 47 pages exist; capped at 2
    by_id = {r.source_listing_id: plugin.parse(r) for r in out}
    l = by_id["15345469"]
    assert l.title == "2022 Toyota Corolla Ascent Sport"
    assert (l.year, l.odometer_km, l.price, l.price_type) == (2022, 54768, 23990, "drive_away")
    assert (l.body_type, l.transmission, l.fuel_type, l.drivetrain) == ("Sedan", "Automatic", "Petrol", "FWD")
    assert (l.colour, l.seller_type, l.suburb, l.state) == ("Silver", "dealer", "Maidstone", "VIC")
    assert l.lat == -37.78 and l.lon == 144.87
    assert l.url == "https://www.carsguide.com.au/car/15345469/toyota/corolla/vic/maidstone/sedan"
    assert l.images[0].startswith("https://images.autotrader.com.au/t_listing_main_cg/inventory/")
    assert l.description and "Corolla" in l.description
    assert {x.price_type for x in by_id.values()} == {"drive_away", "egc"}


async def test_payload_compact_and_stable():
    site = FakeSite({"/buy-a-car/toyota/corolla": (P1, P2)})
    plugin, http = make(site, max_pages=1)
    first = await collect(plugin, http, make="Toyota", model="Corolla")
    p = first[0].payload
    assert "dataLayer" not in p and "searchPosition" not in p and "distanceFromMe" not in p
    assert isinstance(p["photos"][0], str) and len(str(p)) < 8000
    plugin2, http2 = make(FakeSite({"/buy-a-car/toyota/corolla": (P1, P2)}), max_pages=1)
    assert [r.payload for r in await collect(plugin2, http2, make="Toyota", model="Corolla")] == [r.payload for r in first]


async def test_narrowed_path_used_when_carsguide_applies_it():
    narrowed = with_filters(P1, year_from="2015", year_to="2020", price_to="25000")
    site = FakeSite({"/buy-a-car/toyota/corolla/vic/2015-2020/under-$25000": narrowed})
    plugin, http = make(site)
    out = await collect(plugin, http, make="Toyota", model="Corolla", states=["VIC"],
                        year_min=2015, year_max=2020, price_max=25000, km_max=120000)
    assert site.pages[0] == "/buy-a-car/toyota/corolla/vic/2015-2020/under-$25000"
    for r in out:  # the fixture holds other years too: client-side filters still apply
        l = plugin.parse(r)
        assert 2015 <= l.year <= 2020 and l.price <= 25000 and l.odometer_km <= 120000 and l.state == "VIC"


async def test_falls_back_when_narrowed_path_not_taken():
    # CarsGuide didn't apply the year filter (filters still null): use the plain state path instead.
    site = FakeSite({"/buy-a-car/toyota/corolla/vic/over-2015": P1, "/buy-a-car/toyota/corolla/vic": P1})
    plugin, http = make(site, max_pages=3)
    await collect(plugin, http, make="Toyota", model="Corolla", states=["VIC"], year_min=2015)
    assert site.pages[:2] == ["/buy-a-car/toyota/corolla/vic/over-2015", "/buy-a-car/toyota/corolla/vic"]


async def test_redirect_elsewhere_is_not_accepted():
    # A path CarsGuide doesn't understand redirects to all cars: that's not our search.
    site = FakeSite({"/buy-a-car/toyota/corolla/nsw": httpx.Response(
        302, headers={"location": "https://www.carsguide.com.au/buy-a-car"}), "/buy-a-car": P1})
    plugin, http = make(site)
    with pytest.raises(SourceUnavailable, match="no page for Toyota Corolla"):
        await collect(plugin, http, make="Toyota", model="Corolla", states=["NSW"])


async def test_one_path_per_state_and_layout_change():
    site = FakeSite({"/buy-a-car/toyota/corolla/vic": P1, "/buy-a-car/toyota/corolla/qld": P2})
    plugin, http = make(site, max_pages=2)
    out = await collect(plugin, http, make="Toyota", model="Corolla", states=["VIC", "QLD"])
    assert site.pages == ["/buy-a-car/toyota/corolla/vic", "/buy-a-car/toyota/corolla/qld"]
    assert out and {plugin.parse(r).state for r in out} <= {"VIC", "QLD"}
    plugin2, http2 = make(FakeSite({"/buy-a-car/toyota/corolla": "<html>new</html>"}))
    with pytest.raises(SourceChanged):
        await collect(plugin2, http2, make="Toyota", model="Corolla")


async def test_confirm_gone():
    url = "https://www.carsguide.com.au/car/1/toyota/corolla/vic/x/sedan"
    removed = FakeSite({"/car/1/toyota/corolla/vic/x/sedan": httpx.Response(
        302, headers={"location": "https://www.carsguide.com.au/buy-a-car?removed=true"})})
    plugin, http = make(removed)
    assert await plugin.confirm_gone(KnownListing("1", url), http) is True
    live = FakeSite({"/car/1/toyota/corolla/vic/x/sedan": "<html>a car</html>"})
    plugin, http = make(live)
    assert await plugin.confirm_gone(KnownListing("1", url), http) is False


def test_paths_respect_robots():
    robots = Robots.parse(ROBOTS)
    plugin, _ = make(FakeSite({}))
    paths = plugin.paths(SearchParams(make="toyota", model="corolla", states=["VIC", "NSW"],
                                      year_min=2015, year_max=2020, price_max=25000), "corolla")
    assert [p for p, _ in paths] == ["/buy-a-car/toyota/corolla/vic/2015-2020/under-$25000",
                                     "/buy-a-car/toyota/corolla/nsw/2015-2020/under-$25000"]
    for p, _ in paths:
        assert robots.can_fetch(UA, "https://www.carsguide.com.au" + p + "?page=3")
    assert not robots.can_fetch(UA, "https://www.carsguide.com.au/buy-a-car/toyota/corolla?year_from=2015")
    assert plugin.year_segment(2015, None)[0] == "over-2015" and plugin.year_segment(None, 2009)[0] == "under-2009"
