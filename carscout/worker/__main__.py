"""Worker process: cron schedule + queue dispatcher. `python -m carscout.worker`"""
from __future__ import annotations

import asyncio
import signal

import psycopg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from carscout.core.config import get_settings
from carscout.core.db import get_sessionmaker
from carscout.core.logging import configure_logging, get_logger
from carscout.worker.queue import CHANNEL, claim_next, enqueue_scheduled, fail_orphaned_runs, sync_sources
from carscout.worker.runner import execute_run

log = get_logger("carscout.worker")
POLL_SECONDS = 30
MAX_PARALLEL_RUNS = 4  # across different sources; never more than one per source


class Dispatcher:
    def __init__(self) -> None:
        self.sm = get_sessionmaker()
        self.busy: dict[str, asyncio.Task[str]] = {}
        self.wake = asyncio.Event()

    def dispatch(self) -> None:
        while len(self.busy) < MAX_PARALLEL_RUNS:
            with self.sm.begin() as s:
                claimed = claim_next(s, set(self.busy))
            if claimed is None:
                return
            run_id, source = claimed
            task = asyncio.create_task(execute_run(self.sm, run_id))
            self.busy[source] = task
            task.add_done_callback(lambda _t, src=source: self._done(src))

    def _done(self, source: str) -> None:
        self.busy.pop(source, None)
        self.wake.set()

    async def listen(self, conninfo: str) -> None:
        """Wake on NOTIFY; reconnect if Postgres goes away."""
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    log.info("listening", channel=CHANNEL)
                    async for _ in conn.notifies():
                        self.wake.set()
            except (psycopg.Error, OSError) as e:
                log.warning("listen_reconnect", error=str(e))
                await asyncio.sleep(5)

    async def loop(self) -> None:
        while True:
            try:
                self.dispatch()
            except Exception:
                log.exception("dispatch_error")
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=POLL_SECONDS)
            except TimeoutError:
                pass


def scheduled_job() -> None:
    with get_sessionmaker().begin() as s:
        n = enqueue_scheduled(s)
    log.info("scheduled_enqueue", runs=n)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    sm = get_sessionmaker()
    with sm.begin() as s:
        sync_sources(s)
        orphaned = fail_orphaned_runs(s)
    if orphaned:
        log.warning("orphaned_runs_failed", count=orphaned)

    trigger = CronTrigger.from_crontab(settings.scrape_cron, timezone=settings.schedule_tz)
    scheduler = AsyncIOScheduler(timezone=settings.schedule_tz)
    job = scheduler.add_job(scheduled_job, trigger, id="scheduled_scrape", coalesce=True,
                            max_instances=1, misfire_grace_time=3600)
    scheduler.start()
    log.info("worker_started", cron=settings.scrape_cron, tz=settings.schedule_tz,
             next_run=str(job.next_run_time))

    d = Dispatcher()
    conninfo = settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(d.listen(conninfo)), asyncio.create_task(d.loop())]
    await stop.wait()
    log.info("worker_stopping", running=list(d.busy))
    scheduler.shutdown(wait=False)
    for t in tasks:
        t.cancel()
    # Running scrapes are cancelled; on next start they are marked failed ("worker restarted").


if __name__ == "__main__":
    asyncio.run(main())
