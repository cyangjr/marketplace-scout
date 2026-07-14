from __future__ import annotations

import asyncio
import logging
import random
import re
from pathlib import Path
from types import TracebackType
from urllib.parse import quote_plus

from scout.config import Settings
from scout.sources import RawListing

logger = logging.getLogger(__name__)


class FacebookBlockedError(Exception):
    """Login wall, checkpoint, or hard block — open the circuit breaker."""


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


def _looks_blocked(url: str, content: str) -> str | None:
    lower = content.lower()
    url_l = url.lower()
    if "checkpoint" in url_l or "checkpoint" in lower[:5000]:
        return "checkpoint"
    if "/login" in url_l or "login.php" in url_l:
        return "login_wall"
    if "log in" in lower and "/marketplace" not in url_l:
        return "login_wall"
    if "temporarily blocked" in lower or "confirm you're human" in lower:
        return "rate_limited"
    if "we suspended your account" in lower:
        return "suspended"
    return None


class FacebookSession:
    """One persistent Chromium context reused for multiple Marketplace searches."""

    def __init__(self, adapter: "FacebookAdapter") -> None:
        self.adapter = adapter
        self._playwright = None
        self._context = None
        self._page = None

    async def __aenter__(self) -> "FacebookSession":
        from playwright.async_api import async_playwright

        self.adapter.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.adapter.profile_dir),
            headless=self.adapter.settings.fb_headless,
            args=launch_args,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id=self.adapter.settings.fb_timezone,
        )
        self._page = (
            self._context.pages[0] if self._context.pages else await self._context.new_page()
        )
        # Light stealth: hide webdriver flag
        await self._page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._context:
                await self._context.close()
        finally:
            if self._playwright:
                await self._playwright.stop()
        self._context = None
        self._page = None
        self._playwright = None

    async def _human_pause(self, lo: float | None = None, hi: float | None = None) -> None:
        low = lo if lo is not None else self.adapter.settings.fb_wait_min_seconds
        high = hi if hi is not None else self.adapter.settings.fb_wait_max_seconds
        await asyncio.sleep(random.uniform(low, high))

    async def smoke_check(self) -> None:
        """Quick Marketplace load; raises FacebookBlockedError if session is bad."""
        assert self._page is not None
        await self._page.goto(
            "https://www.facebook.com/marketplace/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await self._human_pause(1.5, 3.5)
        content = await self._page.content()
        reason = _looks_blocked(self._page.url, content)
        if reason:
            self.adapter.mark_session(False)
            raise FacebookBlockedError(reason)
        # Soft signal: marketplace should be in URL when logged in
        if "marketplace" not in self._page.url.lower():
            self.adapter.mark_session(False)
            raise FacebookBlockedError("marketplace_unreachable")
        self.adapter.mark_session(True)

    async def search(
        self, query: str, home_zip: str, max_price: float | None
    ) -> list[RawListing]:
        assert self._page is not None
        page = self._page
        url = (
            "https://www.facebook.com/marketplace/"
            f"{quote_plus(home_zip)}/search/?query={quote_plus(query)}"
        )
        if max_price is not None:
            url += f"&maxPrice={int(max_price)}"

        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await self._human_pause()

        content = await page.content()
        blocked = _looks_blocked(page.url, content)
        if blocked:
            self.adapter.mark_session(False)
            raise FacebookBlockedError(blocked)

        # Gentle scrolls — results grid only, never open listing pages
        for _ in range(random.randint(2, 3)):
            await page.mouse.wheel(0, random.randint(1200, 2200))
            await self._human_pause(0.8, 2.0)

        items = await page.query_selector_all('a[href*="/marketplace/item/"]')
        if not items and ("log in" in content.lower() or "marketplace" not in page.url.lower()):
            self.adapter.mark_session(False)
            raise FacebookBlockedError("empty_or_login")

        results: list[RawListing] = []
        seen: set[str] = set()
        limit = self.adapter.settings.fb_max_results
        for item in items[:limit]:
            href = await item.get_attribute("href")
            if not href:
                continue
            m = re.search(r"/marketplace/item/(\d+)", href)
            if not m:
                continue
            external_id = m.group(1)
            if external_id in seen:
                continue
            seen.add(external_id)
            full_url = href if href.startswith("http") else f"https://www.facebook.com{href}"

            text = (await item.inner_text()).strip()
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            title = lines[0] if lines else f"Marketplace item {external_id}"
            price = None
            location_text = None
            for ln in lines:
                if price is None and ("$" in ln or ln.lower() == "free"):
                    price = 0.0 if ln.lower() == "free" else _parse_price(ln)
                elif location_text is None and "$" not in ln and ln != title:
                    if not re.match(r"^[\d,.]+$", ln):
                        location_text = ln

            img = await item.query_selector("img")
            images: list[str] = []
            if img:
                src = await img.get_attribute("src")
                if src:
                    images.append(src)

            results.append(
                RawListing(
                    source=self.adapter.name,
                    external_id=external_id,
                    url=full_url.split("?")[0],
                    title=title,
                    price=price,
                    location_text=location_text,
                    images=images,
                    raw_text="\n".join(lines),
                )
            )

        self.adapter.mark_session(True)
        logger.info("facebook search %r -> %d results", query, len(results))
        return results


class FacebookAdapter:
    """Playwright-based Facebook Marketplace search with session reuse."""

    name = "facebook"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.profile_dir = settings.playwright_dir

    def session_ok(self) -> bool:
        marker = self.profile_dir / ".session_ok"
        return marker.exists() and any(self.profile_dir.glob("*"))

    def mark_session(self, ok: bool) -> None:
        marker = self.profile_dir / ".session_ok"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if ok:
            marker.write_text("1", encoding="utf-8")
        elif marker.exists():
            marker.unlink()

    def open_session(self) -> FacebookSession:
        return FacebookSession(self)

    async def search(
        self, query: str, home_zip: str, max_price: float | None
    ) -> list[RawListing]:
        """One-shot search (opens and closes a session). Prefer open_session() in loops."""
        async with self.open_session() as session:
            return await session.search(query, home_zip, max_price)


def ensure_profile_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
