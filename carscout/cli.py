"""Maintenance commands:  docker compose exec api python -m carscout.cli <command>

  purge-source <name>   delete every listing, raw payload and run from one source (e.g. demo)
  reparse <name>        re-run a source's parser over its stored raw payloads (no network);
                        use after changing parse() or the normalise/data/*.yaml tables
  dedupe                recompute which listings are the same car, across all sources (then revalue)
  revalue               recompute every market valuation (after editing config/valuation.yaml)
"""
from __future__ import annotations

import sys

from sqlalchemy import delete, select

from carscout.core.config import load_sources
from carscout.core.db import get_sessionmaker
from carscout.models import Listing, RawPayload, ScrapeRun, Source, SourceStatus


def purge_source(name: str) -> None:
    with get_sessionmaker().begin() as s:
        n = s.execute(delete(Listing).where(Listing.source == name)).rowcount
        r = s.execute(delete(RawPayload).where(RawPayload.source == name)).rowcount
        runs = s.execute(delete(ScrapeRun).where(ScrapeRun.source == name)).rowcount
        src = s.get(Source, name)
        if src:
            src.status, src.last_error, src.last_count = SourceStatus.NEVER_RUN, None, None
            src.last_run_at = src.last_ok_at = None
        from carscout.dedupe.store import dedupe

        dedupe(s)  # drops vehicles that only had this source's listings
    print(f"Deleted {n} listings, {r} raw payloads and {runs} runs from '{name}'.")


def reparse(name: str) -> tuple[int, int]:
    """Returns (listings re-parsed, listings whose fields changed). Price history is left alone:
    it records observed prices, and a parser fix isn't an observation."""
    from dataclasses import asdict

    from carscout.sources.base import RawListing, load_plugin
    from carscout.worker.runner import _LISTING_FIELDS, _fit

    cfg = load_sources().get(name)
    if cfg is None:
        raise SystemExit(f"unknown source '{name}' (not in sources.yaml)")
    plugin = load_plugin(cfg)
    done = changed = 0
    with get_sessionmaker().begin() as s:
        rows = s.execute(
            select(Listing, RawPayload)
            .join(RawPayload, Listing.latest_raw_id == RawPayload.id)
            .where(Listing.source == name)
        ).all()
        for listing, raw in rows:
            payload = raw.payload_json if raw.content_type == "json" else raw.payload_text
            parsed = asdict(plugin.parse(RawListing(raw.source_listing_id, raw.url or listing.url,
                                                    payload, raw.content_type)))
            values = _fit({k: parsed[k] for k in _LISTING_FIELDS})
            if any(getattr(listing, k) != v for k, v in values.items()):
                changed += 1
                for k, v in values.items():
                    setattr(listing, k, v)
            done += 1
    return done, changed


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "purge-source":
        purge_source(argv[1])
        return 0
    if len(argv) == 2 and argv[0] == "reparse":
        done, changed = reparse(argv[1])
        print(f"Re-parsed {done} listings from '{argv[1]}'; {changed} changed. Re-running dedupe...")
        argv = ["dedupe"]
    if argv == ["dedupe"]:
        from carscout.dedupe.store import dedupe

        with get_sessionmaker().begin() as s:
            st = dedupe(s)
        print(f"{st.get('listings', 0)} listings are {st.get('vehicles', 0)} cars; "
              f"{st.get('multi', 0)} cars are listed more than once. Re-running valuations...")
        argv = ["revalue"]
    if argv == ["revalue"]:
        from carscout.valuation.store import revalue

        with get_sessionmaker().begin() as s:
            st = revalue(s)
        print(f"Valued {st.get('regression', 0)} listings by regression and {st.get('comps', 0)} by "
              f"comparables; {st.get('insufficient', 0)} had too little data; "
              f"{st.get('excluded', 0)} excluded as junk.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
