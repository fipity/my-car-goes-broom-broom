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
    assert "5 listings" in html and "Mazda 3" in html
    assert client.get("/listings/1").status_code == 200
    assert client.get("/listings/424242").status_code == 404
