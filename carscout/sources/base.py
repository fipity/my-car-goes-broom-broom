"""The source plugin contract.

A source is one module in carscout/sources/<name>.py that defines `class Plugin(SourcePlugin)`,
plus an entry under `sources:` in sources.yaml. Nothing else needs registering.
"""
from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field, fields
from typing import Any, ClassVar

from carscout.core.config import SourceConfig
from carscout.sources.http import PoliteClient


# ---- errors -------------------------------------------------------------------------------
class SourceError(Exception):
    """Base for problems that mean "this source is degraded", not "the code is broken"."""


class SourceBlocked(SourceError):
    """Bot protection, a challenge page, or persistent 403/429."""


class RobotsDisallowed(SourceBlocked):
    """robots.txt disallows the URL we need."""


class SourceUnavailable(SourceError):
    """Network errors or 5xx that survived retries."""


class SourceChanged(SourceError):
    """The site answered, but its data isn't where the plugin expects: the parser needs updating."""


# ---- data passed between plugins and the runner -------------------------------------------
@dataclass
class SearchParams:
    make: str | None = None
    model: str | None = None
    variant: str | None = None
    year_min: int | None = None
    year_max: int | None = None
    km_max: int | None = None
    price_min: int | None = None
    price_max: int | None = None
    states: list[str] = field(default_factory=list)
    body_type: str | None = None
    transmission: str | None = None
    max_results: int = 200  # hard cap per source per run, to keep volume low

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SearchParams:
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (d or {}).items() if k in known and v not in (None, "")}
        if isinstance(clean.get("states"), str):
            clean["states"] = [s.strip().upper() for s in clean["states"].split(",") if s.strip()]
        return cls(**clean)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", [])}


@dataclass
class RawListing:
    """What a source returned for one listing, stored verbatim in raw_payloads."""

    source_listing_id: str
    url: str
    payload: Any  # dict/list for JSON, str for HTML
    content_type: str = "json"  # json | html


@dataclass
class NormalisedListing:
    """The common schema. `parse()` must fill what the source provides and leave the rest None."""

    source_listing_id: str
    url: str
    title: str
    make: str | None = None
    model: str | None = None
    variant: str | None = None
    year: int | None = None
    odometer_km: int | None = None
    price: int | None = None
    price_type: str = "unknown"
    body_type: str | None = None
    transmission: str | None = None
    fuel_type: str | None = None
    engine: str | None = None
    drivetrain: str | None = None
    colour: str | None = None
    seller_type: str = "unknown"
    suburb: str | None = None
    state: str | None = None
    postcode: str | None = None
    images: list[str] = field(default_factory=list)
    description: str | None = None
    rego: str | None = None
    vin: str | None = None


# ---- the plugin base ----------------------------------------------------------------------
@dataclass
class KnownListing:
    """A listing we stored earlier, as passed to `confirm_gone`."""

    source_listing_id: str
    url: str


class SourcePlugin(ABC):
    name: ClassVar[str]
    parser_version: ClassVar[int] = 1  # bump when parse() changes, to find rows to re-parse
    needs_http: ClassVar[bool] = True

    def __init__(self, config: SourceConfig):
        self.config = config
        # Set by the runner: source_listing_id -> the latest stored payload (or None). Lets a
        # plugin reuse an expensive detail fetch when the search result hasn't changed.
        self.previous: Callable[[str], object | None] = lambda _id: None
        # A plugin sets this when it stopped early (page cap etc.). Disappearance checks only
        # run after complete searches, so a truncated run never marks anything inactive.
        self.truncated = False

    @abstractmethod
    def search(self, params: SearchParams, http: PoliteClient) -> AsyncIterator[RawListing]:
        """Yield raw listings for a saved search. Raise SourceError subclasses on blocks."""

    async def fetch_detail(self, url: str, http: PoliteClient) -> RawListing:
        """Fetch one listing's detail page/JSON. Optional: only for sources whose search
        results lack fields we need."""
        raise NotImplementedError

    async def confirm_gone(self, listing: KnownListing, http: PoliteClient) -> bool | None:
        """Called for listings a complete search no longer returns. True = sold/removed (mark
        inactive), False = still listed (it just stopped matching), None = can't tell.
        The default trusts the search: absent means gone."""
        return True

    @abstractmethod
    def parse(self, raw: RawListing) -> NormalisedListing:
        """Pure function: raw payload -> common schema. No network, no DB."""


def load_plugin(config: SourceConfig) -> SourcePlugin:
    module = importlib.import_module(f"carscout.sources.{config.name}")
    plugin_cls = getattr(module, "Plugin")
    if not issubclass(plugin_cls, SourcePlugin):
        raise TypeError(f"carscout.sources.{config.name}.Plugin is not a SourcePlugin")
    return plugin_cls(config)


def plugin_exists(name: str) -> bool:
    try:
        importlib.import_module(f"carscout.sources.{name}")
    except ModuleNotFoundError:
        return False
    return True
