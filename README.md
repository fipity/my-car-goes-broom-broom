# CarScout

A self-hosted aggregator for Australian used-car listings, for personal use. It runs your saved searches against several sites, stores every listing in one database with price history, and (from phase 6) estimates each car's market value.

Status: **phase 7 of 8 done.** **Cars24** and **Carsales** are live, the same car on both sites is shown once, every listing has a market value and deal rating, and the UI has full filters, saved-search alerts and price charts. Gumtree and Drive block all automated access with bot challenges, so they're marked unavailable. Phase 8 (Facebook Marketplace) is optional. See [`docs/PROGRESS.md`](docs/PROGRESS.md), and [`docs/BRIEF.md`](docs/BRIEF.md) for the full spec.

## Setup

```sh
cp .env.example .env          # then set POSTGRES_PASSWORD (and CARSCOUT_PORT if 8097 is taken)
docker compose up -d --build
```

Open `http://<server-ip>:8097`. Database migrations run automatically when the `api` container starts. All three containers use `restart: unless-stopped`, so they come back after a reboot.

| Container | Does |
|---|---|
| `db` | PostgreSQL 17. Data lives in the `carscout_pg` volume; the port isn't exposed. |
| `api` | Web UI + JSON API (`/api/docs`), health check at `/healthz`. |
| `worker` | Cron schedule and the scrape queue. Runs one scrape at a time per source. |

Logs are JSON, one line per event: `docker compose logs -f worker`.

## Using it

CarScout only fetches what your **saved searches** ask for. It doesn't crawl whole sites, so the Listings page holds the makes and models you've searched for. Add a search for every car you're interested in.

1. **Searches**: save a search, e.g. "Toyota Corolla 2015–2020, under 120,000 km, VIC". You can narrow it by variant, body, transmission and price, and limit it to particular sources. Scheduled searches run on `SCRAPE_CRON` (every 6 hours by default); **Run now** queues one immediately. Each search has its own page:
   - **New**: listings new to CarScout since you last pressed **Mark as seen**.
   - **Price drops**: price cuts since then, with the old price.
   - **All**: everything it currently matches, sortable by best deal.

   Editing a search's criteria resets which listings it tracks; the listings themselves are kept.
2. **Listings**: everything collected, one card per car, with a deal badge and the market value. The main filters are always visible. **More filters** adds variant, year, price and km ranges, body, transmission, fuel, seller, source, deal rating and **distance from a postcode**. Sort by best deal, price, newest, km, year, or nearest. Sold or removed cars are kept but hidden; tick **Include sold** to see them.
3. **A listing**: specs, photos, market value with its comparables, the same car on other sites, and a **price history chart** across every site it's listed on.
4. **Sources**: status, active listings, recent runs (✓ / ! / ✕) and on/off toggles.

**Distance search** uses suburb coordinates from GeoNames (CC BY 4.0), loaded on first start. Cars24 listings have a suburb. Carsales only gives a state, so with a radius set, Carsales listings in the postcode's state are included.

*Demo* (fake data) listings are hidden unless you pick Demo as the source. To delete them, run `cli purge-source demo`.

Maintenance commands:

```sh
docker compose exec api python -m carscout.cli purge-source demo   # delete one source's data
docker compose exec api python -m carscout.cli reparse cars24      # re-run the parser over stored raw payloads (then dedupe)
docker compose exec api python -m carscout.cli dedupe              # recompute which listings are the same car (then revalue)
docker compose exec api python -m carscout.cli revalue             # recompute valuations (after editing config/valuation.yaml)
docker compose exec api python -m carscout.cli load-postcodes      # reload postcode coordinates (done automatically on first start)
```

### When a listing counts as gone

Each run records which listings the saved search returned. When a run *completes* (not blocked, and not cut short by `max_results` or the source's page cap), listings the search returned before but not this time are checked with the source: Cars24 answers HTTP 410 for a sold car. Confirmed ones are marked inactive and kept, since sold prices are useful for valuation. A car that's still for sale but no longer matches the search (e.g. its price rose past your maximum) stays active. If a gone car shows up again, it's reactivated.

## Configuration

- **`.env`**: ports, database, schedule (`SCRAPE_CRON`, `SCHEDULE_TZ`) and politeness defaults (`DEFAULT_DELAY_SECONDS`, `DEFAULT_JITTER_SECONDS`, `MAX_RETRIES`, `BACKOFF_BASE_SECONDS`, `RESPECT_ROBOTS`). Optionally, `ENABLED_SOURCES` is an allowlist that overrides `sources.yaml`. Restart with `docker compose up -d` after changing it.
- **`config/sources.yaml`**: one entry per source, with `enabled`, `delay`, `jitter`, `concurrency`, `use_browser` and plugin `options`. It's re-read at the start of every run, so no restart is needed. `enabled: false` here wins over the UI toggle.

### How sources behave when a site pushes back

Each source goes through `PoliteClient` (`carscout/sources/http.py`), which:
- waits at least `delay` seconds between requests to a site, plus random jitter;
- honours robots.txt with RFC 9309 rules (longest match, `*` and `$` wildcards; the stdlib parser gets these wrong), and its `Crawl-delay` if that's longer than `delay`;
- on 429/403/5xx, retries with exponential backoff, using `Retry-After` when the site sends it.

If a site keeps refusing, or serves a bot-challenge page (Cloudflare, DataDome, PerimeterX, Imperva, Peakhour), CarScout stops for that run, marks the source **Degraded** with the reason, and carries on with the other sources. It does not try to get past bot protection.

## Source notes

| Source | Status | How |
|---|---|---|
| Cars24 | ✅ live | Make/model pages (`/buy-used-toyota-corolla/`, 20 cars a page) read from the embedded Next.js data, then one detail page per car for VIN, suburb, colour, body, drivetrain and engine. robots.txt disallows `?f=` filters, so year, km, price and state are filtered by CarScout after fetching. Detail pages are reused for up to `detail_max_age_days` while price, km and status are unchanged, so a repeat run of a 21-car search costs about 3 requests. Cars24 is a retailer: every listing is a dealer listing with an EGC price, located at a hub (Melbourne, Sydney, Brisbane). |
| Carsales | ✅ live, partial | Carsales puts a DataDome captcha on every URL with a query string (so no `?offset=` paging) and on every detail page. CarScout doesn't try to get past it, so it reads only the **first page (~25 cars) of SEO search paths** such as `/cars/used/toyota/corolla/victoria-state/hatch-bodystyle/`. To see more of a big search, it fans out over up to `max_pages` paths per run: one per state in the search, then splits by state, body style and transmission using the values seen on the first page. Year, km and price are filtered by CarScout. You get card data only (no VIN, suburb or description), and "sold" detection only runs when a search is small enough to fit on its first pages. Coverage builds up over runs as Carsales' "Featured" order changes. |
| Gumtree | ❌ unavailable | Peakhour bot challenge on every page, even for a headless browser. CarScout doesn't get around bot protection. |
| Drive | ❌ unavailable | Cloudflare Turnstile challenge on every URL, robots.txt included. |
| Facebook Marketplace | phase 8 (optional) | |

## Adding a source

1. Create `carscout/sources/<name>.py` with a `Plugin(SourcePlugin)` class. See `carscout/sources/base.py` for the contract, and `demo.py` for a complete example:
   - `async search(params, http)`: yield a `RawListing` (the original JSON/HTML) for each result. Use only `http.get(...)` for requests.
   - `parse(raw) -> NormalisedListing`: a pure function, with no network or database access. Raw payloads are stored, so a changed parser can re-parse old data (`cli reparse <name>`). Bump `parser_version` when you change it.
   - If the site can't filter server-side, call `carscout.sources.filters.matches(params, listing)` before yielding.
   - Optional: `confirm_gone(listing, http)` checks whether a listing that dropped out of a search was really sold (the default trusts the search). `self.previous(id)` returns the last stored payload, so a plugin can skip refetching detail pages that haven't changed (see `cars24.py`). Set `self.truncated = True` if you stop paging early.
   - Raise `SourceChanged` when the page loads but the data isn't where you expect it. That marks the source degraded with a clear message.
   - Raise `SourceBlocked` or `SourceUnavailable` when the site won't serve you. Any other exception counts as a bug: the run is marked *failed* and the source *degraded*. Other sources are unaffected either way.
2. Add an entry under `sources:` in `config/sources.yaml`.
3. Add name aliases to `carscout/normalise/data/makes.yaml` and `models.yaml`, and spec spellings (body, transmission, fuel, drivetrain) to `specs.yaml`, if the site spells things differently. Use `normalise_make/model/spec` from `carscout/normalise/mapping.py` in `parse()`.
4. Save a real page or two under `tests/fixtures/<name>/` and test against them with a fake transport (see `tests/test_cars24.py`).

To remove a source, delete the module and its config entry.

## Duplicates across sites

The same car is often listed on several sites. CarScout groups listings into **cars** (`vehicles` table) and shows each car once in the list, with the other sites' badges and prices under it. The listing page has a "Same car on other listings" table and a price history covering every listing of the car.

How two listings are judged to be the same car (`carscout/dedupe/match.py`):
1. **VIN**: the same VIN means the same car; two different VINs never are.
2. **Rego + state**: the same.
3. **Fuzzy**, between different sites only, and only when both show an odometer reading. Everything known on both sides must agree: make, model, state, body, transmission, fuel, drivetrain and colour. Then odometer (most weight), year, price and variant are scored. Groups use *complete linkage*: a listing joins a car only if it matches **every** listing already in it, so near-misses can't chain together.

Photos aren't compared, because that would mean downloading every image from the sites.

Dedupe runs after every scrape for the makes and models it touched. To recompute everything: `docker compose exec api python -m carscout.cli dedupe`.

### Tuning dedupe

Edit `MatchConfig` in `carscout/dedupe/match.py`, run `cli dedupe`, and check the results with `tests/test_dedupe.py`:

| Setting | Default | Meaning |
|---|---|---|
| `km_abs`, `km_pct` | 2000, 2% | Odometer readings further apart than the larger of these are never the same car |
| `price_pct` | 15% | Maximum price difference (sites differ on drive-away vs EGC) |
| `year_tolerance` | 1 | Build year vs model year |
| `threshold` | 0.75 | Fuzzy score needed. Lower means more merges and more false matches |
| `weights` | km .45, year .25, price .15, variant .15 | How the score is made up |

## Market value and deal ratings

Every listing gets an estimated market value, a typical range, the difference in $ and %, a deal label (Great deal / Good deal / Fair / Above market / Overpriced) and a confidence (High / Medium / Low). The listing page explains how the estimate was reached and lists the comparable cars, so you can sanity-check it. Valuations are recomputed after every scrape for the makes and models it touched.

How it works (`carscout/valuation/model.py`, pure functions with tests in `tests/test_valuation.py`):
- **Comparables**: same make and model, **one listing per physical car** (dedupe), including cars sold in the last 180 days. Junk is excluded, and so is fake Demo data. The criteria widen step by step until there are enough: same variant and specs within ±2 years and a similar odometer, then any variant, then any body or transmission, then ±4 years at any odometer. Outliers are dropped with an IQR filter.
- **Estimate**: a regression of log(price) on age, odometer, transmission, private vs dealer, variant and body, fitted per make and model. It's used when there are at least 30 cars, the fit is good (R² ≥ 0.5) and the car is inside the data's year and km range. Otherwise the estimate is the weighted median of the comparables (closer cars count more).
- **Range**: 25th–75th percentile, taken from how the nearby comparables scatter around the estimate. Old cars get wider ranges than new ones.
- **One price basis**: drive-away prices are converted to "excluding government charges" with a per-state factor before comparing, then the estimate is shown on the listing's own basis.
- **Confidence** reflects how much data backs the estimate and how tight the range is. It drops a level when the criteria had to be widened, and again when the regression and comparables disagree by more than 25%.
- **Junk** (not rated, not used as a comparable): no price or POA, under $500, write-offs (negations like "no write-offs on record" are handled), wrecking, parts-only and rentals. The listing shows "Not rated: …".

### Tuning valuation

Everything tunable is in **`config/valuation.yaml`**: label thresholds, drive-away factors, comparable tiers, outlier strictness, regression minimums, confidence levels and junk keywords. After editing, recompute with:

```sh
docker compose exec api python -m carscout.cli revalue
```

The code is in `carscout/valuation/`. Run `pytest tests/test_valuation.py` after changing it; the tests build synthetic markets with a known price formula and check the estimates against it.

## Development

```sh
docker compose run --rm api pytest            # tests (uses a separate carscout_test database)
docker compose run --rm --no-deps -v "$PWD/migrations:/app/migrations" api \
  alembic revision --autogenerate -m "describe change"   # after changing carscout/models
```

## Data model

`listings` (the common schema, unique per source and source ID) · `price_history` (one row per price change) · `search_listings` (which search returned which listing, for "gone" detection) · `raw_payloads` (verbatim source data, saved only when it changes) · `saved_searches` · `scrape_runs` (also the job queue) · `sources` · `vehicles` (one row per physical car; every listing points to one) · `valuations` (estimate, range, label, confidence and comparables per listing) · `postcodes` (suburb coordinates for distance search; GeoNames, CC BY 4.0).
