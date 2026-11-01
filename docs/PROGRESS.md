# CarScout progress

The brief is in [`BRIEF.md`](BRIEF.md). The approved architecture is in the "Architecture" section below. Update this file at the end of each phase.

## Phase status

| # | Phase | Status |
|---|---|---|
| 1 | Plan & architecture | ✅ Approved 2026-11-01 |
| 2 | Compose skeleton, DB schema, FastAPI, basic UI | ✅ Done 2026-11-01 |
| 3 | First scraper + normalisation + storage | ✅ Done 2026-11-01 (Cars24) |
| 4 | Remaining scrapers, one at a time | 🔄 In progress: Carsales ✅, Gumtree, Drive |
| 5 | Dedupe + price history | (price history already recorded since phase 2) |
| 6 | Valuation module + tests | |
| 7 | Full UI: filters, detail, saved-search alerts, sources page | |
| 8 | Facebook Marketplace (experimental) | |

## Architecture (approved in phase 1)

- **Stack:** Python 3.13, FastAPI, SQLAlchemy 2, Alembic and Postgres 17.
  - The UI is server-rendered Jinja2, not React. The brief allowed this; there's no Node on the server.
  - The worker uses APScheduler, with the `scrape_runs` table as the queue (LISTEN/NOTIFY plus polling). There's no Redis.
- **Containers:** `db`, `api` (UI + API + migrations, host port 8097) and `worker` (one run per source at a time, up to 4 sources in parallel).
- **Sources** are plugins (`carscout/sources/<name>.py` + `config/sources.yaml`), and all network access goes through `PoliteClient`.
  - Rule: no bot-protection evasion (CAPTCHA solving, proxy rotation, fingerprint spoofing). A blocked source is marked "degraded".
- **Site check** on 2026-11-01 (plain HTTP):
  - Carsales: 200 with `__NEXT_DATA__` and JSON-LD; DataDome present but passed.
  - Cars24: Next.js App Router with JSON-LD. robots.txt disallows `/api/`, crawl-delay 2.
  - Gumtree: 403 JS challenge. robots.txt disallows `*/search/*` and `?price=`.
  - Drive: Cloudflare challenge, even on robots.txt.
- **Phase 3 plan:** Cars24 first, then Carsales, if live tests confirm each works. Gumtree and Drive need Playwright (phase 4) and may stay degraded.

## Phase 2 notes

- The **Demo** source (fake data, off by default in the UI) exercises the whole flow. `options.mode: blocked | error` in `config/sources.yaml` tests degraded handling. Remove its data with `python -m carscout.cli purge-source demo`.
- `config/` is mounted as a directory, not a single file, because editors that replace the file broke a single-file bind mount.
- **Not done yet (deliberately):**
  - Marking listings inactive when they disappear needs search↔listing tracking. That's planned for phase 3, where the real "seen in this run" semantics exist.
  - Playwright isn't installed in the worker image yet (phase 4).
  - The junk filter (`excluded_reason`) comes in phase 6.
- 23 tests: config, normalisation, PoliteClient (retry, backoff, robots, challenge detection), runner (storage, price history, raw dedupe, isolation) and API/pages.

## Phase 3 notes

- **Cars24 is live** (`carscout/sources/cars24.py`), confirmed against the real site on 2026-11-01:
  - The first run of "Toyota Corolla, all states" stored 21 listings from 24 requests in 93s. Every listing has VIN, suburb, colour, drivetrain, engine and body type.
  - The repeat run took 3 requests and 7s, because detail pages are reused while price, km and status are unchanged (up to `detail_max_age_days`).
  - Search pages: RSC flight data. Detail pages: `window.__PRELOADED_STATE__`. Sold cars return HTTP **410**.
  - robots.txt disallows `?f=`/`?sf=`/`/api/`, so filters are applied client-side with `sources/filters.py`, first on the card (to skip detail fetches) and again after the detail page.
  - A model slug that 404s falls back to the make page, then the all-cars page.
- **Robots fix:** `sources/robots.py` implements RFC 9309. The stdlib `RobotFileParser` used in phase 2 applied the first matching rule, so Cars24's leading `Allow: /` would have allowed `/api/`. Crawl-delay now raises the per-source delay.
- **Gone detection:** the `search_listings` table plus `SourcePlugin.confirm_gone()`.
  - It only runs after complete runs: not degraded or failed, not stopped by `max_results`, not `plugin.truncated`.
  - It checks at most 50 listings per run. `scrape_runs.gone` counts them, and the UI shows it under Searches.
  - Phase 7 note: if saved searches become editable, clear that search's `search_listings` rows on edit. Otherwise the default `confirm_gone` (which trusts the search) would mark listings that no longer match as gone. Cars24 is safe either way because it checks each URL.
- **Normalisation:** `normalise/data/specs.yaml` covers body, transmission, fuel and drivetrain. Parsed strings are trimmed to their column lengths before saving. Fixed a LandCruiser/Prado alias clash.
- New plugin hooks:
  - `self.previous(id)`: the last stored payload.
  - `self.truncated`: set when the plugin stops paging early.
  - `SourceChanged`: raised when the layout changes; marks the source degraded with a clear message.
- `python -m carscout.cli reparse <source>` re-parses stored raw payloads. It doesn't add price history.
- UI: a "Gone" column in recent runs, an "Include sold" filter, and a photo strip on the listing page.
- 46 tests (23 new): robots, Cars24 against gzipped real-page fixtures (`tests/fixtures/cars24/`, staff emails and signed URLs scrubbed), gone detection, trimming, previous-payload lookup and reparse.
- **For phase 4 / later:**
  - Cars24 lists Mercedes models as "C200"/"A250" (the sitemap has `mercedes-benz-c200`), whereas other sites use "C-Class". Map them in models.yaml when Carsales arrives, for dedupe and valuation.
  - Cars24 gives no postcode. Fill it from suburb + state using the `postcodes` table (phase 7).
  - Next up: Carsales. In phase 1 it served `__NEXT_DATA__` and JSON-LD with DataDome passed; re-check live first.

## Phase 4 notes

### Carsales (done 2026-11-01)
- **Decision (user, 2026-11-01):** cards plus a modest fan-out, capped at `max_pages: 8` requests per search per run.
- **Why:**
  - Every URL with a query string (including `?offset=` paging) and every `/cars/details/` page gets a DataDome captcha. We don't get past it.
  - Only the first page of SEO paths is served: `/cars/used/<make>/<model>/[<badge>-badge/][<state>-state/][<body>-bodystyle/][<trans>-transmission/]`.
  - robots.txt also disallows `/cars?*`, `/cars/?*`, `?sort` and `/cars/details*?`. A test checks every path we build against it.
- **Data:** each page's `__NEXT_DATA__` holds a server-driven UI tree with `ListingCard`s (~21 organic + ~8 promoted).
  - Parsed fields: make/model/year/price/state/ad type (dealer, private, demo) from tracking metadata; body/engine/transmission/km from `keyDetailsTexts`; drive-away vs EGC from `priceInfo`; badge and drivetrain from the subtitle ("Ascent Sport Auto F", where F = FWD).
  - Not available: VIN, suburb, description, colour.
- **Fan-out:** first one page per state in the search. While budget remains, splits by state, body style and transmission, using values seen on the cards (most common first), so no requests go to empty paths.
- **Gone detection:** a run counts as complete, and the gone check runs, only when every base path fits on its first page.
- **Live test**, saved search "Corolla 2015-2020 VIC": 6 requests (base + hatch, sedan, auto, manual splits), 147 cards seen, 8 matched and stored. No captcha.
- **Normalisation:**
  - `models.yaml` gained `_rules` (regex). Mercedes "C200" (Cars24) → model "C-Class", variant "C200 …", matching Carsales. Cars24 `parser_version` is now 2. Run `cli reparse cars24` after deploying (no Mercedes in the DB yet).
  - `specs.yaml` gained the Carsales drivetrain shorthands (F, R, i-ACTIV AWD, GR-FOUR).
- **Robots:** groups now match on the UA product token ("Mozilla"), per RFC 9309, not on any substring.
- **Phase 6 idea:** the JSON-LD name carries the model series ("KF Series"), and it's kept in the raw payload. It would be a good comparable key if added as a column.
