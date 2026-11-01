"""Cross-source dedupe: matching rules (pure) and the vehicles table (DB)."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from carscout.dedupe.match import Car, MatchConfig, cluster, compare

VIN = "JTNKE3BE803503547"
# The same 2022 Corolla as Cars24 and Carsales would describe it.
C24 = Car(id=1, source="cars24", make="Toyota", model="Corolla", variant="Ascent Sport Hybrid", year=2022,
          odometer_km=50735, price=26190, state="VIC", body_type="Hatch", transmission="Automatic",
          fuel_type="Hybrid", drivetrain="FWD", colour="Jasper Red", vin=VIN)
CS = Car(id=2, source="carsales", make="Toyota", model="Corolla", variant="Ascent Sport Hybrid", year=2022,
         odometer_km=50735, price=26990, state="VIC", body_type="Hatch", transmission="Automatic",
         fuel_type="Hybrid", drivetrain="FWD")


def test_fuzzy_match_across_sources():
    m = compare(C24, CS)
    assert m and m.method == "fuzzy" and m.score > 0.9


def test_vin_decides_both_ways():
    assert compare(C24, replace(CS, vin=VIN, odometer_km=None, source="cars24")).method == "vin"
    other = "JTNKE3BE803509999"
    assert compare(C24, replace(CS, vin=other)) is None  # different VINs: never the same car


def test_rego_match():
    a = replace(C24, vin=None, rego="1ABC234")
    b = replace(CS, rego="1abc-234", odometer_km=None)
    assert compare(a, b).method == "rego"
    assert compare(a, replace(b, state="NSW")) is None


def test_known_fields_must_agree():
    for change in ({"state": "NSW"}, {"body_type": "Sedan"}, {"transmission": "Manual"},
                   {"model": "Corolla Cross"}, {"colour": "White"}, {"fuel_type": "Petrol"}):
        assert compare(C24, replace(CS, **change)) is None, change
    # unknown on one side is fine
    assert compare(C24, replace(CS, body_type=None, drivetrain=None, state=None))


def test_tolerances():
    assert compare(C24, replace(CS, odometer_km=50735 + 900))           # small km drift: ok
    assert compare(C24, replace(CS, odometer_km=50735 + 5000)) is None  # different car
    assert compare(C24, replace(CS, price=int(26190 * 1.3))) is None
    assert compare(C24, replace(CS, year=2021))                          # build vs model year
    assert compare(C24, replace(CS, year=2019)) is None
    assert compare(C24, replace(CS, odometer_km=None)) is None           # no km: no fuzzy match


def test_same_source_never_fuzzy():
    assert compare(C24, replace(CS, source="cars24")) is None


def test_weak_evidence_below_threshold():
    # km within tolerance but at its edge, different variant, price apart: not convincing
    b = replace(CS, odometer_km=50735 + 1900, variant="ZR", price=29500, year=2021)
    assert compare(C24, b) is None
    assert compare(C24, b, MatchConfig(threshold=0.1))  # threshold is tunable


def test_complete_linkage_prevents_chaining():
    # B (colour unknown) matches A (white) and C (red), but A and C can't be the same car:
    # B joins one of them, never both.
    a = replace(CS, id=10, source="carsales", colour="White")
    b = replace(CS, id=11, source="cars24", colour=None, odometer_km=CS.odometer_km + 100)
    c = replace(CS, id=12, source="drive", colour="Red")
    assert compare(a, b) and compare(b, c) and not compare(a, c)
    assert sorted(len(g.cars) for g in cluster([a, b, c])) == [1, 2]


def test_cluster_method_and_canonical():
    seen = datetime.now(UTC)
    a = replace(C24, last_seen=seen)
    b = replace(CS, last_seen=seen + timedelta(hours=1))
    [g] = cluster([a, b])
    assert g.method == "fuzzy" and 0.9 < g.confidence <= 1 and g.vin == VIN
    assert g.canonical().id == 1  # more complete (VIN, colour) beats fresher
    [g] = cluster([replace(a, is_active=False), b])
    assert g.canonical().id == 2  # an active listing beats a sold one


def test_candidate_pairs_skip_far_apart_odometers():
    from carscout.dedupe.match import candidate_pairs

    cars = [replace(CS, id=i, odometer_km=i * 10_000, source=f"s{i}") for i in range(200)]
    pairs = candidate_pairs(cars, MatchConfig())
    assert len(pairs) < 400  # not 19,900


# ---- database ------------------------------------------------------------------------------
def _listing(**kw):
    from carscout.models import Listing

    base = dict(source="cars24", source_listing_id="x", url="https://x", title="t", make="Toyota",
                model="Corolla", variant="Ascent Sport Hybrid", year=2022, odometer_km=50735,
                price=26190, price_type="egc", state="VIC", body_type="Hatch",
                transmission="Automatic", images=[], is_active=True)
    base.update(kw)
    return Listing(**base)


def test_store_assigns_vehicles_and_keeps_ids(sm):
    from carscout.dedupe.store import dedupe
    from carscout.models import Listing, Vehicle

    with sm.begin() as s:
        s.add_all([
            _listing(source_listing_id="a", vin=VIN, colour="Jasper Red"),
            _listing(source="carsales", source_listing_id="OAG-AD-1", price=26990),
            _listing(source="carsales", source_listing_id="OAG-AD-2", odometer_km=120000, price=15000, year=2016),
            _listing(source="carsales", source_listing_id="OAG-AD-3", make="Mazda", model="3"),
        ])
    with sm.begin() as s:
        st = dedupe(s)
    assert st == {"listings": 4, "vehicles": 3, "multi": 1}
    with sm() as s:
        a, b = (s.scalars(select(Listing).where(Listing.source_listing_id == x)).one() for x in ("a", "OAG-AD-1"))
        assert a.vehicle_id == b.vehicle_id
        v = s.get(Vehicle, a.vehicle_id)
        assert (v.match_method, v.vin, v.canonical_listing_id) == ("fuzzy", VIN, a.id)
        first_vid = a.vehicle_id

    # Re-running keeps ids. A listing whose km changes enough splits off into its own vehicle.
    with sm.begin() as s:
        dedupe(s)
        s.scalars(select(Listing).where(Listing.source_listing_id == "OAG-AD-1")).one().odometer_km = 70000
    with sm.begin() as s:
        dedupe(s, [("Toyota", "Corolla")])
    with sm() as s:
        a, b = (s.scalars(select(Listing).where(Listing.source_listing_id == x)).one() for x in ("a", "OAG-AD-1"))
        assert a.vehicle_id == first_vid and b.vehicle_id != first_vid
        assert s.scalar(select(func.count(Vehicle.id))) == 4
        assert s.get(Vehicle, b.vehicle_id).match_method == "single"


def test_orphan_vehicles_removed(sm):
    from carscout.dedupe.store import dedupe
    from carscout.models import Listing, Vehicle

    with sm.begin() as s:
        s.add_all([_listing(source_listing_id="a"), _listing(source="carsales", source_listing_id="b")])
    with sm.begin() as s:
        dedupe(s)
    with sm.begin() as s:
        assert s.scalar(select(func.count(Vehicle.id))) == 1
        s.add(_listing(source="carsales", source_listing_id="c", odometer_km=10))
    with sm.begin() as s:
        s.execute(Listing.__table__.delete().where(Listing.source_listing_id.in_(["a", "b"])))
        dedupe(s)
    with sm() as s:
        assert s.scalar(select(func.count(Vehicle.id))) == 1  # only c's vehicle is left


async def test_runner_dedupes_after_run(sm):
    from carscout.models import Listing, Vehicle
    from tests.test_runner import _queue_and_run, _setup
    from carscout.worker.runner import execute_run

    _setup(sm)
    [r1] = _queue_and_run(sm)
    await execute_run(sm, r1)
    with sm() as s:
        assert s.scalar(select(func.count(Listing.id)).where(Listing.vehicle_id.is_(None))) == 0
        assert s.scalar(select(func.count(Vehicle.id))) == 5  # demo listings are 5 different cars
