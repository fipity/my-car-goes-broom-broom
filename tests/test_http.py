"""PoliteClient behaviour with a fake transport: no real network."""
import httpx
import pytest

from carscout.sources.base import RobotsDisallowed, SourceBlocked, SourceUnavailable
from carscout.sources.http import PoliteClient

ROBOTS = "User-agent: *\nDisallow: /private/\n"


def make_client(handler, **kw):
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    defaults = dict(delay=2, jitter=0, max_retries=3, backoff_base=10, rng=lambda: 0.0,
                    clock=lambda: 0.0)
    defaults.update(kw)
    client = PoliteClient("test", transport=httpx.MockTransport(handler), sleep=fake_sleep, **defaults)
    return client, sleeps


def router(pages):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        resp = pages[req.url.path]
        return resp.pop(0) if isinstance(resp, list) else resp

    return handler, calls


async def test_ok_and_rate_limited():
    handler, calls = router({"/a": httpx.Response(200, text="hi"), "/b": httpx.Response(200, text="yo")})
    c, sleeps = make_client(handler)
    assert (await c.get("https://x.test/a")).text == "hi"
    assert (await c.get("https://x.test/b")).text == "yo"
    assert calls == ["/robots.txt", "/a", "/b"]  # robots fetched once
    assert sleeps == [2, 2]  # fixed clock: each request after the first waits the delay
    assert c.requests == 3


async def test_robots_disallow():
    handler, _ = router({})
    c, _ = make_client(handler)
    with pytest.raises(RobotsDisallowed):
        await c.get("https://x.test/private/thing")


async def test_retry_then_success_honours_retry_after():
    handler, calls = router({"/a": [httpx.Response(429, headers={"Retry-After": "7"}),
                                    httpx.Response(503), httpx.Response(200, text="ok")]})
    c, sleeps = make_client(handler, delay=0)
    assert (await c.get("https://x.test/a")).text == "ok"
    assert calls.count("/a") == 3
    assert 7 in sleeps and 20 in sleeps  # Retry-After, then base*2^1 backoff


async def test_persistent_403_is_blocked():
    handler, calls = router({"/a": [httpx.Response(403) for _ in range(10)]})
    c, _ = make_client(handler, delay=0)
    with pytest.raises(SourceBlocked, match="HTTP 403 after 4 attempts"):
        await c.get("https://x.test/a")
    assert calls.count("/a") == 4


async def test_challenge_page_stops_immediately():
    page = httpx.Response(403, headers={"content-type": "text/html"},
                          text="<html><title>Just a moment...</title></html>")
    handler, calls = router({"/a": [page for _ in range(5)]})
    c, _ = make_client(handler, delay=0)
    with pytest.raises(SourceBlocked, match="bot protection"):
        await c.get("https://x.test/a")
    assert calls.count("/a") == 1  # no hammering a challenge


async def test_normal_page_mentioning_vendor_is_not_a_block():
    page = httpx.Response(200, headers={"content-type": "text/html"},
                          text='<script src="https://js.datadome.co/tags.js"></script><h1>Cars</h1>')
    handler, _ = router({"/a": page})
    c, _ = make_client(handler, delay=0)
    assert (await c.get("https://x.test/a")).status_code == 200


async def test_network_errors_become_unavailable():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ConnectError("boom")

    c, _ = make_client(handler, delay=0, max_retries=1)
    with pytest.raises(SourceUnavailable):
        await c.get("https://x.test/a")


async def test_robots_behind_challenge_is_blocked():
    def handler(req):
        return httpx.Response(403, headers={"content-type": "text/html"},
                              text="<title>Just a moment...</title>")

    c, _ = make_client(handler)
    with pytest.raises(SourceBlocked, match="robots.txt"):
        await c.get("https://x.test/a")
