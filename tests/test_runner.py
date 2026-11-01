"""Runner: storage, price history, raw payloads, and per-source isolation."""
from collections.abc import AsyncIterator

from sqlalchemy import func, select

from carscout.core.config import load_sources
from carscout.models import (
    Listing,
    PriceHistory,
    RawPayload,
    RunStatus,
    SavedSearch,
    ScrapeRun,
    SearchListing,
    Source,
)
from carscout.sources.base import RawListing, SearchParams, SourceBlocked
from carscout.sources.demo import Plugin as Demo
from carscout.worker.queue import claim_next, enqueue_search, sync_sources
from carscout.worker.runner import execute_run
from tests.conftest import set_demo_mode


def _setup(sm, enable_demo=True):
    with sm.begin() as s:
        sync_sources(s)
        s.get(Source, "demo").enabled = enable_demo
        search = SavedSearch(name="Corolla", params={"make": "Toyota", "model": "Corolla"})
        s.add(search)
    return search.id


def _queue_and_run(sm):
    with sm.begin() as s:
        runs = enqueue_search(s, s.get(SavedSearch, 1))
        ids = [r.id for r in runs]
    return ids


async def test_demo_run_stores_listings_history_and_raw(sm):
    _setup(sm)
    [run_id] = _queue_and_run(sm)
    with sm.begin() as s:
        assert claim_next(s, set()) == (run_id, "demo")
    assert await execute_run(sm, run_id) == RunStatus.OK

    with sm() as s:
        assert s.scalar(select(func.count(Listing.id))) == 5
        assert s.scalar(select(func.count(PriceHistory.id))) == 5
        assert s.scalar(select(func.count(RawPayload.id))) == 5
        l = s.scalars(select(Listing)).first()
        assert l.make == "Toyota" and l.model == "Corolla" and l.is_active and l.latest_raw_id
        run = s.get(ScrapeRun, run_id)
        assert (run.found, run.new, run.status) == (5, 5, "ok")
        src = s.get(Source, "demo")
        assert src.status == "ok" and src.last_count == 5 and src.last_ok_at

    # Second identical run: nothing new, no duplicate raw payloads or history.
    [run2] = _queue_and_run(sm)
    await execute_run(sm, run2)
    with sm() as s:
        assert s.scalar(select(func.count(Listing.id))) == 5
        assert s.scalar(select(func.count(RawPayload.id))) == 5
        assert s.get(ScrapeRun, run2).new == 0


class PriceDrop(Demo):
    """Demo listings, but $1,000 cheaper: should add a price-history row each."""

    async def search(self, params, http) -> AsyncIterator[RawListing]:
        async for raw in super().search(params, http):
            raw.payload["price"] -= 1000
            yield raw


async def test_price_change_recorded(sm):
    _setup(sm)
    [r1] = _queue_and_run(sm)
    await execute_run(sm, r1)
    [r2] = _queue_and_run(sm)
    await execute_run(sm, r2, plugin=PriceDrop(load_sources()["demo"]))
    with sm() as s:
        l = s.scalars(select(Listing).order_by(Listing.id)).first()
        prices = [p.price for p in l.price_history]
        assert len(prices) == 2 and prices[0] - prices[1] == 1000 and l.price == prices[1]
        assert s.get(ScrapeRun, r2).updated == 5
        assert s.scalar(select(func.count(RawPayload.id))) == 10  # changed payloads are kept


async def test_blocked_source_is_degraded_not_fatal(sm, sources_file):
    _setup(sm)
    set_demo_mode(sources_file, "blocked")
    [run_id] = _queue_and_run(sm)
    assert await execute_run(sm, run_id) == RunStatus.DEGRADED
    with sm() as s:
        run, src = s.get(ScrapeRun, run_id), s.get(Source, "demo")
        assert run.status == "degraded" and "simulated bot protection" in run.error
        assert src.status == "degraded" and src.last_error


async def test_plugin_crash_keeps_partial_results(sm, sources_file):
    _setup(sm)
    set_demo_mode(sources_file, "error")
    [run_id] = _queue_and_run(sm)
    assert await execute_run(sm, run_id) == RunStatus.FAILED
    with sm() as s:
        assert s.scalar(select(func.count(Listing.id))) == 2
        assert "RuntimeError" in s.get(ScrapeRun, run_id).error
        assert s.get(Source, "demo").status == "degraded"


class AlwaysBlocked(Demo):
    async def search(self, params, http):
        raise SourceBlocked("nope")
        yield  # pragma: no cover


async def test_one_blocked_source_does_not_affect_another(sm):
    """Two runs back to back, first blocked: the second still stores its listings."""
    _setup(sm)
    with sm.begin() as s:
        a = ScrapeRun(saved_search_id=1, source="demo")
        b = ScrapeRun(saved_search_id=1, source="demo")
        s.add_all([a, b])
    cfg = load_sources()["demo"]
    assert await execute_run(sm, a.id, plugin=AlwaysBlocked(cfg)) == RunStatus.DEGRADED
    assert await execute_run(sm, b.id) == RunStatus.OK
    with sm() as s:
        assert s.scalar(select(func.count(Listing.id))) == 5
        assert s.get(Source, "demo").status == "ok"  # recovered on the next good run


async def test_enqueue_respects_toggles_config_and_dedup(sm):
    _setup(sm, enable_demo=False)
    assert _queue_and_run(sm) == []  # demo toggled off; carsales not built; facebook off in config
    with sm.begin() as s:
        s.get(Source, "demo").enabled = True
    assert len(_queue_and_run(sm)) == 1
    assert _queue_and_run(sm) == []  # already queued for this search
    with sm.begin() as s:
        assert claim_next(s, {"demo"}) is None  # one run per source at a time


# ---- disappearance (is_active) ------------------------------------------------------------
class FirstThree(Demo):
    """Demo, but only the first 3 of the 5 listings: the other 2 have 'disappeared'."""

    async def search(self, params, http):
        n = 0
        async for raw in super().search(params, http):
            if n < 3:
                yield raw
            n += 1


class FirstThreeStillListed(FirstThree):
    async def confirm_gone(self, listing, http):
        return False  # e.g. the car is still for sale, just no longer matches the search


async def _two_runs(sm, plugin_cls, **second_run_params):
    """A normal run (5 listings), then a second run with `plugin_cls` and extra search params."""
    _setup(sm)
    [r1] = _queue_and_run(sm)
    await execute_run(sm, r1)
    if second_run_params:
        with sm.begin() as s:
            s.get(SavedSearch, 1).params = {"make": "Toyota", "model": "Corolla", **second_run_params}
    [r2] = _queue_and_run(sm)
    await execute_run(sm, r2, plugin=plugin_cls(load_sources()["demo"]))
    return r2


async def test_disappeared_listings_marked_inactive(sm):
    r2 = await _two_runs(sm, FirstThree)
    with sm() as s:
        active = s.scalar(select(func.count(Listing.id)).where(Listing.is_active.is_(True)))
        assert active == 3 and s.get(ScrapeRun, r2).gone == 2
        assert s.scalar(select(func.count()).select_from(SearchListing)) == 5
    # Seen again later: active again, without a duplicate row.
    [r3] = _queue_and_run(sm)
    await execute_run(sm, r3)
    with sm() as s:
        assert s.scalar(select(func.count(Listing.id)).where(Listing.is_active.is_(True))) == 5
        assert s.scalar(select(func.count(Listing.id))) == 5


async def test_still_listed_elsewhere_stays_active(sm):
    r2 = await _two_runs(sm, FirstThreeStillListed)
    with sm() as s:
        assert s.scalar(select(func.count(Listing.id)).where(Listing.is_active.is_(True))) == 5
        assert s.get(ScrapeRun, r2).gone == 0


async def test_truncated_run_marks_nothing_inactive(sm):
    # max_results=3 stops the run at the cap: we can't know what's missing, so nothing is touched.
    r2 = await _two_runs(sm, Demo, max_results=3)
    with sm() as s:
        assert s.scalar(select(func.count(Listing.id)).where(Listing.is_active.is_(True))) == 5
        assert s.get(ScrapeRun, r2).gone == 0


async def test_blocked_run_marks_nothing_inactive(sm):
    r2 = await _two_runs(sm, AlwaysBlocked)
    with sm() as s:
        assert s.scalar(select(func.count(Listing.id)).where(Listing.is_active.is_(True))) == 5
        assert s.get(ScrapeRun, r2).status == "degraded"


# ---- storage details ----------------------------------------------------------------------
class LongValues(Demo):
    def parse(self, raw):
        l = super().parse(raw)
        l.colour = "Very " * 20 + "Red"
        return l


async def test_overlong_values_are_trimmed_not_fatal(sm):
    _setup(sm)
    [r1] = _queue_and_run(sm)
    assert await execute_run(sm, r1, plugin=LongValues(load_sources()["demo"])) == RunStatus.OK
    with sm() as s:
        assert len(s.scalars(select(Listing)).first().colour) == 32


async def test_previous_payload_lookup(sm):
    _setup(sm)
    [r1] = _queue_and_run(sm)
    await execute_run(sm, r1)
    seen = {}

    class Peek(Demo):
        async def search(self, params, http):
            async for raw in super().search(params, http):
                seen[raw.source_listing_id] = self.previous(raw.source_listing_id)
                yield raw

    [r2] = _queue_and_run(sm)
    await execute_run(sm, r2, plugin=Peek(load_sources()["demo"]))
    assert len(seen) == 5 and all(isinstance(v, dict) and v["make"] == "Toyota" for v in seen.values())


def test_reparse_command(sm):
    from carscout.cli import reparse

    _setup(sm)
    [r1] = _queue_and_run(sm)
    import asyncio
    asyncio.run(execute_run(sm, r1))
    with sm.begin() as s:
        s.scalars(select(Listing)).first().colour = "WRONG"
    assert reparse("demo") == (5, 1)
    with sm() as s:
        assert "WRONG" not in {l.colour for l in s.scalars(select(Listing))}
