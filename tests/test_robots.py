"""robots.txt parsing per RFC 9309 (longest match, wildcards), using Cars24's real file."""
from pathlib import Path

import httpx

from carscout.sources.robots import Robots
from tests.test_http import make_client

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
CARS24 = Robots.parse((Path(__file__).parent / "fixtures/cars24/robots.txt").read_text())
B = "https://www.cars24.com.au"


def test_cars24_rules():
    assert CARS24.can_fetch(UA, f"{B}/buy-used-toyota-corolla/")
    assert CARS24.can_fetch(UA, f"{B}/buy-used-toyota-corolla/?page=2")
    assert CARS24.can_fetch(UA, f"{B}/buy-used-toyota-corolla-car-23606515385984/")
    # The stdlib parser gets these wrong: the group starts with "Allow: /".
    assert not CARS24.can_fetch(UA, f"{B}/api/listing")
    assert not CARS24.can_fetch(UA, f"{B}/buy-used-cars-australia/?f=bodyType%3Ain%3Asuv")
    assert not CARS24.can_fetch(UA, f"{B}/buy-used-toyota/?sf=make:toyota")
    assert CARS24.crawl_delay(UA) == 2


def test_longest_match_wildcards_and_anchor():
    r = Robots.parse(
        "User-agent: *\n"
        "Disallow: /a\n"
        "Allow: /a/b\n"
        "Disallow: /*.pdf$\n"
        "Disallow:\n"
    )
    assert not r.can_fetch(UA, "https://x.test/a/c")
    assert r.can_fetch(UA, "https://x.test/a/b/c")
    assert not r.can_fetch(UA, "https://x.test/files/doc.pdf")
    assert r.can_fetch(UA, "https://x.test/files/doc.pdf?x=1")
    assert r.can_fetch(UA, "https://x.test/other")


def test_equal_length_allow_wins_and_specific_group():
    r = Robots.parse("User-agent: *\nDisallow: /p\nAllow: /p\n\nUser-agent: mozilla\nDisallow: /m/\n")
    # A group naming our product token replaces the * group entirely.
    assert r.can_fetch(UA, "https://x.test/p")
    assert not r.can_fetch(UA, "https://x.test/m/1")
    assert r.can_fetch("OtherBot/1.0", "https://x.test/p")  # tie: Allow wins


def test_group_matches_product_token_not_substring():
    r = Robots.parse("User-agent: Safari\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert r.can_fetch(UA, "https://x.test/page")  # our UA mentions Safari but is "Mozilla"
    assert not r.can_fetch("Safari/605.1", "https://x.test/page")


async def test_client_honours_crawl_delay():
    def handler(req: httpx.Request):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 9\n")
        return httpx.Response(200, text="ok")

    c, sleeps = make_client(handler, delay=2)
    await c.get("https://x.test/a")
    await c.get("https://x.test/b")
    assert c.delay == 9
    assert sleeps and max(sleeps) == 9


async def test_facebook_marketplace_refused():
    """Phase 8 outcome: robots.txt disallows everything, so the client never fetches Marketplace."""
    import pytest

    from carscout.sources.base import RobotsDisallowed

    text = (Path(__file__).parent / "fixtures/facebook/robots_excerpt.txt").read_text()
    assert not Robots.parse(text).can_fetch(UA, "https://www.facebook.com/marketplace/melbourne/vehicles")
    fetched: list[str] = []

    def handler(req: httpx.Request):
        fetched.append(req.url.path)
        return httpx.Response(200, text=text if req.url.path == "/robots.txt" else "<html>listings</html>")

    c, _ = make_client(handler)
    with pytest.raises(RobotsDisallowed):
        await c.get("https://www.facebook.com/marketplace/melbourne/vehicles")
    assert fetched == ["/robots.txt"]  # the Marketplace page itself was never requested
