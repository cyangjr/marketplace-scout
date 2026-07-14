from __future__ import annotations

import logging
import re
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup

from scout.sources import RawListing

logger = logging.getLogger(__name__)

# Common US Craigslist site mapping by ZIP prefix (best-effort).
# Falls back to geo.craigslist.org search which redirects.
ZIP_SITE_HINTS: dict[str, str] = {
    "100": "newyork",
    "101": "newyork",
    "102": "newyork",
    "103": "newyork",
    "104": "newyork",
    "11": "newyork",
    "07": "newjersey",
    "08": "newjersey",
    "19": "philadelphia",
    "20": "washingtondc",
    "21": "baltimore",
    "30": "atlanta",
    "33": "miami",
    "60": "chicago",
    "70": "houston",
    "75": "dallas",
    "80": "denver",
    "85": "phoenix",
    "90": "losangeles",
    "94": "sfbay",
    "98": "seattle",
}


def site_for_zip(zip_code: str) -> str:
    z = re.sub(r"\D", "", zip_code)[:5]
    for prefix, site in sorted(ZIP_SITE_HINTS.items(), key=lambda x: -len(x[0])):
        if z.startswith(prefix):
            return site
    return "newyork"


def _parse_price(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


class CraigslistAdapter:
    name = "craigslist"

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    async def search(
        self, query: str, home_zip: str, max_price: float | None
    ) -> list[RawListing]:
        site = site_for_zip(home_zip)
        params = f"query={quote_plus(query)}&sort=date"
        if max_price is not None:
            params += f"&max_price={int(max_price)}"
        url = f"https://{site}.craigslist.org/search/sss?{params}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, "lxml")
        results: list[RawListing] = []
        seen: set[str] = set()

        # Newer CL markup: li.cl-static-search-result or ol.cl-static-search-results
        cards = soup.select("li.cl-static-search-result, li.cl-search-result, .result-row")
        if not cards:
            # Fallback: any result links
            cards = soup.select("ol.cl-static-search-results li, ul.rows li")

        for card in cards[:40]:
            link = card.select_one("a[href]")
            if not link:
                continue
            href = link.get("href", "")
            if not href or "/search/" in href:
                continue
            full_url = urljoin(f"https://{site}.craigslist.org/", href)
            # external id from path
            m = re.search(r"/(\d+)\.html", full_url)
            external_id = m.group(1) if m else full_url
            if external_id in seen:
                continue
            seen.add(external_id)

            title_el = card.select_one(".title, .result-title, a")
            title = (title_el.get_text(strip=True) if title_el else "") or link.get_text(strip=True)
            if not title:
                continue

            price_el = card.select_one(".priceinfo, .result-price, .price")
            price = _parse_price(price_el.get_text() if price_el else None)

            loc_el = card.select_one(".location, .result-hood, .meta")
            location_text = loc_el.get_text(strip=True) if loc_el else None
            if location_text:
                location_text = location_text.strip("() ")

            img_el = card.select_one("img")
            images: list[str] = []
            if img_el:
                src = img_el.get("src") or img_el.get("data-src")
                if src and src.startswith("http"):
                    images.append(src)

            results.append(
                RawListing(
                    source=self.name,
                    external_id=str(external_id),
                    url=full_url,
                    title=title,
                    price=price,
                    location_text=location_text,
                    images=images,
                    raw_text=title,
                )
            )

        logger.info("craigslist search %r on %s -> %d results", query, site, len(results))
        return results
