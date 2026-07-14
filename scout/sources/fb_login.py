"""Interactive Facebook login to persist Playwright session cookies."""

from __future__ import annotations

import asyncio
from pathlib import Path

from scout.config import get_settings


async def main() -> None:
    from playwright.async_api import async_playwright

    settings = get_settings()
    profile = Path(settings.playwright_profile_dir)
    profile.mkdir(parents=True, exist_ok=True)

    print(f"Opening Chromium with profile at {profile}")
    print("Log into Facebook, open Marketplace once, then close the browser window.")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.facebook.com/marketplace/", wait_until="domcontentloaded")
        # Wait until user closes the browser
        try:
            while context.browser and context.browser.is_connected():
                await asyncio.sleep(1)
                if not context.pages:
                    break
        except Exception:
            pass
        marker = profile / ".session_ok"
        marker.write_text("1", encoding="utf-8")
        print("Session saved.")


if __name__ == "__main__":
    asyncio.run(main())
