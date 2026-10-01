# Marketplace Scout

Personal deal-scout: polls Facebook Marketplace and Craigslist for local pickup, and Slickdeals plus Reddit for national online deals. Listings go through a three-tier free cascade (hard filters → Groq text → conditional Gemini vision). Local hunts filter by distance. Alerts land on the dashboard and Discord.

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

## Online hunts

Slickdeals and Reddit are national online retail feeds. They are not local pickup, so an online hunt (`kind=online`) does not apply the distance check — `drive_miles` stays empty. Local hunts (Facebook Marketplace and Craigslist) still use max miles.

- **Slickdeals** polls the public frontpage RSS and the popular-deals RSS. Those feeds ignore a server-side `search=`, so titles and descriptions are filtered client-side by hunt keywords.
- **Reddit** polls public `new.json` (no OAuth) for up to four subreddits from `REDDIT_SUBREDDITS` (default `deals,buildapcsales`). Stickied posts are skipped.

Keyword matching is only a recall filter. The existing verifier still decides whether a listing is the item you want. If one feed request fails, that feed is skipped and the hunt continues.

## Architecture

- **Tier 1** — hard filters (price, exclude keywords, dedup)
- **Tier 2** — Groq text match
- **Tier 3** — Gemini Flash vision (ambiguous / image-critical only)
- **Distance** — Nominatim + Haversine for local hunts only
- **Sources** — Craigslist, Playwright Facebook Marketplace, Slickdeals RSS, Reddit JSON
