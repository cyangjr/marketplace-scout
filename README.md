# Marketplace Scout

Personal Local Deal-Scout: polls Facebook Marketplace and Craigslist, verifies listings with a three-tier free cascade (hard filters → Groq text → conditional Gemini vision), filters by distance, and alerts via a dashboard + Discord.

## Setup

```bash
cd marketplace-scout
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
playwright install chromium
cp .env.example .env
# fill GROQ_API_KEY, optional GEMINI_API_KEY, DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID

# Dashboard frontend
cd web && npm install && npm run build && cd ..

python -m scout.main
```

Open http://127.0.0.1:8765

### Facebook session (Phase 2)

On first FB hunt poll, if not logged in, run once:

```bash
python -m scout.sources.fb_login
```

Log into Facebook in the browser window, then close it. Session persists in `data/playwright_profile/`.

## Env

See `.env.example`. Minimum for Craigslist path: `GROQ_API_KEY`. Discord and Gemini are optional until you want alerts / vision.

## Facebook pacing

FB searches reuse one browser session per poll cycle, stagger between hunts (~45s), and only run every `FB_POLL_MINUTES` (default 30). On login wall / checkpoint / block, a **circuit breaker** pauses Facebook for `FB_CIRCUIT_HOURS` (default 12) while Craigslist keeps running. Discord gets a status ping; reset from the dashboard or `POST /api/fb/reset-circuit` after re-login.

## Craigslist

Search uses the JSON results endpoint the Craigslist site itself loads (`sapi.craigslist.org`, batch of up to 360 newest rows). The request includes the hunt ZIP, `search_distance` from `max_miles`, `query`, `sort=date`, and `max_price` when set. Results are capped by `CL_MAX_RESULTS` (default 120). If that request asks for more than the first batch and the response includes a cache timestamp, a second call fetches the rest and the adapter keeps only the cap. If the JSON request fails, search falls back to the public HTML pages (`s` offset, up to `CL_MAX_PAGES`). The live HTML search currently ignores `s` and repeats the first page, so the JSON path is the one that returns a full page of distinct listings. Posting pages are fetched only for rows that pass the tier-1 hard filter and whose text is still empty or just the title, capped by `CL_DETAIL_LIMIT` (default 15) with `CL_DETAIL_DELAY_SECONDS` (default 0.4) between those GETs. A failed detail fetch keeps the search card.

## Architecture

- **Tier 1** — hard filters (price, exclude keywords, dedup)
- **Tier 2** — Groq text match
- **Tier 3** — Gemini Flash vision (ambiguous / image-critical only)
- **Distance** — Nominatim + Haversine
- **Sources** — Craigslist adapter + Playwright Facebook Marketplace
