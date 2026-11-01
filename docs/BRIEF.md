# Project: Aussie Car Listings Aggregator (self-hosted)

(Original brief, pasted by the user on 2026-11-01. Source of truth for scope and phases.)

Build a self-hosted web app that scrapes Australian used-car listings from multiple
sites, combines them into one searchable database, and estimates the market value of
every listing so I can see whether it's priced above or below market.

It must run on my local Ubuntu server with `docker compose up -d`. This is for my own
personal use only, not a public service.

Before writing any code, enter plan mode. Inspect each target site's current structure,
then propose the architecture, data model and phase plan for my approval. Build one
phase at a time and get each one working end to end before moving on.

## Sources (in priority order)
1. Carsales (carsales.com.au)
2. Gumtree (gumtree.com.au, cars & vehicles category)
3. Cars24 (cars24.com.au)
4. Drive Marketplace (drive.com.au)
5. Facebook Marketplace: optional and experimental. Only attempt it after everything
   else works. Keep it isolated so it can be disabled without affecting anything else.

Each source must be a self-contained plugin implementing a common interface
(e.g. `search(params) -> listings`, `fetch_detail(url) -> listing`). Adding or removing
a source should mean adding or removing a single module plus a config entry.
Each source can be enabled or disabled in config and through the UI.

## Scraping requirements
- Prefer hidden JSON/API endpoints or embedded JSON (e.g. `__NEXT_DATA__`, JSON-LD) over
  HTML parsing wherever a site exposes them. Fall back to Playwright (headless Chromium)
  only when needed.
- Be polite: a per-source rate limit with configurable delay and jitter, low concurrency,
  realistic headers, and exponential backoff on 429/403 errors. Respect robots.txt
  where practical and keep request volume low.
- Some sites (Carsales especially) use bot protection. If a source is blocked, log it
  clearly, mark the source "degraded" in the UI, and keep the other sources running.
  Never crash the whole job because of one source.
- Run scrapes on a schedule (configurable, default every 6 hours) and also on demand
  from the UI for a specific saved search.
- Scrape based on saved searches (e.g. "Toyota Corolla, 2015–2020, under 120,000 km,
  VIC"), not by crawling entire sites.
- Store raw source payloads alongside parsed data so I can re-parse later without
  re-scraping.

## Normalised listing data
Normalise every listing into a common schema, including at least:
source, source_listing_id, url, title, make, model, variant/badge, year, odometer_km,
price, price_type (drive-away / EGC / negotiable / unknown), body_type, transmission,
fuel_type, engine, drivetrain, colour, seller_type (dealer/private), location
(suburb, state, postcode), images, description, rego/VIN if shown,
first_seen, last_seen, is_active.

- Normalise make/model/variant names across sources (e.g. "VW" -> "Volkswagen",
  consistent model and badge naming). Keep a mapping table that's easy to extend.
- Track price history: record every price change with a timestamp.
- Mark a listing inactive when it disappears, and keep it, because sold listings are
  useful data for valuation.
- Cross-source deduplication: the same car is often listed on several sites. Match on
  VIN/rego when available, otherwise use a fuzzy match on make/model/year/odometer/
  price/location/images. Show it as a single car with links to every source.

## Market value estimation (the core feature)
For every listing, calculate an estimated market value and a deal rating.

- Find comparables in the database: same make + model, with a preference for the same
  variant, transmission and body type; year within ±2; odometer in a similar band.
  Widen criteria step by step if there are too few comps.
- Fit a simple model per make/model group (e.g. regression of log(price) on age,
  odometer, variant, transmission and seller_type). Fall back to a weighted median of
  the closest comps when data is thin.
- Remove outliers (e.g. IQR filter) and exclude obvious junk: $1 prices, "POA",
  write-offs, wrecking, parts-only listings and rentals (detect these from keywords).
- Output per listing:
  - estimated market value plus a range (e.g. 25th–75th percentile)
  - difference in $ and %
  - a deal label: Great deal / Good deal / Fair / Above market / Overpriced, with
    configurable thresholds
  - confidence (High/Medium/Low) based on comp count and spread
  - the comparables used, viewable in the UI so I can sanity-check the estimate
- Recalculate valuations after each scrape run.
- Keep the valuation logic in its own module with unit tests, so I can tune it later.

## Web UI
- Search across all sources at once with filters: make, model, variant, year range,
  price range, odometer range, state/location (plus radius from postcode if feasible),
  body type, transmission, fuel, seller type, source and deal rating.
- Sort by: price, deal % below market, newest, odometer, year.
- Results list or grid showing photo, key specs, price, market value, a coloured deal
  badge and source logos/links.
- Listing detail page with all specs, price history chart, valuation breakdown and
  comparables, plus links to the original listings.
- Saved searches with an optional "new listings / price drops" view.
- A sources status page: last run, listings found, errors, and enable/disable toggles.
- A clean, responsive design that works on my phone over the LAN.

## Tech stack (suggested; propose changes if you have good reasons)
- Backend: Python + FastAPI
- Scraping: httpx for JSON endpoints, Playwright for JS-heavy pages
- Jobs: a scheduled worker (e.g. APScheduler, or Celery/RQ with Redis)
- Database: PostgreSQL
- Frontend: React (Vite) or Next.js, or server-rendered templates if that's simpler
- Everything in Docker Compose: db, backend/api, worker, frontend
  (and Redis if used), with named volumes for persistence

## Deployment & ops
- `docker-compose.yml` plus a `.env.example` covering ports, schedule, rate limits and
  enabled sources.
- One command to start: `docker compose up -d`. Run database migrations automatically
  (e.g. Alembic).
- Structured logs viewable with `docker compose logs`.
- Restart policies set so everything comes back after a reboot.
- A README covering setup, configuration, adding a new source, and tuning valuation.

## Phases
1. Plan and confirm the architecture with me.
2. Compose skeleton, DB schema, FastAPI, and a basic UI showing listings.
3. First scraper (start with whichever source is easiest and most reliable), with
   normalisation and storage.
4. Remaining scrapers, one at a time, each tested against live data.
5. Deduplication and price history.
6. Market valuation module, with tests.
7. Full search/filter UI, listing detail, saved searches, sources status page.
8. Facebook Marketplace (experimental, optional).

After each phase, tell me how to test it and what's next.
