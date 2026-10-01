"""Slickdeals frontpage / popular-deals RSS. No login, no server-side search."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

import httpx

from scout.sources import RawListing
from scout.sources.keywords import parse_dollar_price, price_over_max, recall_match

logger = logging.getLogger(__name__)

FRONTPAGE_RSS = "https://slickdeals.net/newsearch.php?mode=frontpage&rss=1"
POPDEALS_RSS = (
    "https://slickdeals.net/newsearch.php?mode=popdeals&searcharea=deals&searchin=first&rss=1"
)
FEED_URLS = (FRONTPAGE_RSS, POPDEALS_RSS)

_IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(parent: ET.Element, name: str) -> str:
    for child in list(parent):
        if _local_tag(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _plain(text: str) -> str:
    cleaned = _TAG_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _http_url(value: str | None) -> bool:
    return bool(value) and (value.startswith("https://") or value.startswith("http://"))


def _item_images(item: ET.Element, description: str) -> list[str]:
    images: list[str] = []
    for child in list(item):
        if _local_tag(child.tag) != "enclosure":
            continue
        url = child.get("url") or ""
        if _http_url(url) and url not in images:
            images.append(url)
    for src in _IMG_RE.findall(description or ""):
        if _http_url(src) and src not in images:
            images.append(src)
    return images


def parse_slickdeals_rss(
    xml_text: str, query: str, max_price: float | None
) -> list[RawListing]:
    """Parse one RSS document. Keyword and price filters run client-side."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("slickdeals rss did not parse")
        return []

    listings: list[RawListing] = []
    seen: set[str] = set()
    for item in root.iter():
        if _local_tag(item.tag) != "item":
            continue
        title = _child_text(item, "title")
        link = _child_text(item, "link")
        description = _child_text(item, "description")
        guid = _child_text(item, "guid") or link
        if not title or not guid or guid in seen:
            continue
        if not recall_match(query, f"{title}\n{description}"):
            continue
        price = parse_dollar_price(title)
        if price_over_max(price, max_price):
            continue
        seen.add(guid)
        listings.append(
            RawListing(
                source="slickdeals",
                external_id=guid,
                url=link or guid,
                title=title,
                price=price,
                images=_item_images(item, description),
                raw_text=f"{title} {_plain(description)}".strip(),
            )
        )
    return listings


class SlickdealsAdapter:
    name = "slickdeals"

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    async def _fetch_feed(
        self,
        client: httpx.AsyncClient,
        url: str,
        query: str,
        max_price: float | None,
    ) -> list[RawListing]:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("slickdeals feed failed %s: %s", url, exc)
            return []
        return parse_slickdeals_rss(resp.text, query, max_price)

    async def search(
        self, query: str, home_zip: str, max_price: float | None
    ) -> list[RawListing]:
        # National feed: zip is unused. Slickdeals ignores search= on these RSS URLs.
        del home_zip
        headers = {
            "User-Agent": "marketplace-scout/0.1 (personal deal scout)",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        }
        listings: list[RawListing] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, headers=headers
        ) as client:
            for url in FEED_URLS:
                for listing in await self._fetch_feed(client, url, query, max_price):
                    if listing.external_id in seen:
                        continue
                    seen.add(listing.external_id)
                    listings.append(listing)
        logger.info("slickdeals search %r -> %d results", query, len(listings))
        return listings
