from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from scout.api import router
from scout.config import get_settings
from scout.db import Database
from scout.worker import ScoutWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    db = Database(settings.db_path)
    discord_bot = None
    alert_cb = None

    status_cb = None
    if settings.discord_bot_token:
        from scout.discord_bot import ScoutDiscordBot

        discord_bot = ScoutDiscordBot(db, settings)

        async def alert_cb(hunt, listing, evaluation):
            return await discord_bot.send_match_alert(hunt, listing, evaluation)

        async def status_cb(message: str) -> None:
            await discord_bot.send_status(message)

    worker = ScoutWorker(db, settings, on_alert=alert_cb, on_status=status_cb)
    app.state.db = db
    app.state.worker = worker
    app.state.settings = settings
    app.state.discord_bot = discord_bot

    poll_task: asyncio.Task | None = None
    bot_task: asyncio.Task | None = None

    async def poll_loop() -> None:
        # Stagger first run slightly
        await asyncio.sleep(3)
        while True:
            try:
                await worker.poll_once()
            except Exception:
                logger.exception("scheduled poll failed")
            await asyncio.sleep(max(60, settings.default_poll_minutes * 60))

    poll_task = asyncio.create_task(poll_loop())

    if discord_bot and settings.discord_bot_token:
        bot_task = asyncio.create_task(discord_bot.start(settings.discord_bot_token))

    logger.info("Marketplace Scout listening — dashboard + worker started")
    try:
        yield
    finally:
        if poll_task:
            poll_task.cancel()
        if discord_bot:
            await discord_bot.close()
        if bot_task:
            bot_task.cancel()


def create_app() -> FastAPI:
    app = FastAPI(title="Marketplace Scout", lifespan=lifespan)
    app.include_router(router)

    web_dist = Path(__file__).resolve().parent.parent / "web" / "dist"
    if web_dist.exists():
        assets = web_dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(web_dist / "index.html")

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str) -> FileResponse:
            candidate = web_dist / full_path
            if full_path and candidate.exists() and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(web_dist / "index.html")
    else:

        @app.get("/")
        async def index_fallback() -> dict[str, str]:
            return {
                "message": "Marketplace Scout API is running. Build the dashboard with: cd web && npm install && npm run build"
            }

    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "scout.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
