"""API + pages through FastAPI's TestClient against the test DB."""
import pytest
from fastapi.testclient import TestClient

from carscout.worker.runner import execute_run


@pytest.fixture()
def client(sm):
    from carscout.core.db import get_session
    from carscout.web.app import create_app

    app = create_app()

    def session_override():
        with sm() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    return TestClient(app)


async def test_full_flow_via_api(client, sm):
    r = client.get("/api/sources")
    assert r.status_code == 200
    by_name = {s["name"]: s for s in r.json()["sources"]}
    assert by_name["demo"]["state"] == "off" and by_name["carsales"]["state"] == "not_built"
    assert r.json()["next_scheduled_run"]

    assert client.patch("/api/sources/carsales", json={"enabled": True}).status_code == 409
    assert client.patch("/api/sources/demo", json={"enabled": True}).json()["enabled"] is True

    r = client.post("/api/searches", json={"name": "Corolla", "params": {"make": "Toyota",
                                           "model": "Corolla", "year_min": 2015, "bogus": 1}})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert r.json()["params"] == {"make": "Toyota", "model": "Corolla", "year_min": 2015,
                                  "max_results": 200}

    runs = client.post(f"/api/searches/{sid}/run").json()
    assert [r["source"] for r in runs] == ["demo"] and runs[0]["status"] == "queued"
    await execute_run(sm, runs[0]["id"])

    page = client.get("/api/listings", params={"make": "toyota", "sort": "price_asc"}).json()
    assert page["total"] == 5
    prices = [i["price"] for i in page["items"]]
    assert prices == sorted(prices)
    assert all(i["year"] >= 2015 for i in page["items"])

    lid = page["items"][0]["id"]
    detail = client.get(f"/api/listings/{lid}").json()
    assert len(detail["price_history"]) == 1
    assert client.get("/api/listings/999999").status_code == 404

    runs = client.get("/api/runs").json()
    assert runs[0]["status"] == "ok" and runs[0]["found"] == 5


async def test_pages_render(client, sm):
    for path in ("/", "/searches", "/sources"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert "No listings yet" in client.get("/").text

    # form flow: toggle demo on, save a search, run it
    assert client.post("/sources/demo/toggle", follow_redirects=False).status_code == 303
    r = client.post("/searches", data={"name": "Mazda 3", "make": "Mazda", "model": "3",
                                       "km_max": "120,000", "states": ["VIC", "NSW"],
                                       "schedule_enabled": "1"})
    assert r.status_code == 200 and "Saved" in r.text
    r = client.post("/searches/1/run")
    assert "Queued 1 source(s): demo" in r.text
    from sqlalchemy import select

    from carscout.models import ScrapeRun
    with sm() as s:
        run_id = s.scalars(select(ScrapeRun.id)).one()
    await execute_run(sm, run_id)

    html = client.get("/?sort=price_desc&price_max=abc").text  # junk number ignored
    assert "5 cars" in html and "Mazda 3" in html
    assert client.get("/listings/1").status_code == 200
    assert client.get("/listings/424242").status_code == 404


def test_same_car_shown_once_with_both_sources(client, sm):
    from carscout.dedupe.store import dedupe
    from carscout.models import Listing, PriceHistory

    def listing(source, sid, price):
        return Listing(source=source, source_listing_id=sid, url=f"https://{source}.test/{sid}",
                       title="2022 Toyota Corolla Ascent Sport Hybrid", make="Toyota", model="Corolla",
                       variant="Ascent Sport Hybrid", year=2022, odometer_km=50735, price=price,
                       price_type="egc", state="VIC", images=[], is_active=True)

    with sm.begin() as s:
        a, b = listing("cars24", "1", 26190), listing("carsales", "OAG-AD-1", 26990)
        a.vin = "JTNKE3BE803503547"
        s.add_all([a, b])
        s.flush()
        s.add_all([PriceHistory(listing_id=a.id, price=26190, price_type="egc"),
                   PriceHistory(listing_id=b.id, price=26990, price_type="egc")])
    with sm.begin() as s:
        dedupe(s)

    page = client.get("/api/listings").json()
    assert page["total"] == 1 and len(page["items"]) == 1
    item = page["items"][0]
    assert item["source"] == "cars24"  # canonical: has the VIN
    assert [o["source"] for o in item["same_car"]] == ["carsales"]
    detail = client.get(f"/api/listings/{item['id']}").json()
    assert detail["match_method"] == "fuzzy" and len(detail["car_price_history"]) == 2

    html = client.get("/").text
    assert "1 car " in html and "Also on" in html
    only_carsales = client.get("/api/listings?source=carsales").json()["items"]
    assert [i["source"] for i in only_carsales] == ["carsales"]  # filter picks that site's listing
    detail_html = client.get(f"/listings/{item['id']}").text
    assert "Same car on other listings" in detail_html and "carsales.test/OAG-AD-1" in detail_html
