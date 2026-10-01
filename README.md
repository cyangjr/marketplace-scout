# Marketplace Scout

Personal Local Deal-Scout: polls Facebook Marketplace, Craigslist, and eBay local pickup, verifies listings with a three-tier free cascade (hard filters → Groq text → conditional Gemini vision), filters by distance, and alerts via a dashboard + Discord.

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

## eBay local pickup

eBay local pickup uses the official Browse API on production marketplace `EBAY_US`. Create an eBay developer app and set `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` (client credentials). Searches are skipped when either value is unset, so a hunt that includes eBay still polls. Results stay subject to the hunt's distance check.

## Facebook pacing

FB searches reuse one browser session per poll cycle, stagger between hunts (~45s), and only run every `FB_POLL_MINUTES` (default 30). On login wall / checkpoint / block, a **circuit breaker** pauses Facebook for `FB_CIRCUIT_HOURS` (default 12) while Craigslist keeps running. Discord gets a status ping; reset from the dashboard or `POST /api/fb/reset-circuit` after re-login.

## Architecture

- **Tier 1** — hard filters (price, exclude keywords, dedup)
- **Tier 2** — Groq text match
- **Tier 3** — Gemini Flash vision (ambiguous / image-critical only)
- **Distance** — Nominatim + Haversine
- **Sources** — Craigslist adapter, Playwright Facebook Marketplace, and eBay Browse API local pickup
