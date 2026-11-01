# CarScout progress

The brief is in [`BRIEF.md`](BRIEF.md). The approved architecture is in the "Architecture" section below. Update this file at the end of each phase.

## Phase status

| # | Phase | Status |
|---|---|---|
| 1 | Plan & architecture | ✅ Approved 2026-11-01 |
| 2 | Compose skeleton, DB schema, FastAPI, basic UI | ✅ Done 2026-11-01 |
| 3 | First scraper + normalisation + storage | ✅ Done 2026-11-01 (Cars24) |
| 4 | Remaining scrapers, one at a time | ✅ Done 2026-11-01: Carsales live; Gumtree and Drive unavailable (bot protection) |
| 5 | Dedupe + price history | ✅ Done 2026-11-01 |
| 6 | Valuation module + tests | ✅ Done 2026-11-01 |
| 7 | Full UI: filters, detail, saved-search alerts, sources page | ✅ Done 2026-11-01 |
| 8 | Facebook Marketplace (experimental) | ⏭ Optional |

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

### Gumtree and Drive (checked 2026-11-01): unavailable
- **Gumtree:** every page returns a **Peakhour** JS challenge (HTTP 403, `PEAKHOUR_VISIT` cookie), to both plain HTTP and stock headless Chromium (Playwright 1.49, which reports "HeadlessChrome").
- **Drive:** a **Cloudflare Turnstile** challenge (`cf-mitigated: challenge`) on every URL, robots.txt included, again for both plain HTTP and headless Chromium.
- The only remaining options are disguising the browser (UA or fingerprint changes, stealth plugins) or solving challenges. Both are excluded by the no-evasion rule, so both sources are `implemented: false` with an `unavailable:` note, which the Sources page shows.
- **Playwright** was not added to the worker image (~2 GB): neither site works with it.
- `PoliteClient` now recognises Peakhour challenges, so it stops at the first one instead of retrying the 403.
- **To revisit:** re-run the probe now and then; protection can change. If either site opens up, it needs a parser written from real pages first.

## Phase 5 notes

- **Matching:** `carscout/dedupe/match.py` (pure). VIN (exact, and different VINs veto), then rego + state, then a fuzzy match across sources.
  - The fuzzy match needs an odometer reading on both sides, and every field known on both sides must agree: make, model, state, body, transmission, fuel, drivetrain, colour.
  - It then scores odometer .45, year .25, price .15, variant .15, against a threshold of .75. Tolerances: km max(2000, 2%), price 15%, year ±1. Tunable via `MatchConfig`; the README has a table.
- **Clustering:** complete linkage (no chaining). Candidate pairs come from VIN/rego indexes plus an odometer sweep, not all n² pairs.
- **Storage:** `carscout/dedupe/store.py`.
  - Every listing gets a `vehicles` row, with `match_method` (vin/rego/fuzzy/single), `confidence` and `canonical_listing_id` (active, then most complete, then freshest).
  - Vehicle ids stay stable across recomputes, and orphan vehicles are deleted. A pg advisory lock serialises runs.
- **When it runs:** after every scrape for the (make, model) blocks the run touched, after `purge-source` and `reparse`, and via `cli dedupe` for everything.
- **UI/API:**
  - The list shows one card per car, counted as cars; the face is the canonical listing, or the matching one when filtered by source. Under it: "Also on" badges with each site's price.
  - The listing page has a "Same car on other listings" table (with how it was matched) and a price history covering every listing of the car.
  - The API adds `same_car`, `car_price_history`, `match_method` and `match_confidence`.
- **Live result:** "Corolla, any year, all states" on both sources gave 171 listings = 170 cars.
  - The one cross-site match is genuine: a 2021 Corolla ZR Hybrid in QLD, 62,994 km, $28,490 on both Cars24 and Carsales. Cars24 lists its stock on Carsales.
  - Test-search change: search 2 now has sources `{cars24, carsales}` (it was Cars24-only).
- **Normalisation:** `normalise_variant` upper-cases badges ("Zr" → "ZR"). Parser versions bumped: cars24 v3, carsales v2. Both re-parsed.
- **Not done (by design):** photo comparison, which would mean downloading images from the sites' CDNs.
- **Phase 6 note:** the valuation should use one listing per vehicle (the canonical one), so a car listed twice doesn't count as two comparables.

## Phase 6 notes

- **Module:** `carscout/valuation/`, with `model.py` (pure), `junk.py`, `config.py` and `store.py` (DB). Settings live in `config/valuation.yaml`, re-read on every recompute. numpy was added as a dependency.
- **Comparables:** one listing per vehicle (canonical first), sold listings ≤180 days old, junk excluded, synthetic (demo) sources excluded. Four widening tiers until `min_comps` (6) survive the IQR filter (k 1.5). Weights favour closer year/km/variant/specs, and sold listings ×0.8.
- **Estimate:** ridge regression of log(EGC price) on age, age², km, km², manual, private, and variant/body dummies (categories with ≥5 cars, the commonest as baseline). Standardised features; refitted once without IQR residual outliers. Used when n ≥ 30, R² ≥ 0.5 and the target is within the data's year and km range; otherwise the comps' weighted median.
- **Range:** the 25th–75th percentile of residuals, widened to the nearby comps' residual spread when there are ≥5 comps. Prices scatter far more for old cars, and the global spread understated it.
- **Price basis:** drive-away × a per-state factor gives EGC. Factors were measured from 36 Carsales listings showing both: VIC 0.958, QLD 0.970, default 0.96.
- **Confidence:** High/Medium/Low from evidence (group n for regression, comps n otherwise) and spread. Lowered a level when the tier was widened, and again when regression and comps disagree by more than 25%.
- **Junk:** no_price, low_price (<$500), write_off, wrecking, parts_only, rental, with a negation strip ("No write offs on record" in Cars24's history text is not junk). Stored in `listings.excluded_reason`; junk gets no valuation row.
- **New source flag:** `synthetic: true` (on demo). Synthetic listings are deduped only among themselves and are never used as comparables. Before this, 12 fake Corollas were in the pool.
- **Runs:** after dedupe in each scrape run (touched blocks), and via `cli revalue`. `cli dedupe` and `cli reparse` chain into revalue.
- **UI/API:**
  - Cards show a deal badge, % vs market and the market value; "Best deal" sort.
  - The listing page has a Market value section (estimate, range, confidence, explanation including the per-year and per-10k-km effects) and a comparables table.
  - The API adds `valuation` and `excluded_reason` on listings, and `sort=deal`.
- **Live (Toyota Corolla, 159 real listings):** regression on 147 cars, R² 0.984; the effect of +1 year is about −7%, +10k km about −2.8%.
  - Labels: Fair 87, Good 29, Above 30, Great 5, Overpriced 8.
  - The "Great deals" are old, high-km cars with Low/Medium confidence, which is expected.
- **Phase 7:** add a deal-rating filter. Saved-search "new / price drop" views can use valuations too.

## Phase 7 notes

- **Filters (list and API):** variant, year/price/km ranges, body, transmission, fuel, seller, source, deal labels (multi), and postcode + radius. New sorts: `deal` and `distance`.
  - The phone layout keeps the main filters visible and puts the rest under a "More filters" panel (two columns, open when any are set).
- **Distance:**
  - GeoNames AU postcodes (CC BY 4.0) are in `carscout/geo/data/au_postcodes.tsv.gz`, loaded into `postcodes` on API start when the table is empty (`cli load-postcodes [--if-empty]`).
  - Listings gained `lat`/`lon`: Cars24 coordinates come from its detail page (parser v4); otherwise from suburb + state on store/reparse, which also fills a missing postcode.
  - Distance is haversine in SQL (no PostGIS). Carsales has no suburb, so with a radius set its listings count as in range when they're in the postcode's state; the UI says so.
- **Saved searches:**
  - `/searches/{id}` has New / Price drops / All tabs and "Mark as seen" (`saved_searches.seen_at`).
  - "New" uses `listings.first_seen` (new to CarScout), so editing a search doesn't flood it.
  - Price drops come from `price_history` with lag() since `seen_at`: the price before the first change in the window vs now, biggest drop per car.
  - Editing (HTML form, `PATCH /api/searches/{id}`) clears the search's `search_listings` when params or sources change, as noted in phase 3.
  - The search form gained variant, body, transmission, min price and source checkboxes.
  - API: `GET /api/searches/{id}/changes`, `POST /api/searches/{id}/seen`.
- **Price chart:** `web/charts.py` builds a server-side SVG step line per source.
  - Fixed colour slot per source (validated with the dataviz palette checks for both themes: CVD ΔE ≥ 9.2, normal-vision ≥ 20.9). The light-mode aqua is under 3:1 contrast, which is covered by the legend, end labels and the table.
  - Hover titles on points.
  - Drawn only when some listing's price actually changed.
  - A 420-unit canvas keeps text readable on phones.
  - Checked by screenshots in light and dark.
- **Sources page:** active listings per source (linked) and a strip of the last 8 runs.
- **Phone polish:** compact result rows (photo left), two-column specs and form fields.
- **Demo:** synthetic sources are hidden from listings unless chosen as the source (a fake car was topping "Best deal").
- **Tests:** 113. New `tests/test_phase7_ui.py` covers filters, radius, location fill, search changes/seen/edit, the chart, and source counts.
