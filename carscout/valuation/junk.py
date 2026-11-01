"""Spotting listings that aren't normal sales: write-offs, wrecks, parts cars, rentals, $1 prices."""
from __future__ import annotations

import re
from functools import lru_cache

from carscout.valuation.config import JunkConfig


@lru_cache(maxsize=8)
def _compiled(keywords: tuple[tuple[str, tuple[str, ...]], ...], negations: tuple[str, ...]):
    kw = [(reason, re.compile("|".join(f"(?:{p})" for p in pats), re.I)) for reason, pats in keywords if pats]
    neg = re.compile("|".join(f"(?:{p})" for p in negations), re.I) if negations else None
    return kw, neg


def junk_reason(price: int | None, texts: list[str | None], cfg: JunkConfig) -> str | None:
    """None for a normal listing, else why it's excluded: no_price, low_price, write_off,
    wrecking, parts_only, rental (keyword groups come from the config)."""
    if price is None:
        return "no_price"  # POA etc.
    if price < cfg.min_price:
        return "low_price"
    kw, neg = _compiled(tuple((k, tuple(v)) for k, v in cfg.keywords.items()), tuple(cfg.negations))
    text = " . ".join(t for t in texts if t)
    if neg:
        text = neg.sub(" ", text)  # "No write offs on record" is good news, not a write-off
    for reason, rx in kw:
        if rx.search(text):
            return reason
    return None
