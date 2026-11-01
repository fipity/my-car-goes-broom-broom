"""Database schema. Every table for the project exists from phase 2; later phases fill them.

Enumerated values are stored as short strings (see the *Values classes) so adding a value
never needs a Postgres enum migration.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class PriceType:
    DRIVE_AWAY = "drive_away"
    EGC = "egc"
    NEGOTIABLE = "negotiable"
    UNKNOWN = "unknown"
    ALL = (DRIVE_AWAY, EGC, NEGOTIABLE, UNKNOWN)


class SellerType:
    DEALER = "dealer"
    PRIVATE = "private"
    UNKNOWN = "unknown"


class SourceStatus:
    OK = "ok"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    NEVER_RUN = "never_run"


class RunStatus:
    QUEUED = "queued"
    RUNNING = "running"
    OK = "ok"
    DEGRADED = "degraded"  # blocked / rate-limited: source marked degraded
    FAILED = "failed"      # unexpected error (bug, parse failure)


def _now() -> Any:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Source(Base):
    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # UI toggle
    status: Mapped[str] = mapped_column(String(16), default=SourceStatus.NEVER_RUN, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_count: Mapped[int | None] = mapped_column(Integer)


class SavedSearch(Base):
    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # make, model, variant, year_min, year_max, km_max, price_min, price_max, states[], ...
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    sources: Mapped[list[str] | None] = mapped_column(ARRAY(String(32)))  # None = all enabled
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = _now()

    runs: Mapped[list[ScrapeRun]] = relationship(back_populates="saved_search", passive_deletes=True)


class ScrapeRun(Base):
    """One (saved search x source) scrape. Rows with status=queued are the job queue."""

    __tablename__ = "scrape_runs"
    __table_args__ = (Index("ix_scrape_runs_status", "status", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    saved_search_id: Mapped[int | None] = mapped_column(
        ForeignKey("saved_searches.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(ForeignKey("sources.name"), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.QUEUED, nullable=False)
    queued_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gone: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    saved_search: Mapped[SavedSearch | None] = relationship(back_populates="runs")


class RawPayload(Base):
    """Source response kept verbatim so listings can be re-parsed without re-scraping.
    Postgres TOAST compresses large values automatically."""

    __tablename__ = "raw_payloads"
    __table_args__ = (Index("ix_raw_source_listing", "source", "source_listing_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_listing_id: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = _now()
    content_type: Mapped[str] = mapped_column(String(16), default="json", nullable=False)
    payload_json: Mapped[Any | None] = mapped_column(JSONB)
    payload_text: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Vehicle(Base):
    """A physical car; cross-source duplicates share one vehicle (phase 5)."""

    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vin: Mapped[str | None] = mapped_column(String(32), index=True)
    rego: Mapped[str | None] = mapped_column(String(16), index=True)
    canonical_listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL", use_alter=True,
                   name="fk_vehicles_canonical_listing")
    )
    match_method: Mapped[str | None] = mapped_column(String(16))  # vin | rego | fuzzy | single
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = _now()


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source", "source_listing_id", name="uq_listing_source_id"),
        Index("ix_listings_make_model_year", "make", "model", "year"),
        Index("ix_listings_active_seen", "is_active", "last_seen"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_listing_id: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)

    make: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    variant: Mapped[str | None] = mapped_column(String(128))
    year: Mapped[int | None] = mapped_column(Integer)
    odometer_km: Mapped[int | None] = mapped_column(Integer, index=True)
    price: Mapped[int | None] = mapped_column(Integer, index=True)  # whole AUD
    price_type: Mapped[str] = mapped_column(String(16), default=PriceType.UNKNOWN, nullable=False)
    body_type: Mapped[str | None] = mapped_column(String(32))
    transmission: Mapped[str | None] = mapped_column(String(16))
    fuel_type: Mapped[str | None] = mapped_column(String(32))
    engine: Mapped[str | None] = mapped_column(String(64))
    drivetrain: Mapped[str | None] = mapped_column(String(16))
    colour: Mapped[str | None] = mapped_column(String(32))
    seller_type: Mapped[str] = mapped_column(String(16), default=SellerType.UNKNOWN, nullable=False)
    suburb: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str | None] = mapped_column(String(3), index=True)
    postcode: Mapped[str | None] = mapped_column(String(4))
    images: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    rego: Mapped[str | None] = mapped_column(String(16))
    vin: Mapped[str | None] = mapped_column(String(32))

    first_seen: Mapped[datetime] = _now()
    last_seen: Mapped[datetime] = _now()
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    excluded_reason: Mapped[str | None] = mapped_column(String(32))  # junk filter (phase 6)

    latest_raw_id: Mapped[int | None] = mapped_column(
        ForeignKey("raw_payloads.id", ondelete="SET NULL")
    )
    vehicle_id: Mapped[int | None] = mapped_column(
        ForeignKey("vehicles.id", ondelete="SET NULL"), index=True
    )

    price_history: Mapped[list[PriceHistory]] = relationship(
        back_populates="listing", order_by="PriceHistory.observed_at", passive_deletes=True
    )
    valuation: Mapped[Valuation | None] = relationship(back_populates="listing", uselist=False)


class SearchListing(Base):
    """Which saved search returned which listing, and on which run last. When a complete run
    of a search no longer returns a listing it used to, that listing is checked and marked
    inactive if it's gone (see worker/runner.py)."""

    __tablename__ = "search_listings"

    saved_search_id: Mapped[int] = mapped_column(
        ForeignKey("saved_searches.id", ondelete="CASCADE"), primary_key=True
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    first_seen: Mapped[datetime] = _now()
    last_seen: Mapped[datetime] = _now()
    last_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("scrape_runs.id", ondelete="SET NULL")
    )


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (Index("ix_price_history_listing", "listing_id", "observed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), nullable=False
    )
    price: Mapped[int | None] = mapped_column(Integer)
    price_type: Mapped[str] = mapped_column(String(16), default=PriceType.UNKNOWN, nullable=False)
    observed_at: Mapped[datetime] = _now()

    listing: Mapped[Listing] = relationship(back_populates="price_history")


class Valuation(Base):
    __tablename__ = "valuations"

    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True
    )
    estimate: Mapped[int | None] = mapped_column(Integer)
    p25: Mapped[int | None] = mapped_column(Integer)
    p75: Mapped[int | None] = mapped_column(Integer)
    diff_abs: Mapped[int | None] = mapped_column(Integer)
    diff_pct: Mapped[float | None] = mapped_column(Float, index=True)
    label: Mapped[str | None] = mapped_column(String(16), index=True)
    confidence: Mapped[str | None] = mapped_column(String(8))
    method: Mapped[str | None] = mapped_column(String(16))
    n_comps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    comp_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=list, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    computed_at: Mapped[datetime] = _now()

    listing: Mapped[Listing] = relationship(back_populates="valuation")


class Postcode(Base):
    """For radius search (phase 7)."""

    __tablename__ = "postcodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    postcode: Mapped[str] = mapped_column(String(4), index=True, nullable=False)
    suburb: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(3), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
