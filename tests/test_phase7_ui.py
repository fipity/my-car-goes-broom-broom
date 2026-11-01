"""Phase 7: filters, radius search, saved-search changes, editing, price chart, sources page."""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from carscout.models import Listing, PriceHistory, SavedSearch, SearchListing, Valuation


@pytest.fixture()
def client(sm):
    from carscout.web.app import create_app

    with TestClient(create_app()) as c:
        yield c


def _l(**kw) -> Listing:
    base = dict(source="carsales", url="https://x", title="t", make="Toyota", model="Corolla", year=2019,
                odometer_km=80000, price=20000, price_type="egc", state="VIC", images=[], is_active=True)
    base.update(kw)
    base.setdefault("title", f"{base['year']} {base['make']} {base['model']} {base.get('variant', '')}")
    return Listing(**base)


def _ids(client, query: str) -> set[str]:
    items = client.get(f"/api/listings?per_page=200&{query}").json()["items"]
    return {i["source_listing_id"] for i in items}


def test_filters(client, sm):
    with sm.begin() as s:
        s.add_all([
            _l(source_listing_id="zr-auto", variant="ZR", body_type="Hatch", transmission="Automatic",
               fuel_type="Petrol", seller_type="dealer", odometer_km=60000),
            _l(source_listing_id="sx-manual", variant="SX", body_type="Sedan", transmission="Manual",
               fuel_type="Petrol", seller_type="private", odometer_km=140000),
            _l(source_listing_id="hybrid", variant="Ascent Sport Hybrid", body_type="Hatch",
               transmission="Automatic", fuel_type="Hybrid", seller_type="dealer", odometer_km=30000),
        ])
        s.flush()
        ids = {l.source_listing_id: l.id for l in s.scalars(select(Listing))}
        s.add_all([Valuation(listing_id=ids["zr-auto"], label="Great deal", diff_pct=-20, n_comps=10, comp_ids=[], details={}),
                   Valuation(listing_id=ids["sx-manual"], label="Overpriced", diff_pct=25, n_comps=10, comp_ids=[], details={})])
    assert _ids(client, "variant=zr") == {"zr-auto"}
    assert _ids(client, "body_type=hatch") == {"zr-auto", "hybrid"}
    assert _ids(client, "transmission=Manual") == {"sx-manual"}
    assert _ids(client, "fuel_type=Hybrid") == {"hybrid"}
    assert _ids(client, "seller_type=private") == {"sx-manual"}
    assert _ids(client, "km_min=50000&km_max=100000") == {"zr-auto"}
    assert _ids(client, "deal=Great deal&deal=Good deal") == {"zr-auto"}
    # the HTML form takes the same filters and keeps "More filters" open when used
    html = client.get("/?deal=Great+deal&body_type=Hatch").text
    assert "1 car " in html and "2 set" in html


def test_radius_search(client, sm):
    from carscout.geo.postcodes import load_postcodes, locate_suburb, postcode_centre

    with sm.begin() as s:
        assert load_postcodes(s) > 18000
        assert load_postcodes(s) == 0  # only when empty
        pc, lat, lon = locate_suburb(s, "Altona", "VIC")
        assert pc == "3018"
        assert postcode_centre(s, "3000")[2] == "VIC"
        s.add_all([
            _l(source="cars24", source_listing_id="altona", suburb="Altona", lat=-37.8651, lon=144.82),
            _l(source="cars24", source_listing_id="brisbane", state="QLD", lat=-27.47, lon=153.02),
            _l(source="carsales", source_listing_id="vic-state-only"),            # no coordinates
            _l(source="carsales", source_listing_id="nsw-state-only", state="NSW"),
        ])
    near_melbourne = _ids(client, "postcode=3000&radius_km=50")
    assert near_melbourne == {"altona", "vic-state-only"}  # state-only listings: same state kept
    assert "brisbane" in _ids(client, "postcode=3000&radius_km=2000")
    first = client.get("/api/listings?postcode=3000&radius_km=2000&sort=distance").json()["items"][0]
    assert first["source_listing_id"] == "altona"
    assert "Postcode 9999 wasn't found" in client.get("/?postcode=9999&radius_km=50").text


def test_store_listing_fills_location_from_suburb(sm):
    from carscout.geo.postcodes import load_postcodes
    from carscout.worker.runner import enrich_location

    with sm.begin() as s:
        load_postcodes(s)
        v = enrich_location(s, {"suburb": "Altona", "state": "VIC", "lat": None, "lon": None, "postcode": None})
        assert v["postcode"] == "3018" and v["lat"] < -37
        given = enrich_location(s, {"suburb": "Altona", "state": "VIC", "lat": -1.0, "lon": 1.0, "postcode": None})
        assert given["lat"] == -1.0 and given["postcode"] == "3018"  # source coords win; postcode filled


def test_saved_search_changes_and_seen(client, sm):
    now = datetime.now(UTC)
    with sm.begin() as s:
        search = SavedSearch(name="Corolla", params={"make": "Toyota", "model": "Corolla"},
                             created_at=now - timedelta(days=10), seen_at=now - timedelta(days=2))
        s.add(search)
        old = _l(source_listing_id="old", first_seen=now - timedelta(days=5), price=18000)
        new = _l(source_listing_id="new", first_seen=now - timedelta(hours=3))
        gone = _l(source_listing_id="gone", first_seen=now - timedelta(hours=3), is_active=False)
        s.add_all([old, new, gone])
        s.flush()
        for l in (old, new, gone):
            s.add(SearchListing(saved_search_id=search.id, listing_id=l.id))
        s.add_all([PriceHistory(listing_id=old.id, price=21000, observed_at=now - timedelta(days=5)),
                   PriceHistory(listing_id=old.id, price=19500, observed_at=now - timedelta(days=1)),
                   PriceHistory(listing_id=old.id, price=18000, observed_at=now - timedelta(hours=5)),
                   PriceHistory(listing_id=new.id, price=20000, observed_at=now - timedelta(hours=3))])
        sid = search.id

    ch = client.get(f"/api/searches/{sid}/changes").json()
    assert [l["source_listing_id"] for l in ch["new"]] == ["new"]  # inactive one isn't shown
    [drop] = ch["price_drops"]
    assert (drop["listing"]["source_listing_id"], drop["was"], drop["now"]) == ("old", 21000, 18000)
    assert ch["active"] == 2

    page = client.get(f"/searches/{sid}").text  # defaults to the "new" tab
    assert 'aria-current="page">New' in page.replace("\n", "")
    drops = client.get(f"/searches/{sid}?view=drops").text
    assert "Dropped $3,000 from $21,000" in drops
    listing = client.get("/searches").text
    assert "1 new" in listing and "1 price drop" in listing

    client.post(f"/searches/{sid}/seen")
    ch = client.get(f"/api/searches/{sid}/changes").json()
    assert ch["new"] == [] and ch["price_drops"] == []
    assert "No new listings" in client.get(f"/searches/{sid}?view=new").text
    assert "Corolla" in client.get(f"/searches/{sid}?view=all").text


def test_edit_search_resets_tracking_only_when_criteria_change(client, sm):
    with sm.begin() as s:
        search = SavedSearch(name="A", params={"make": "Toyota"})
        l = _l(source_listing_id="x")
        s.add_all([search, l])
        s.flush()
        s.add(SearchListing(saved_search_id=search.id, listing_id=l.id))
        sid = search.id

    def links():
        with sm() as s:
            return s.scalar(select(func.count()).select_from(SearchListing))

    r = client.patch(f"/api/searches/{sid}", json={"name": "Renamed", "schedule_enabled": False})
    assert r.status_code == 200 and r.json()["name"] == "Renamed" and links() == 1
    r = client.patch(f"/api/searches/{sid}", json={"params": {"make": "Mazda", "model": "3"}})
    assert r.json()["params"]["make"] == "Mazda" and links() == 0
    assert client.patch(f"/api/searches/{sid}", json={"sources": ["nope"]}).status_code == 422

    # HTML edit form, with the new fields
    r = client.post(f"/searches/{sid}/edit", data={"name": "Hatch", "make": "Toyota", "model": "Corolla",
                                                   "body_type": "Hatch", "transmission": "Manual",
                                                   "price_min": "5,000", "states": ["VIC"], "sources": ["demo"]})
    assert "criteria changed" in r.text
    with sm() as s:
        s2 = s.get(SavedSearch, sid)
        assert s2.params == {"make": "Toyota", "model": "Corolla", "body_type": "Hatch", "transmission": "Manual",
                             "price_min": 5000, "states": ["VIC"], "max_results": 200}
        assert s2.sources == ["demo"] and not s2.schedule_enabled


def test_price_chart(client, sm):
    from carscout.web.charts import price_chart

    now = datetime.now(UTC)
    with sm.begin() as s:
        a = _l(source="carsales", source_listing_id="a")
        b = _l(source="demo", source_listing_id="b", is_active=False, last_seen=now - timedelta(days=1))
        single = _l(source="carsales", source_listing_id="single")
        s.add_all([a, b, single])
        s.flush()
        s.add_all([PriceHistory(listing_id=a.id, price=22000, observed_at=now - timedelta(days=6)),
                   PriceHistory(listing_id=a.id, price=20500, observed_at=now - timedelta(days=2)),
                   PriceHistory(listing_id=b.id, price=21000, observed_at=now - timedelta(days=4)),
                   PriceHistory(listing_id=single.id, price=15000, observed_at=now)])
        a.vehicle_id = b.vehicle_id = None
        hist = [(p, "carsales") for p in s.scalars(select(PriceHistory).where(PriceHistory.listing_id == a.id))]
        hist += [(p, "demo") for p in s.scalars(select(PriceHistory).where(PriceHistory.listing_id == b.id))]
        chart = price_chart(hist, [a, b], now=now)
        by = {sr.source: sr for sr in chart.series}
        assert set(by) == {"carsales", "demo"} and by["carsales"].slot != by["demo"].slot
        cs = by["carsales"]
        assert cs.path.startswith("M") and " H" in cs.path and " V" in cs.path  # a step line
        assert cs.end_x == chart.plot[2] and cs.end_price == 20500 and cs.active  # runs to "now"
        assert not by["demo"].active and by["demo"].end_x < chart.plot[2]
        lo_y, hi_y = chart.plot[1], chart.plot[3]
        assert all(lo_y <= p.y <= hi_y for sr in chart.series for p in sr.points)
        assert price_chart([(p, "carsales") for p in s.scalars(
            select(PriceHistory).where(PriceHistory.listing_id == single.id))], [single]) is None
        # two sites, same price, never changed: nothing worth a chart (the table says it all)
        flat = [(PriceHistory(price=28490, price_type="egc", observed_at=now), "carsales"),
                (PriceHistory(price=28490, price_type="egc", observed_at=now), "demo")]
        assert price_chart(flat, [a, b], now=now) is None
        # identical end prices get one label
        same_end = hist + [(PriceHistory(price=20500, price_type="egc", observed_at=now - timedelta(days=3)), "demo")]
        labels = [sr.show_label for sr in price_chart(same_end, [a, b], now=now).series]
        assert labels.count(True) == 1
        aid, sid = a.id, single.id

    html = client.get(f"/listings/{aid}").text
    assert '<svg class="chart"' in html and "$20,500" in html
    assert '<svg class="chart"' not in client.get(f"/listings/{sid}").text


def test_sources_page_counts(client, sm):
    with sm.begin() as s:
        s.add_all([_l(source="demo", source_listing_id=str(i)) for i in range(3)])
    html = client.get("/sources").text
    assert "3 active listings" in html
