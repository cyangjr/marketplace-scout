from __future__ import annotations

import logging
import re
from dataclasses import replace
from urllib.parse import urlencode, urljoin

import httpx
from bs4 import BeautifulSoup

from scout.sources import RawListing

logger = logging.getLogger(__name__)

# Older HTML result pages were offset with `s` in steps of about 120.
# The current site ignores that offset and repeats the first page, so search()
# uses the JSON results the site itself loads, and falls back to one HTML pass.
SEARCH_PAGE_SIZE = 120
JSON_SEARCH_URL = "https://sapi.craigslist.org/web/v8/postings/search/full"
# Minimum batch the JSON search accepts. Newer rows come first when sort=date.
JSON_BATCH = "1-0-360-1-0"

# Used only when search() is called without max_miles. The worker passes the
# hunt value (or Settings.default_max_miles) explicitly.
DEFAULT_MAX_MILES = 25.0

_QR_BOILERPLATE = "QR Code Link to This Post"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

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


def _format_miles(miles: float) -> str:
    value = float(miles)
    if value.is_integer():
        return str(int(value))
    return str(value)


def _as_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def build_search_url(
    site: str,
    query: str,
    home_zip: str,
    max_price: float | None,
    max_miles: float,
    offset: int = 0,
) -> str:
    """Public HTML search URL. Does not call sapi.craigslist.org."""
    pairs: list[tuple[str, str]] = [
        ("query", query),
        ("sort", "date"),
        ("postal", home_zip),
        ("search_distance", _format_miles(max_miles)),
        ("s", str(offset)),
    ]
    if max_price is not None:
        pairs.append(("max_price", str(int(max_price))))
    return f"https://{site}.craigslist.org/search/sss?{urlencode(pairs)}"


def parse_search_results(html: str, site: str) -> list[RawListing]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("li.cl-static-search-result, li.cl-search-result, .result-row")
    if not cards:
        cards = soup.select("ol.cl-static-search-results li, ul.rows li")

    results: list[RawListing] = []
    for card in cards:
        link = card.select_one("a[href]")
        if not link:
            continue
        href = link.get("href", "")
        if not href or "/search/" in href:
            continue
        full_url = urljoin(f"https://{site}.craigslist.org/", href)
        external_id = _posting_id(full_url)

        title_el = card.select_one(".title, .result-title")
        if title_el:
            title = title_el.get_text(strip=True)
        else:
            title = link.get_text(strip=True)
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
                source="craigslist",
                external_id=str(external_id),
                url=full_url,
                title=title,
                price=price,
                location_text=location_text,
                images=images,
                raw_text=title,
            )
        )
    return results


def _posting_id(url: str) -> str:
    """Numeric id from older /123.html URLs, otherwise the token in /view/d/slug/TOKEN."""
    match = re.search(r"/(\d+)\.html", url)
    if match:
        return match.group(1)
    token = url.rstrip("/").split("/")[-1].split("?")[0]
    return token or url


def _image_url(token: str) -> str | None:
    # JSON image ids look like "3:00S0S_5Ha92TfkGlD_0t20CI".
    image_id = token.split(":", 1)[1] if ":" in token else token
    if not image_id or not re.fullmatch(r"[0-9A-Za-z_]+", image_id):
        return None
    return f"https://images.craigslist.org/{image_id}_600x450.jpg"


def _geo_point(value: str) -> tuple[str | None, float | None, float | None]:
    """Parse '1:1:1~40.7339~-74.0054' into a location index plus coordinates."""
    parts = value.split("~")
    if len(parts) < 3:
        return None, None, None
    index = parts[0].split(":")[-1] or None
    return index, _as_float(parts[-2]), _as_float(parts[-1])


def _listing_from_json_item(
    item: object, descriptions: list
) -> RawListing | None:
    if not isinstance(item, list) or not item:
        return None
    token: str | None = None
    slug: str | None = None
    price: float | None = None
    images: list[str] = []
    lat: float | None = None
    lng: float | None = None
    loc_index: str | None = None
    for part in item:
        if isinstance(part, str) and "~" in part:
            loc_index, lat, lng = _geo_point(part)
            continue
        if not isinstance(part, list) or not part:
            continue
        tag = part[0]
        if tag == 13 and len(part) > 1 and isinstance(part[1], str):
            token = part[1]
        elif tag == 6 and len(part) > 1 and isinstance(part[1], str):
            slug = part[1]
        elif tag == 10 and len(part) > 1:
            price = _parse_price(str(part[1]))
        elif tag == 4:
            for image in part[1:]:
                if isinstance(image, str):
                    url = _image_url(image)
                    if url and url not in images:
                        images.append(url)
    title = item[-1].strip() if isinstance(item[-1], str) else ""
    if not token or not title or "~" in title:
        return None
    if slug:
        url = f"https://www.craigslist.org/view/d/{slug}/{token}"
    else:
        url = f"https://www.craigslist.org/view/d/{token}"
    location_text = None
    if loc_index and loc_index.isdigit():
        idx = int(loc_index)
        if 0 <= idx < len(descriptions) and isinstance(descriptions[idx], str):
            location_text = descriptions[idx]
    return RawListing(
        source="craigslist",
        external_id=token,
        url=url,
        title=title,
        price=price,
        location_text=location_text,
        lat=lat,
        lng=lng,
        images=images,
        raw_text=title,
    )


def parse_json_search(payload: object) -> list[RawListing]:
    """Map a Craigslist search JSON document into listings."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    decode = data.get("decode") if isinstance(data.get("decode"), dict) else {}
    descriptions = decode.get("locationDescriptions") or []
    if not isinstance(descriptions, list):
        descriptions = []
    results: list[RawListing] = []
    seen: set[str] = set()
    for item in data.get("items") or []:
        listing = _listing_from_json_item(item, descriptions)
        if listing is None or listing.external_id in seen:
            continue
        seen.add(listing.external_id)
        results.append(listing)
    return results


def build_json_params(
    query: str,
    home_zip: str,
    max_price: float | None,
    max_miles: float,
    *,
    batch: str = JSON_BATCH,
) -> dict[str, str]:
    postal = re.sub(r"\D", "", home_zip)[:5]
    params = {
        "batch": batch,
        "cc": "us",
        "lang": "en",
        "postal": postal,
        "searchPath": "sss",
        "search_distance": _format_miles(max_miles),
        "query": query,
        "sort": "date",
    }
    if max_price is not None:
        params["max_price"] = str(int(max_price))
    return params


def _add_new_listings(
    results: list[RawListing],
    seen: set[str],
    found: list[RawListing],
    result_limit: int,
) -> int:
    added = 0
    for item in found:
        if len(results) >= result_limit:
            break
        if item.external_id in seen:
            continue
        seen.add(item.external_id)
        results.append(item)
        added += 1
    return added


def _posting_body_text(soup: BeautifulSoup) -> str:
    body = soup.select_one("section#postingbody")
    if body is None:
        return ""
    for el in body.select(".print-qrcode-container, .print-information, script, style"):
        el.decompose()
    text = body.get_text("\n", strip=True).replace(_QR_BOILERPLATE, "")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line and line != _QR_BOILERPLATE).strip()


def _parse_lat_lng(soup: BeautifulSoup) -> tuple[float | None, float | None]:
    candidates: list = []
    map_el = soup.select_one("#map")
    if map_el is not None:
        candidates.append(map_el)
    candidates.extend(soup.select("[data-latitude][data-longitude]"))
    for el in candidates:
        lat = _as_float(el.get("data-latitude"))
        lng = _as_float(el.get("data-longitude"))
        if lat is not None and lng is not None:
            return lat, lng
    return None, None


def _parse_detail_images(soup: BeautifulSoup) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(src: str | None) -> None:
        if not src:
            return
        cleaned = src.strip()
        if not cleaned.startswith("http") or cleaned in seen:
            return
        seen.add(cleaned)
        urls.append(cleaned)

    for anchor in soup.select("#thumbs a[href], a.thumb[href]"):
        add(anchor.get("href"))
    if urls:
        return urls
    for img in soup.select(".gallery img, .swipe img, figure img"):
        add(img.get("src") or img.get("data-src"))
    return urls


def parse_posting_page(html: str, listing: RawListing) -> RawListing:
    """Fill body, price, images, and coordinates from a posting HTML page."""
    soup = BeautifulSoup(html, "lxml")

    title = listing.title
    title_el = soup.select_one("span#titletextonly")
    if title_el:
        title_text = title_el.get_text(strip=True)
        if title_text:
            title = title_text

    price = listing.price
    price_el = soup.select_one("span.price")
    parsed_price = _parse_price(price_el.get_text() if price_el else None)
    if parsed_price is not None:
        price = parsed_price

    body = _posting_body_text(soup)
    lat, lng = _parse_lat_lng(soup)
    images = _parse_detail_images(soup)

    return replace(
        listing,
        title=title,
        price=price,
        raw_text=body if body else listing.raw_text,
        lat=lat if lat is not None else listing.lat,
        lng=lng if lng is not None else listing.lng,
        images=images or list(listing.images),
    )


class CraigslistAdapter:
    name = "craigslist"

    def __init__(
        self,
        timeout: float = 20.0,
        *,
        max_pages: int = 3,
        max_results: int = 120,
    ) -> None:
        self.timeout = timeout
        self.max_pages = max_pages
        self.max_results = max_results

    async def search(
        self,
        query: str,
        home_zip: str,
        max_price: float | None,
        *,
        max_miles: float | None = None,
        max_pages: int | None = None,
        max_results: int | None = None,
    ) -> list[RawListing]:
        miles = DEFAULT_MAX_MILES if max_miles is None else float(max_miles)
        page_limit = self.max_pages if max_pages is None else max_pages
        result_limit = self.max_results if max_results is None else max_results
        try:
            results = await self._search_json(query, home_zip, max_price, miles, result_limit)
        except Exception as exc:
            logger.warning("craigslist json search failed, using html: %s", exc)
            results = await self._search_html(
                query, home_zip, max_price, miles, page_limit, result_limit
            )
        logger.info("craigslist search %r -> %d results", query, len(results))
        return results

    async def _search_json(
        self,
        query: str,
        home_zip: str,
        max_price: float | None,
        miles: float,
        result_limit: int,
    ) -> list[RawListing]:
        params = build_json_params(query, home_zip, max_price, miles)
        headers = {**_HEADERS, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            resp = await client.get(JSON_SEARCH_URL, params=params, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
            rows = parse_json_search(payload)
            data = payload.get("data") if isinstance(payload, dict) else {}
            total = 0
            cache_ts = None
            if isinstance(data, dict):
                total = int(data.get("totalResultCount") or 0)
                cache_ts = data.get("cacheTs")
            # The first batch is capped at 360. A follow-up with the cache
            # timestamp returns the rest when the hunt asks for more.
            if (
                result_limit > len(rows)
                and total > len(rows)
                and cache_ts
            ):
                more = dict(params)
                more["batch"] = f"1-{cache_ts}-0-1-0"
                follow = await client.get(JSON_SEARCH_URL, params=more, headers=headers)
                follow.raise_for_status()
                fuller = parse_json_search(follow.json())
                if fuller:
                    rows = fuller
        if result_limit >= 0:
            return rows[:result_limit]
        return rows

    async def _search_html(
        self,
        query: str,
        home_zip: str,
        max_price: float | None,
        miles: float,
        page_limit: int,
        result_limit: int,
    ) -> list[RawListing]:
        site = site_for_zip(home_zip)
        results: list[RawListing] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for page_idx in range(page_limit):
                if len(results) >= result_limit:
                    break
                offset = page_idx * SEARCH_PAGE_SIZE
                url = build_search_url(site, query, home_zip, max_price, miles, offset)
                resp = await client.get(url, headers=_HEADERS)
                resp.raise_for_status()
                added = _add_new_listings(
                    results,
                    seen,
                    parse_search_results(resp.text, site),
                    result_limit,
                )
                if added == 0:
                    break
        return results

    async def fetch_detail(self, listing: RawListing) -> RawListing:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            resp = await client.get(listing.url, headers=_HEADERS)
            resp.raise_for_status()
            return parse_posting_page(resp.text, listing)
