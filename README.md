# CarScout

A self-hosted aggregator for Australian used-car listings, for personal use. It runs your saved searches against several sites, stores every listing in one database with price history, and (from phase 6) estimates each car's market value.

Status: **phase 2 of 8.** The app, database, worker, scheduler and basic UI run end to end, using a fake *Demo* source. Real scrapers start in phase 3. See [`docs/PROGRESS.md`](docs/PROGRESS.md), and [`docs/BRIEF.md`](docs/BRIEF.md) for the full spec.

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

1. **Sources**: turn sources on or off. *Demo* generates fake listings, so you can try everything before the real scrapers exist.
2. **Searches**: save a search, e.g. "Toyota Corolla 2015–2020, under 120,000 km, VIC". Scheduled searches run on `SCRAPE_CRON` (every 6 hours by default). **Run now** queues one immediately.
3. **Listings**: browse and filter everything collected. Each listing has its specs and price history.

To clear out the demo data later:

```sh
docker compose exec api python -m carscout.cli purge-source demo
```

## Configuration

- **`.env`**: ports, database, schedule (`SCRAPE_CRON`, `SCHEDULE_TZ`) and politeness defaults (`DEFAULT_DELAY_SECONDS`, `DEFAULT_JITTER_SECONDS`, `MAX_RETRIES`, `BACKOFF_BASE_SECONDS`, `RESPECT_ROBOTS`). Optionally, `ENABLED_SOURCES` is an allowlist that overrides `sources.yaml`. Restart with `docker compose up -d` after changing it.
- **`config/sources.yaml`**: one entry per source, with `enabled`, `delay`, `jitter`, `concurrency`, `use_browser` and plugin `options`. It's re-read at the start of every run, so no restart is needed. `enabled: false` here wins over the UI toggle.

### How sources behave when a site pushes back

Each source goes through `PoliteClient` (`carscout/sources/http.py`), which:
- waits at least `delay` seconds between requests to a site, plus random jitter;
- honours robots.txt;
- on 429/403/5xx, retries with exponential backoff, using `Retry-After` when the site sends it.

If a site keeps refusing, or serves a bot-challenge page (Cloudflare, DataDome, PerimeterX, Imperva), CarScout stops for that run, marks the source **Degraded** with the reason, and carries on with the other sources. It does not try to get past bot protection.

## Adding a source

1. Create `carscout/sources/<name>.py` with a `Plugin(SourcePlugin)` class. See `carscout/sources/base.py` for the contract, and `demo.py` for a complete example:
   - `async search(params, http)`: yield a `RawListing` (the original JSON/HTML) for each result. Use only `http.get(...)` for requests.
   - `parse(raw) -> NormalisedListing`: a pure function, with no network or database access. Raw payloads are stored, so a changed parser can re-parse old data. Bump `parser_version` when you change it.
   - Raise `SourceBlocked` or `SourceUnavailable` when the site won't serve you. Any other exception counts as a bug: the run is marked *failed* and the source *degraded*. Other sources are unaffected either way.
2. Add an entry under `sources:` in `config/sources.yaml`.
3. Add name aliases to `carscout/normalise/data/makes.yaml` and `models.yaml` if the site spells things differently.

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

`listings` (the common schema, unique per source and source ID) · `price_history` (one row per price change) · `raw_payloads` (verbatim source data, saved only when it changes) · `saved_searches` · `scrape_runs` (also the job queue) · `sources` · `vehicles` (cross-source dedupe, phase 5) · `valuations` (phase 6) · `postcodes` (radius search, phase 7).
