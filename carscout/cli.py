"""Maintenance commands:  docker compose exec api python -m carscout.cli <command>

  purge-source <name>   delete every listing, raw payload and run from one source (e.g. demo)
"""
from __future__ import annotations

import sys

from sqlalchemy import delete

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
    print(f"Deleted {n} listings, {r} raw payloads and {runs} runs from '{name}'.")


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "purge-source":
        purge_source(argv[1])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
