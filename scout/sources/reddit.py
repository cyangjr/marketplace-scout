"""Public Reddit new.json feeds. No OAuth."""

from __future__ import annotations

import logging
import re

import httpx

from scout.config import Settings
from scout.sources import RawListing
from scout.sources.keywords import parse_dollar_price, price_over_max, recall_match

logger = logging.getLogger(__name__)

USER_AGENT = "marketplace-scout/0.1 (personal deal scout)"
MAX_SUBREDDITS = 4
_SUB_RE = re.compile(r"^[A-Za-z0-9_]{2,50}$")


def parse_subreddits(raw: str | None, *, limit: int = MAX_SUBREDDITS) -> list[str]:
    found: list[str] = []
    for part in (raw or "").split(","):
        name = part.strip()
        if name.lower().startswith("r/"):
            name = name[2:]
        name = name.strip()
        if not name or not _SUB_RE.match(name) or name.lower() in {s.lower() for s in found}:
            continue
        found.append(name)
        if len(found) >= limit:
            break
    return found


def _http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith("https://") or value.startswith("http://"):
        return value
    return None


def _absolute_permalink(permalink: str, post_id: str) -> str:
    if permalink.startswith("https://") or permalink.startswith("http://"):
        return permalink
    if permalink.startswith("/"):
        return f"https://www.reddit.com{permalink}"
    if permalink:
        return f"https://www.reddit.com/{permalink}"
    return f"https://www.reddit.com/comments/{post_id}"


def _plain_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def _html_images(html: str) -> list[str]:
    images: list[str] = []
    for src in re.findall(r"""<img[^>]+src=["']([^"']+)["']""", html or "", re.IGNORECASE):
        url = src.replace("&amp;", "&")
        if _http_url(url) and url not in images:
            images.append(url)
    return images


def parse_reddit_atom(
    xml_text: str, query: str, max_price: float | None
) -> list[RawListing]:
    """Map a Reddit Atom feed (/new/.rss). www.reddit.com JSON is often blocked."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("reddit atom feed did not parse")
        return []

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    listings: list[RawListing] = []
    seen: set[str] = set()
    for entry in root.iter():
        if local(entry.tag) != "entry":
            continue
        title = ""
        link = ""
        post_id = ""
        content = ""
        thumb: str | None = None
        for child in list(entry):
            tag = local(child.tag)
            if tag == "title" and child.text:
                title = child.text.strip()
            elif tag == "link":
                href = child.get("href") or ""
                rel = child.get("rel") or "alternate"
                if href and rel == "alternate":
                    link = href
            elif tag == "id" and child.text:
                post_id = child.text.strip()
            elif tag == "content" and child.text:
                content = child.text
            elif tag == "thumbnail":
                thumb = _http_url(child.get("url"))
        if post_id.startswith("t3_"):
            post_id = post_id[3:]
        title = title.strip()
        if not title or not post_id or post_id in seen:
            continue
        plain = _plain_html(content)
        if not recall_match(query, f"{title}\n{plain}"):
            continue
        price = parse_dollar_price(title)
        if price_over_max(price, max_price):
            continue
        images = _html_images(content)
        if thumb and thumb not in images:
            images.insert(0, thumb)
        seen.add(post_id)
        listings.append(
            RawListing(
                source="reddit",
                external_id=post_id,
                url=link or f"https://www.reddit.com/comments/{post_id}",
                title=title,
                price=price,
                images=images,
                raw_text=plain,
            )
        )
    return listings


def parse_reddit_payload(
    payload: dict, query: str, max_price: float | None
) -> list[RawListing]:
    """Map a Reddit listing JSON document. Stickied posts are skipped."""
    children = ((payload or {}).get("data") or {}).get("children") or []
    listings: list[RawListing] = []
    seen: set[str] = set()
    for child in children:
        data = (child or {}).get("data") or {}
        if data.get("stickied"):
            continue
        title = str(data.get("title") or "").strip()
        post_id = str(data.get("id") or "").strip()
        if not title or not post_id or post_id in seen:
            continue
        selftext = str(data.get("selftext") or "")
        if not recall_match(query, f"{title}\n{selftext}"):
            continue
        price = parse_dollar_price(title)
        if price_over_max(price, max_price):
            continue
        thumb = _http_url(data.get("thumbnail"))
        seen.add(post_id)
        listings.append(
            RawListing(
                source="reddit",
                external_id=post_id,
                url=_absolute_permalink(str(data.get("permalink") or ""), post_id),
                title=title,
                price=price,
                images=[thumb] if thumb else [],
                raw_text=selftext.strip(),
            )
        )
    return listings


class RedditAdapter:
    name = "reddit"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        subreddits: list[str] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.timeout = timeout
        if subreddits is not None:
            self.subreddits = parse_subreddits(",".join(subreddits))
        else:
            raw = settings.reddit_subreddits if settings is not None else "deals,buildapcsales"
            self.subreddits = parse_subreddits(raw)

    async def _fetch_sub(
        self,
        client: httpx.AsyncClient,
        sub: str,
        query: str,
        max_price: float | None,
    ) -> list[RawListing]:
        # JSON listing endpoints return 403 from many networks. The public Atom
        # feed is the one that answers. Fall back to JSON if Atom is not XML.
        atom_url = f"https://www.reddit.com/r/{sub}/new/.rss"
        try:
            resp = await client.get(atom_url)
            resp.raise_for_status()
            body = resp.text or ""
            if "<feed" in body[:800] or "http://www.w3.org/2005/Atom" in body[:800]:
                return parse_reddit_atom(body, query, max_price)
            logger.warning("reddit atom r/%s was not a feed (%s)", sub, resp.status_code)
        except Exception as exc:
            logger.warning("reddit atom feed failed r/%s: %s", sub, exc)

        url = f"https://www.reddit.com/r/{sub}/new.json"
        try:
            resp = await client.get(url, params={"limit": 25})
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("reddit json feed failed r/%s: %s", sub, exc)
            return []
        if not isinstance(payload, dict):
            logger.warning("reddit feed r/%s returned non-object JSON", sub)
            return []
        return parse_reddit_payload(payload, query, max_price)

    async def search(
        self, query: str, home_zip: str, max_price: float | None
    ) -> list[RawListing]:
        # National feed: zip is unused.
        del home_zip
        listings: list[RawListing] = []
        seen: set[str] = set()
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, headers=headers
        ) as client:
            for sub in self.subreddits[:MAX_SUBREDDITS]:
                for listing in await self._fetch_sub(client, sub, query, max_price):
                    if listing.external_id in seen:
                        continue
                    seen.add(listing.external_id)
                    listings.append(listing)
        logger.info("reddit search %r subs=%s -> %d results", query, self.subreddits, len(listings))
        return listings
