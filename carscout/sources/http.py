"""PoliteClient: the only way plugins talk to the network.

- one request at a time per source by default, minimum delay plus random jitter between them
- robots.txt is fetched once per host per run and honoured (RFC 9309 rules, see robots.py),
  and its Crawl-delay raises the minimum delay if it asks for more than the config does
- 429/403/5xx are retried with exponential backoff (honouring Retry-After); if they persist,
  the source is reported blocked/unavailable instead of hammering it
- challenge pages (Cloudflare, DataDome, PerimeterX, Imperva) are detected and reported.
  We never try to get around them.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from carscout.core.logging import get_logger
from carscout.sources.robots import Robots

log = get_logger(__name__)

RETRY_STATUSES = {403, 429, 500, 502, 503, 504}

# Strong markers only: normal pages from these sites may still load the vendor's script
# (e.g. Carsales pages mention "datadome"), so the vendor name alone is not a block.
CHALLENGE_MARKERS = (
    "<title>just a moment",          # Cloudflare interstitial
    "cf-chl-",                       # Cloudflare challenge assets
    "captcha-delivery.com",          # DataDome captcha
    "px-captcha",                    # PerimeterX / HUMAN
    "_incapsula_resource",           # Imperva
    "pardon our interruption",       # Imperva / Distil
    "<title>access denied",          # Akamai
)


def detect_challenge(resp: httpx.Response) -> str | None:
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and resp.status_code == 200:
        return None
    head = resp.text[:200_000].lower()
    for marker in CHALLENGE_MARKERS:
        if marker in head:
            return marker
    if resp.headers.get("cf-mitigated") == "challenge":
        return "cf-mitigated"
    if resp.headers.get("x-datadome") and resp.status_code in (401, 403):
        return "datadome"
    return None


class PoliteClient:
    def __init__(
        self,
        source: str,
        *,
        delay: float,
        jitter: float,
        concurrency: int = 1,
        max_retries: int = 3,
        backoff_base: float = 10.0,
        respect_robots: bool = True,
        user_agent: str = "Mozilla/5.0",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.source = source
        self.delay = delay
        self.jitter = jitter
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.respect_robots = respect_robots
        self.user_agent = user_agent
        self.requests = 0
        self._sleep = sleep
        self._rng = rng
        self._clock = clock
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self._robots: dict[str, Robots] = {}
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            http2=False,
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "en-AU,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals ---------------------------------------------------------------------------
    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._next_at - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._next_at = self._clock() + self.delay + self._rng() * self.jitter

    async def _send(self, url: str, **kw: Any) -> httpx.Response:
        async with self._sem:
            await self._throttle()
            self.requests += 1
            return await self._client.get(url, **kw)

    async def _check_robots(self, url: str) -> None:
        from carscout.sources.base import RobotsDisallowed, SourceBlocked

        if not self.respect_robots:
            return
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots:
            robots_url = f"{host}/robots.txt"
            try:
                resp = await self._send(robots_url)
            except httpx.TransportError as e:
                log.warning("robots_unreachable", source=self.source, url=robots_url, error=str(e))
                self._robots[host] = Robots.allowing_all()  # we'll find out on the real request
                return
            if detect_challenge(resp):
                raise SourceBlocked(f"robots.txt is behind bot protection (HTTP {resp.status_code})")
            if resp.status_code in (401, 403):
                robots = Robots.disallowing_all()  # standard convention for 401/403
            elif resp.status_code >= 400:
                robots = Robots.allowing_all()  # no robots.txt: everything allowed
            else:
                robots = Robots.parse(resp.text)
                crawl_delay = robots.crawl_delay(self.user_agent)
                if crawl_delay and crawl_delay > self.delay:
                    log.info("robots_crawl_delay", source=self.source, delay=crawl_delay)
                    self.delay = min(crawl_delay, 60.0)
            self._robots[host] = robots
        if not self._robots[host].can_fetch(self.user_agent, url):
            raise RobotsDisallowed(f"robots.txt disallows {parts.path}")

    def _backoff(self, attempt: int, resp: httpx.Response | None) -> float:
        if resp is not None:
            retry_after = resp.headers.get("retry-after", "")
            if retry_after.isdigit():
                return min(float(retry_after), 600.0)
        return self.backoff_base * (2**attempt) + self._rng() * self.backoff_base

    # -- public ------------------------------------------------------------------------------
    async def get(self, url: str, **kw: Any) -> httpx.Response:
        """GET with politeness rules. Returns any non-retryable response (e.g. 404) for the
        caller to judge; raises SourceBlocked / SourceUnavailable when the source won't serve us."""
        from carscout.sources.base import SourceBlocked, SourceUnavailable

        await self._check_robots(url)
        last: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._send(url, **kw)
            except httpx.TransportError as e:
                last = None
                if attempt >= self.max_retries:
                    raise SourceUnavailable(f"network error: {e!r}") from e
                wait = self._backoff(attempt, None)
                log.warning("http_retry", source=self.source, url=url, error=str(e), wait=round(wait, 1))
                await self._sleep(wait)
                continue

            marker = detect_challenge(resp)
            if marker:
                # Retrying a challenge page just looks more like a bot. Stop here.
                raise SourceBlocked(f"bot protection ({marker}) on HTTP {resp.status_code}")
            if resp.status_code not in RETRY_STATUSES:
                return resp
            last = resp
            if attempt >= self.max_retries:
                break
            wait = self._backoff(attempt, resp)
            log.warning("http_retry", source=self.source, url=url, status=resp.status_code, wait=round(wait, 1))
            await self._sleep(wait)

        assert last is not None
        msg = f"HTTP {last.status_code} after {self.max_retries + 1} attempts"
        if last.status_code in (403, 429):
            raise SourceBlocked(msg)
        raise SourceUnavailable(msg)

    async def get_json(self, url: str, **kw: Any) -> Any:
        headers = {"Accept": "application/json", **kw.pop("headers", {})}
        resp = await self.get(url, headers=headers, **kw)
        resp.raise_for_status()
        return resp.json()
