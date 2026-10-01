from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from scout.config import Settings
from scout.sources import RawListing

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"
MARKETPLACE_ID = "EBAY_US"
# Refresh the application token a minute before eBay says it expires.
TOKEN_EXPIRY_BUFFER_SECONDS = 60
EBAY_MAX_LIMIT = 200


def build_search_filter(
    zip_code: str,
    radius_miles: float,
    max_price: float | None,
) -> str:
    """Browse API filter for US local pickup within radius of a ZIP.

    Field names match item_summary/search: buyingOptions, pickupCountry,
    pickupPostalCode, pickupRadius, pickupRadiusUnit, and (when a ceiling
    is set) price plus priceCurrency.
    """
    postal = _postal_code(zip_code)
    radius = _radius_miles(radius_miles)
    parts = [
        "buyingOptions:{FIXED_PRICE|AUCTION}",
        "pickupCountry:US",
        f"pickupPostalCode:{postal}",
        f"pickupRadius:{radius}",
        "pickupRadiusUnit:mi",
    ]
    if max_price is not None:
        parts.append(f"price:[..{_format_max_price(max_price)}]")
        parts.append("priceCurrency:USD")
    return ",".join(parts)


def map_item_summary(item: dict[str, Any]) -> RawListing | None:
    if not isinstance(item, dict):
        return None
    item_id = item.get("itemId")
    title = item.get("title")
    url = item.get("itemWebUrl")
    if item_id in (None, "") or not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(url, str) or not url.strip():
        return None

    title = title.strip()
    url = url.strip()
    loc = item.get("itemLocation") if isinstance(item.get("itemLocation"), dict) else {}
    location_text = _location_text(loc)
    lat, lng = _coordinates(loc)
    raw_text = _raw_text(title, item, location_text)

    return RawListing(
        source="ebay",
        external_id=str(item_id),
        url=url,
        title=title,
        price=_price_value(item.get("price")),
        location_text=location_text,
        lat=lat,
        lng=lng,
        images=_image_urls(item),
        raw_text=raw_text,
    )


class EbayAdapter:
    name = "ebay"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self.timeout = timeout
        self._access_token: str | None = None
        self._access_token_deadline: float = 0.0
        self._logged_unconfigured = False

    async def search(
        self,
        query: str,
        home_zip: str,
        max_price: float | None,
        *,
        max_miles: float | None = None,
    ) -> list[RawListing]:
        client_id, client_secret = self._credentials()
        if not client_id or not client_secret:
            self._log_unconfigured()
            return []

        miles = self.settings.default_max_miles if max_miles is None else max_miles
        try:
            async with self._client() as client:
                token = await self._get_token(client, client_id, client_secret)
                resp = await client.get(
                    SEARCH_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID,
                        "Accept": "application/json",
                    },
                    params={
                        "q": query,
                        "limit": self._result_limit(),
                        "filter": build_search_filter(home_zip, miles, max_price),
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception:
            logger.exception("eBay search failed for %r", query)
            return []

        if not isinstance(payload, dict):
            return []

        results: list[RawListing] = []
        for item in payload.get("itemSummaries") or []:
            try:
                listing = map_item_summary(item)
            except Exception:
                logger.exception("eBay item mapping failed")
                continue
            if listing is not None:
                results.append(listing)

        logger.info(
            "ebay search %r zip %s -> %d results", query, home_zip, len(results)
        )
        return results

    def _credentials(self) -> tuple[str, str]:
        return (
            self.settings.ebay_client_id.strip(),
            self.settings.ebay_client_secret.strip(),
        )

    def _log_unconfigured(self) -> None:
        if self._logged_unconfigured:
            return
        self._logged_unconfigured = True
        logger.info(
            "eBay Browse API credentials are not set "
            "(EBAY_CLIENT_ID / EBAY_CLIENT_SECRET); skipping eBay search"
        )

    def _result_limit(self) -> int:
        try:
            limit = int(self.settings.ebay_max_results)
        except (TypeError, ValueError):
            limit = 50
        if limit < 1:
            return 1
        if limit > EBAY_MAX_LIMIT:
            return EBAY_MAX_LIMIT
        return limit

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": self.timeout, "follow_redirects": False}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def _get_token(
        self, client: httpx.AsyncClient, client_id: str, client_secret: str
    ) -> str:
        now = time.monotonic()
        if self._access_token and now < self._access_token_deadline:
            return self._access_token

        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "scope": OAUTH_SCOPE,
            },
            auth=(client_id, client_secret),
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise RuntimeError("eBay token response missing access_token")

        try:
            expires_in = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in <= 0:
            expires_in = 7200

        self._access_token = token
        self._access_token_deadline = now + max(0, expires_in - TOKEN_EXPIRY_BUFFER_SECONDS)
        return token


def _postal_code(zip_code: str) -> str:
    digits = re.sub(r"\D", "", zip_code or "")
    if len(digits) >= 5:
        return digits[:5]
    return (zip_code or "").strip()


def _radius_miles(miles: float) -> int:
    try:
        radius = int(round(float(miles)))
    except (TypeError, ValueError):
        radius = 1
    return max(1, radius)


def _format_max_price(max_price: float) -> str:
    number = float(max_price)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _price_value(price: Any) -> float | None:
    if not isinstance(price, dict):
        return None
    value = price.get("value")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _location_text(loc: dict[str, Any]) -> str | None:
    city = loc.get("city")
    state = loc.get("stateOrProvince") or loc.get("state")
    parts: list[str] = []
    if isinstance(city, str) and city.strip():
        parts.append(city.strip())
    if isinstance(state, str) and state.strip():
        parts.append(state.strip())
    if not parts:
        return None
    return ", ".join(parts)


def _coordinates(loc: dict[str, Any]) -> tuple[float | None, float | None]:
    sources: list[dict[str, Any]] = [loc]
    for key in ("geoCoordinates", "coordinates"):
        nested = loc.get(key)
        if isinstance(nested, dict):
            sources.append(nested)
    for src in sources:
        lat = _first_float(src, ("latitude", "lat"))
        lng = _first_float(src, ("longitude", "lng", "lon"))
        if lat is not None and lng is not None:
            return lat, lng
    return None, None


def _first_float(src: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in src or src[key] in (None, ""):
            continue
        try:
            return float(src[key])
        except (TypeError, ValueError):
            continue
    return None


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    lower = url.lower()
    if lower.startswith("https://") or lower.startswith("http://"):
        return url
    return None


def _image_urls(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    image = item.get("image")
    if isinstance(image, dict):
        primary = _http_url(image.get("imageUrl"))
        if primary:
            urls.append(primary)
    additional = item.get("additionalImages")
    if isinstance(additional, list):
        for extra in additional:
            if not isinstance(extra, dict):
                continue
            url = _http_url(extra.get("imageUrl"))
            if url:
                urls.append(url)
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique


def _raw_text(title: str, item: dict[str, Any], location_text: str | None) -> str:
    notes: list[str] = []
    condition = item.get("condition")
    if isinstance(condition, str) and condition.strip():
        notes.append(f"Condition: {condition.strip()}")
    seller = item.get("seller")
    if isinstance(seller, dict):
        username = seller.get("username")
        if isinstance(username, str) and username.strip():
            notes.append(f"Seller: {username.strip()}")
    if location_text:
        notes.append(location_text)
    if not notes:
        return title
    return f"{title} — " + "; ".join(notes)
