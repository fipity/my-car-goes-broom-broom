"""The scrape queue lives in the scrape_runs table. The API and scheduler enqueue; the worker claims.
NOTIFY wakes the worker immediately; it also polls as a fallback."""
from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from carscout.core.config import SourceConfig, load_sources
from carscout.models import RunStatus, SavedSearch, ScrapeRun, Source
from carscout.sources.base import plugin_exists

CHANNEL = "scrape_runs"


def sync_sources(session: Session, configs: dict[str, SourceConfig] | None = None) -> None:
    """Make sure every configured source has a row. New rows start enabled, except demo."""
    configs = configs if configs is not None else load_sources()
    existing = {s.name for s in session.scalars(select(Source))}
    for name in configs:
        if name not in existing:
            session.add(Source(name=name, enabled=name != "demo"))
    session.flush()


def is_runnable(cfg: SourceConfig | None, row: Source | None) -> bool:
    return bool(
        cfg and row and cfg.enabled and cfg.implemented and row.enabled and plugin_exists(cfg.name)
    )


def enqueue_search(session: Session, search: SavedSearch, trigger: str = "manual") -> list[ScrapeRun]:
    """Queue one run per runnable source for this search. Skips sources already queued/running
    for the same search. Caller commits."""
    configs = load_sources()
    sync_sources(session, configs)
    rows = {s.name: s for s in session.scalars(select(Source))}
    wanted = set(search.sources) if search.sources else set(configs)
    busy = set(
        session.scalars(
            select(ScrapeRun.source).where(
                ScrapeRun.saved_search_id == search.id,
                ScrapeRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
            )
        )
    )
    created = []
    for name in sorted(wanted - busy):
        if is_runnable(configs.get(name), rows.get(name)):
            run = ScrapeRun(saved_search_id=search.id, source=name, trigger=trigger)
            session.add(run)
            created.append(run)
    session.flush()
    if created:
        session.execute(text(f"NOTIFY {CHANNEL}"))  # delivered on commit
    return created


def enqueue_scheduled(session: Session) -> int:
    searches = session.scalars(select(SavedSearch).where(SavedSearch.schedule_enabled.is_(True)))
    return sum(len(enqueue_search(session, s, trigger="schedule")) for s in searches)


def claim_next(session: Session, busy_sources: set[str]) -> tuple[int, str] | None:
    """Atomically take the oldest queued run whose source isn't already running here.
    One run per source at a time keeps request volume per site low."""
    row = session.execute(
        text(
            """
            UPDATE scrape_runs SET status = :running, started_at = now()
            WHERE id = (
                SELECT id FROM scrape_runs
                WHERE status = :queued AND NOT (source = ANY(:busy))
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
            )
            RETURNING id, source
            """
        ),
        {"running": RunStatus.RUNNING, "queued": RunStatus.QUEUED, "busy": list(busy_sources)},
    ).first()
    return (row[0], row[1]) if row else None


def fail_orphaned_runs(session: Session) -> int:
    """Runs left 'running' by a worker that died/restarted."""
    result = session.execute(
        text(
            "UPDATE scrape_runs SET status = :failed, finished_at = now(), "
            "error = 'worker restarted during the run' WHERE status = :running"
        ),
        {"failed": RunStatus.FAILED, "running": RunStatus.RUNNING},
    )
    return result.rowcount or 0
