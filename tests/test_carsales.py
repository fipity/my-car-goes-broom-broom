"""Carsales plugin against saved real search pages (tests/fixtures/carsales, captured 2026-11-01).
No network: a fake transport serves the fixtures."""
import gzip
import re
from pathlib import Path

import httpx
import pytest

from carscout.core.config import SourceConfig
from carscout.sources.base import RawListing, SearchParams, SourceBlocked, SourceChanged, SourceUnavailable
from carscout.sources.carsales import Plugin
from carscout.sources.http import PoliteClient
from carscout.sources.robots import Robots

FIX = Path(__file__).parent / "fixtures/carsales"
VIC = gzip.open(FIX / "search_used_toyota_corolla_vic.html.gz", "rt").read()
MERC = gzip.open(FIX / "search_used_mercedes_cclass.html.gz", "rt").read()
ALL_AU = gzip.open(FIX / "search_toyota_corolla.html.gz", "rt").read()
CHALLENGE = (FIX / "datadome_challenge.html").read_text()
ROBOTS = (FIX / "robots.txt").read_text()
HTML = {"content-type": "text/html; charset=utf-8"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"


class FakeSite:
    def __init__(self, routes: dict[str, str | httpx.Response]):
        self.routes = routes  # path prefix -> page html (or a Response)
        self.calls: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append(path)
        if path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        for prefix in sorted(self.routes, key=len, reverse=True):
            if path.startswith(prefix):
                r = self.routes[prefix]
                return r if isinstance(r, httpx.Response) else httpx.Response(200, text=r, headers=HTML)
        return httpx.Response(404, text="<html>not found</html>", headers=HTML)

    @property
    def pages(self) -> list[str]:
        return [c for c in self.calls if c != "/robots.txt"]


def make(site, **options):
    async def no_sleep(_s):
        pass

    cfg = SourceConfig(name="carsales", label="Carsales", delay=0, jitter=0, concurrency=1, options=options)
    http = PoliteClient("carsales", delay=0, jitter=0, transport=httpx.MockTransport(site), sleep=no_sleep)
    return Plugin(cfg), http


async def collect(plugin, http, **params) -> list[RawListing]:
    return [r async for r in plugin.search(SearchParams(**params), http)]


def with_total(html: str, total: int) -> str:
    return re.sub(r'"listingresultcount":\s*"\d+"', f'"listingresultcount":"{total}"', html)


async def test_state_search_with_splits():
    site = FakeSite({"/cars/used/toyota/corolla/victoria-state/": VIC})
    plugin, http = make(site)
    out = await collect(plugin, http, make="Toyota", model="Corolla", states=["VIC"])
    assert site.pages[0] == "/cars/used/toyota/corolla/victoria-state/"
    # 541 results don't fit on one page: split by the body styles / transmissions seen on it
    assert "/cars/used/toyota/corolla/victoria-state/hatch-bodystyle/" in site.pages
    assert "/cars/used/toyota/corolla/victoria-state/automatic-transmission/" in site.pages
    assert not any("ute-bodystyle" in p for p in site.pages)  # never seen on a Corolla page
    assert len(site.pages) <= 8 and plugin.truncated
    assert len(out) == 25 and len({r.source_listing_id for r in out}) == 25  # same fixture: deduped


async def test_parse_card():
    site = FakeSite({"/cars/used/toyota/corolla/victoria-state/": VIC})
    plugin, http = make(site, max_pages=1)
    out = {r.source_listing_id: r for r in await collect(plugin, http, make="Toyota", model="Corolla", states=["VIC"])}
    l = plugin.parse(out["OAG-AD-26427426"])
    assert l.title == "2020 Toyota Corolla Ascent Sport Hybrid"
    assert (l.make, l.model, l.variant, l.year) == ("Toyota", "Corolla", "Ascent Sport Hybrid", 2020)
    assert (l.odometer_km, l.price, l.price_type) == (57000, 27990, "drive_away")
    assert (l.body_type, l.transmission, l.fuel_type, l.drivetrain) == ("Hatch", "Automatic", "Hybrid", "FWD")
    assert l.engine == "4cyl 1.8L Hybrid" and (l.seller_type, l.state) == ("dealer", "VIC")
    assert l.url == "https://www.carsales.com.au/cars/details/2020-toyota-corolla-ascent-sport-hybrid-auto/OAG-AD-26427426/"
    assert len(l.images) == 5 and l.images[0].startswith("https://carsales.pxcrush.net/")

    gr = plugin.parse(out["OAG-AD-26176615"])  # "GR GTS Manual GR-FOUR"
    assert (gr.variant, gr.transmission, gr.drivetrain) == ("GR GTS", "Manual", "AWD")
    private = plugin.parse(out["SSE-AD-20660414"])
    assert (private.seller_type, private.price_type) == ("private", "egc")


async def test_payload_is_compact_and_stable():
    site = FakeSite({"/cars/used/toyota/corolla/victoria-state/": VIC})
    plugin, http = make(site, max_pages=1)
    first = await collect(plugin, http, make="Toyota", model="Corolla", states=["VIC"])
    p = first[0].payload
    assert set(p["meta"]) <= {"networkId", "make", "model", "year", "price", "state", "adtype",
                              "sellerId", "type", "categorytype", "listingBadges"}
    assert "Series" in p["ld"]["name"] or p["ld"]["name"].startswith("20")  # JSON-LD kept
    assert len(str(p)) < 6000
    # Another run of the same page gives identical payloads, so raw dedupe works.
    site2 = FakeSite({"/cars/used/toyota/corolla/victoria-state/": VIC})
    plugin2, http2 = make(site2, max_pages=1)
    again = await collect(plugin2, http2, make="Toyota", model="Corolla", states=["VIC"])
    assert [r.payload for r in again] == [r.payload for r in first]


async def test_mercedes_naming_and_national_state_splits():
    site = FakeSite({"/cars/used/mercedes-benz/c-class/": MERC})
    plugin, http = make(site)
    out = await collect(plugin, http, make="Mercedes", model="C-Class")
    l = plugin.parse(out[0])
    assert l.make == "Mercedes-Benz" and l.model == "C-Class" and l.variant.startswith("C")
    # national search: split by state first, busiest states first, within the cap
    assert site.pages[1].endswith("-state/") and len(site.pages) == 8


async def test_client_side_filters():
    site = FakeSite({"/cars/used/toyota/corolla/victoria-state/": VIC})
    plugin, http = make(site, max_pages=1)
    out = await collect(plugin, http, make="Toyota", model="Corolla", states=["VIC"],
                        year_min=2019, km_max=60000, price_max=30000)
    assert out
    for r in out:
        l = plugin.parse(r)
        assert l.year >= 2019 and l.odometer_km <= 60000 and l.price <= 30000


async def test_body_and_transmission_go_in_the_path():
    site = FakeSite({"/cars/used/toyota/corolla/": VIC})
    plugin, http = make(site, max_pages=1)
    await collect(plugin, http, make="Toyota", model="Corolla", states=["VIC"],
                  body_type="hatchback", transmission="auto")
    assert site.pages == ["/cars/used/toyota/corolla/victoria-state/hatch-bodystyle/automatic-transmission/"]


async def test_complete_page_is_not_truncated():
    from carscout.sources.carsales import Page
    small = with_total(VIC, Page(VIC).organic)  # every result fits on page 1
    site = FakeSite({"/cars/used/toyota/corolla/victoria-state/": small})
    plugin, http = make(site)
    await collect(plugin, http, make="Toyota", model="Corolla", states=["VIC"])
    assert site.pages == ["/cars/used/toyota/corolla/victoria-state/"]  # nothing to split
    assert not plugin.truncated  # so the runner's gone check can run


async def test_unknown_badge_is_dropped_and_unknown_model_reported():
    site = FakeSite({"/cars/used/toyota/corolla/victoria-state/": VIC})
    plugin, http = make(site, max_pages=1)
    # /cars/used/toyota/corolla/zzz-badge/... 404s -> retried without the badge
    out = await collect(plugin, http, make="Toyota", model="Corolla", states=["VIC"], variant="ZZZ")
    assert "/cars/used/toyota/corolla/zzz-badge/victoria-state/" in site.pages and out == []

    site2 = FakeSite({})
    plugin2, http2 = make(site2)
    with pytest.raises(SourceUnavailable, match="no page for Toyota Zzzz"):
        await collect(plugin2, http2, make="Toyota", model="Zzzz")


async def test_hyphenless_model_slug_fallback():
    site = FakeSite({"/cars/used/mazda/cx5/": with_total(VIC, 3)})
    plugin, http = make(site)
    await collect(plugin, http, make="Mazda", model="CX-5")
    assert site.pages[:2] == ["/cars/used/mazda/cx-5/", "/cars/used/mazda/cx5/"]


async def test_datadome_challenge_is_blocked_not_retried():
    site = FakeSite({"/cars/used/toyota/corolla/": httpx.Response(403, text=CHALLENGE, headers=HTML)})
    plugin, http = make(site)
    with pytest.raises(SourceBlocked, match="captcha-delivery"):
        await collect(plugin, http, make="Toyota", model="Corolla")
    assert site.pages == ["/cars/used/toyota/corolla/"]  # one request, no hammering


async def test_layout_change_reported():
    site = FakeSite({"/cars/used/toyota/corolla/": "<html><body>new design</body></html>"})
    plugin, http = make(site)
    with pytest.raises(SourceChanged):
        await collect(plugin, http, make="Toyota", model="Corolla")


def test_all_paths_we_build_are_allowed_by_robots():
    robots = Robots.parse(ROBOTS)
    plugin, _ = make(FakeSite({}))
    paths = [plugin.build_path("Toyota", "Corolla", {"state": "victoria", "bodystyle": "hatch"}),
             plugin.build_path("Mercedes-Benz", "C-Class", {}), plugin.build_path(None, None, {})]
    for p in paths:
        assert robots.can_fetch(UA, "https://www.carsales.com.au" + p), p
    # and the ones we avoid really are off-limits
    assert not robots.can_fetch(UA, "https://www.carsales.com.au/cars/?q=x")
    assert not robots.can_fetch(UA, "https://www.carsales.com.au/cars/toyota/corolla/?sort=Price")


def test_all_au_page_parses():
    # The unprefixed /cars/toyota/corolla/ page (new + used) uses the same structure.
    from carscout.sources.carsales import Page
    page = Page(ALL_AU)
    assert page.total == 1977 and len(page.cards) == 29 and page.organic == 21 and len(page.ld) >= 20
