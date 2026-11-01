"""Valuation: synthetic markets with a known price formula, so estimates can be checked against
the truth. Plus junk detection, config loading and the database step."""
import math
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from carscout.valuation.config import JunkConfig, ValuationConfig, load_valuation_config
from carscout.valuation.junk import junk_reason
from carscout.valuation.model import Item, deal_label, fit_group, select_comps, value, weighted_quantile

REF_YEAR = 2026
CFG = load_valuation_config(Path(__file__).parents[1] / "config/valuation.yaml")


def true_price(year: int, km: int, variant: str = "Ascent Sport", manual: bool = False) -> float:
    age = REF_YEAR - year + 0.5
    p = 42000 * math.exp(-0.09 * age) * math.exp(-0.035 * km / 10000)
    p *= {"Ascent Sport": 1.0, "SX": 1.08, "ZR": 1.18}[variant]
    return p * (0.93 if manual else 1.0)


def market(n: int, seed: int = 1, noise: float = 0.05) -> list[Item]:
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        year = rnd.randint(2014, 2024)
        km = max(5000, int(rnd.gauss((REF_YEAR - year) * 14000, 15000)))
        variant = rnd.choice(["Ascent Sport", "Ascent Sport", "SX", "ZR"])
        manual = rnd.random() < 0.15
        price = int(true_price(year, km, variant, manual) * math.exp(rnd.gauss(0, noise)))
        out.append(Item(id=i + 1, vehicle_id=i + 1, make="Toyota", model="Corolla", variant=variant,
                        year=year, odometer_km=km, price=price, price_type="egc", state="VIC",
                        transmission="Manual" if manual else "Automatic", body_type="Hatch",
                        seller_type=rnd.choice(["dealer", "private"])))
    return out


def target(price_factor: float = 1.0, **kw) -> Item:
    base = dict(id=10_000, vehicle_id=10_000, make="Toyota", model="Corolla", variant="SX", year=2020,
                odometer_km=80000, price_type="egc", state="VIC", transmission="Automatic",
                body_type="Hatch", seller_type="dealer")
    base.update(kw)
    t = Item(**base)
    truth = true_price(t.year, t.odometer_km, t.variant, t.transmission == "Manual")
    return replace(t, price=int(truth * price_factor))


def test_regression_recovers_the_market():
    pool = market(150)
    group = fit_group(pool, CFG, REF_YEAR)
    assert group and group.r2 > 0.9
    t = target()
    r = value(t, pool, CFG, group)
    assert r.method == "regression"
    assert abs(r.estimate - t.price) / t.price < 0.05
    assert r.p25 < r.estimate < r.p75
    assert -12 < r.details["per_year_pct"] < -6 and -5 < r.details["per_10k_km_pct"] < -2
    assert r.confidence in ("High", "Medium") and r.n_comps >= CFG.min_comps
    assert r.details["comps_estimate"] and r.details["estimates_gap_pct"] < 15


@pytest.mark.parametrize("factor,label", [(0.80, "Great deal"), (0.90, "Good deal"), (1.0, "Fair"),
                                          (1.10, "Above market"), (1.25, "Overpriced")])
def test_deal_labels(factor, label):
    pool = market(150)
    r = value(target(factor), pool, CFG, fit_group(pool, CFG, REF_YEAR))
    assert r.label == label, (r.diff_pct, r.estimate)
    assert (r.diff_abs < 0) == (factor < 1) or factor == 1.0


def test_label_thresholds_configurable():
    cfg = ValuationConfig.model_validate(
        {**CFG.model_dump(), "labels": [{"label": "Cheap", "below": 0}, {"label": "Dear", "below": None}]})
    assert deal_label(-0.1, cfg) == "Cheap" and deal_label(3, cfg) == "Dear"


def test_small_group_uses_comps_median():
    pool = market(25, seed=3)  # under regression.min_listings
    assert fit_group(pool, CFG, REF_YEAR) is None
    t = target(year=2019, odometer_km=95000)
    r = value(t, pool, CFG, None)
    assert r.method == "comps" and abs(r.estimate - t.price) / t.price < 0.15


def test_not_enough_data():
    pool = market(2, seed=4)
    r = value(target(), pool, CFG, None)
    assert r.method == "insufficient" and r.estimate is None and r.label is None and "comparable" in r.details["reason"]


def test_tiers_widen_when_needed():
    pool = market(150)
    t = target()
    tier, comps, _ = select_comps(t, pool, CFG)
    assert tier == "close" and all(c.variant == "SX" for c, _ in comps)
    # a variant nobody else has: the same-variant tier can't fill, so criteria widen
    tier, comps, _ = select_comps(replace(t, variant="Hybrid Limited Edition"), pool, CFG)
    assert tier == "specs" and len(comps) >= CFG.min_comps
    # a year far outside the data: only the widest tier (if anything) applies
    tier, _, _ = select_comps(replace(t, year=2010, odometer_km=250000), pool, CFG)
    assert tier == "wide"


def test_outliers_and_own_car_are_excluded():
    pool = market(150)
    t = target()
    tier, comps, _ = select_comps(t, pool, CFG)
    victims = [c for c, _ in comps[:3]]
    crazy = [replace(c, id=900 + n, vehicle_id=900 + n, price=c.price * 4) for n, c in enumerate(victims)]
    twin = replace(t, id=10_001)  # same vehicle_id: the target's own listing on another site
    _, comps2, removed = select_comps(t, pool + crazy + [twin], CFG)
    ids = {c.id for c, _ in comps2}
    assert removed >= 3 and not ids & {c.id for c in crazy} and 10_001 not in ids


def test_drive_away_prices_compared_on_one_basis():
    pool = market(150)
    group = fit_group(pool, CFG, REF_YEAR)
    t = target()
    dap = replace(t, price=round(t.price / CFG.egc_factor("VIC")), price_type="drive_away")
    r = value(dap, pool, CFG, group)
    assert r.label == "Fair" and r.details["price_basis"] == "drive-away"
    assert r.estimate > value(t, pool, CFG, group).estimate  # estimate shown on the drive-away basis


def test_confidence_reflects_data():
    tight = market(150, noise=0.02)
    r = value(target(), tight, CFG, fit_group(tight, CFG, REF_YEAR))
    assert r.confidence == "High"
    noisy = market(12, seed=5, noise=0.30)
    r = value(target(), noisy, CFG, None)
    assert r.confidence == "Low"


def test_weighted_quantile():
    assert weighted_quantile([10, 20, 30], [1, 1, 1], 0.5) == 20
    assert weighted_quantile([10, 20, 30], [1, 1, 10], 0.5) > 25  # heavy weight pulls it up
    assert weighted_quantile([5], [1], 0.25) == 5


# ---- junk ------------------------------------------------------------------------------------
@pytest.mark.parametrize("price,text,reason", [
    (None, "2018 Corolla", "no_price"),
    (1, "2018 Corolla", "low_price"),
    (8000, "2015 Corolla repairable write-off", "write_off"),
    (8000, "Statutory write off, WOVR listed", "write_off"),
    (3000, "Wrecking 2012 Corolla, all parts available", "wrecking"),
    (2500, "Not running, for parts", "parts_only"),
    (9000, "Corolla for rent, uber ready", "rental"),
    (26190, "Cars24 history check: No stolen record; No outstanding loan; No write offs on record; "
            "No major accidental damage.", None),
    (15000, "Never been written off, one owner", None),
    (15000, "2019 Toyota Corolla Ascent Sport", None),
])
def test_junk(price, text, reason):
    assert junk_reason(price, [text], CFG.junk) == reason


def test_repo_config_is_valid():
    cfg = load_valuation_config(Path(__file__).parents[1] / "config/valuation.yaml")
    assert [r.label for r in cfg.labels][0] == "Great deal" and cfg.egc_factor("VIC") < 1
    assert cfg.egc_factor("TAS") == cfg.drive_away_to_egc["default"]
    assert load_valuation_config(Path("/nonexistent.yaml")).min_comps == ValuationConfig().min_comps


# ---- database ---------------------------------------------------------------------------------
def test_revalue_writes_valuations(sm):
    from carscout.dedupe.store import dedupe
    from carscout.models import Listing, Valuation
    from carscout.valuation.store import revalue

    items = market(60, seed=7)
    with sm.begin() as s:
        for it in items:
            s.add(Listing(source="carsales", source_listing_id=str(it.id), url=f"https://x/{it.id}",
                          title=f"{it.year} Toyota Corolla {it.variant}", make="Toyota", model="Corolla",
                          variant=it.variant, year=it.year, odometer_km=it.odometer_km, price=it.price,
                          price_type="egc", state="VIC", transmission=it.transmission, body_type="Hatch",
                          seller_type=it.seller_type, images=[], is_active=True))
        s.add(Listing(source="carsales", source_listing_id="junk", url="https://x/j", title="Wrecking Corolla",
                      make="Toyota", model="Corolla", year=2015, odometer_km=150000, price=1500,
                      price_type="egc", images=[], is_active=True))
        old = datetime.now(UTC) - timedelta(days=400)
        s.add(Listing(source="carsales", source_listing_id="ancient", url="https://x/a", title="old sale",
                      make="Toyota", model="Corolla", year=2020, odometer_km=80000, price=99999,
                      price_type="egc", images=[], is_active=False, first_seen=old, last_seen=old))
    with sm.begin() as s:
        dedupe(s)
        st = revalue(s)
    assert st["regression"] >= 55 and st["excluded"] == 1
    with sm() as s:
        junk = s.scalars(select(Listing).where(Listing.source_listing_id == "junk")).one()
        assert junk.excluded_reason == "wrecking" and junk.valuation is None
        ancient = s.scalars(select(Listing).where(Listing.source_listing_id == "ancient")).one()
        v = s.scalars(select(Valuation).limit(1)).one()
        assert v.label and v.confidence and v.estimate and v.comp_ids
        all_comp_ids = {i for row in s.scalars(select(Valuation.comp_ids)) for i in row}
        assert ancient.id not in all_comp_ids and junk.id not in all_comp_ids  # stale sale / junk unused
        labels = dict(s.execute(select(Valuation.label, func.count()).group_by(Valuation.label)).all())
        assert labels.get("Fair", 0) > labels.get("Great deal", 0)  # an honest market is mostly fair


async def test_runner_revalues_after_run(sm):
    from carscout.models import Valuation
    from carscout.worker.runner import execute_run
    from tests.test_runner import _queue_and_run, _setup

    _setup(sm)
    [r1] = _queue_and_run(sm)
    await execute_run(sm, r1)
    with sm() as s:
        assert s.scalar(select(func.count(Valuation.listing_id))) == 5  # demo: 5 rows (comps or insufficient)


def test_demo_listings_never_comparables(sm):
    from carscout.models import Listing, Valuation
    from carscout.valuation.store import revalue

    with sm.begin() as s:
        for it in market(40, seed=11):
            for source in ("carsales", "demo"):
                s.add(Listing(source=source, source_listing_id=f"{source}{it.id}", url="https://x",
                              title="t", make="Toyota", model="Corolla", variant=it.variant, year=it.year,
                              odometer_km=it.odometer_km, price=it.price * (5 if source == "demo" else 1),
                              price_type="egc", state="VIC", transmission=it.transmission,
                              body_type="Hatch", seller_type=it.seller_type, images=[], is_active=True))
    with sm.begin() as s:
        revalue(s)
    with sm() as s:
        demo_ids = set(s.scalars(select(Listing.id).where(Listing.source == "demo")))
        used = {i for row in s.scalars(select(Valuation.comp_ids)) for i in row}
        assert used and not used & demo_ids
        # demo listings priced 5x get rated against real cars
        demo_labels = set(s.scalars(select(Valuation.label).where(Valuation.listing_id.in_(demo_ids))))
        assert demo_labels == {"Overpriced"}


def test_range_widens_where_prices_scatter():
    # Old cars scatter much more than new ones; the range for an old car must reflect that.
    rnd = random.Random(2)
    pool = []
    for i, it in enumerate(market(200, seed=12)):
        noise = 0.25 if it.year <= 2016 else 0.03
        pool.append(replace(it, price=int(true_price(it.year, it.odometer_km, it.variant,
                                                     it.transmission == "Manual") * math.exp(rnd.gauss(0, noise)))))
    group = fit_group(pool, CFG, REF_YEAR)
    old = value(target(year=2014, odometer_km=170000, variant="Ascent Sport"), pool, CFG, group)
    new = value(target(year=2023, odometer_km=40000), pool, CFG, group)
    width = lambda r: (r.p75 - r.p25) / r.estimate
    assert width(old) > 2 * width(new)
    assert old.confidence != "High"
