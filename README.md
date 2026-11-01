# CarScout

A self-hosted aggregator for Australian used-car listings, for personal use. It runs your saved searches against several sites, stores every listing in one database with price history, and (from phase 6) estimates each car's market value.

Status: **phase 4 of 8, in progress.** **Cars24** and **Carsales** are live; Gumtree and Drive are next. See [`docs/PROGRESS.md`](docs/PROGRESS.md), and [`docs/BRIEF.md`](docs/BRIEF.md) for the full spec.

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

1. **Sources**: turn sources on or off. *Cars24* and *Carsales* are live. *Demo* generates fake listings for trying things out.
2. **Searches**: save a search, e.g. "Toyota Corolla 2015–2020, under 120,000 km, VIC". Scheduled searches run on `SCRAPE_CRON` (every 6 hours by default). **Run now** queues one immediately.
3. **Listings**: browse and filter everything collected. Each listing has its specs, photos and price history. Sold or removed cars are kept but hidden; tick **Include sold** to see them.

Maintenance commands:

```sh
docker compose exec api python -m carscout.cli purge-source demo   # delete one source's data
docker compose exec api python -m carscout.cli reparse cars24      # re-run the parser over stored raw payloads
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

If a site keeps refusing, or serves a bot-challenge page (Cloudflare, DataDome, PerimeterX, Imperva), CarScout stops for that run, marks the source **Degraded** with the reason, and carries on with the other sources. It does not try to get past bot protection.

## Source notes

| Source | Status | How |
|---|---|---|
| Cars24 | ✅ live | Make/model pages (`/buy-used-toyota-corolla/`, 20 cars a page) read from the embedded Next.js data, then one detail page per car for VIN, suburb, colour, body, drivetrain and engine. robots.txt disallows `?f=` filters, so year, km, price and state are filtered by CarScout after fetching. Detail pages are reused for up to `detail_max_age_days` while price, km and status are unchanged, so a repeat run of a 21-car search costs about 3 requests. Cars24 is a retailer: every listing is a dealer listing with an EGC price, located at a hub (Melbourne, Sydney, Brisbane). |
| Carsales | ✅ live, partial | Carsales puts a DataDome captcha on every URL with a query string (so no `?offset=` paging) and on every detail page. CarScout doesn't try to get past it, so it reads only the **first page (~25 cars) of SEO search paths** such as `/cars/used/toyota/corolla/victoria-state/hatch-bodystyle/`. To see more of a big search, it fans out over up to `max_pages` paths per run: one per state in the search, then splits by state, body style and transmission using the values seen on the first page. Year, km and price are filtered by CarScout. You get card data only (no VIN, suburb or description), and "sold" detection only runs when a search is small enough to fit on its first pages. Coverage builds up over runs as Carsales' "Featured" order changes. |
| Gumtree, Drive | phase 4 | |
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

## Tuning valuation

Comes with phase 6. The valuation logic will live in `carscout/valuation/`, as pure functions with unit tests.

## Development

```sh
docker compose run --rm api pytest            # tests (uses a separate carscout_test database)
docker compose run --rm --no-deps -v "$PWD/migrations:/app/migrations" api \
  alembic revision --autogenerate -m "describe change"   # after changing carscout/models
```

## Data model

`listings` (the common schema, unique per source and source ID) · `price_history` (one row per price change) · `search_listings` (which search returned which listing, for "gone" detection) · `raw_payloads` (verbatim source data, saved only when it changes) · `saved_searches` · `scrape_runs` (also the job queue) · `sources` · `vehicles` (cross-source dedupe, phase 5) · `valuations` (phase 6) · `postcodes` (radius search, phase 7).
