# CarScout progress

The brief is in [`BRIEF.md`](BRIEF.md). The approved architecture is in the "Architecture" section below. Update this file at the end of each phase.

## Phase status

| # | Phase | Status |
|---|---|---|
| 1 | Plan & architecture | ✅ Approved 2026-11-01 |
| 2 | Compose skeleton, DB schema, FastAPI, basic UI | ✅ Done 2026-11-01 |
| 3 | First scraper + normalisation + storage | ⏭ Next |
| 4 | Remaining scrapers, one at a time | |
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
