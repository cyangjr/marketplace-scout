from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from scout.config import Settings
from scout.db import Database
from scout.sources.craigslist import CraigslistAdapter
from scout.sources.fb import FacebookAdapter, FacebookBlockedError, FacebookSession
from scout.verify import VerifierPipeline
from scout.verify.distance import geocode_zip

logger = logging.getLogger(__name__)

AlertCallback = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], Awaitable[str | None]]
StatusNotify = Callable[[str], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class ScoutWorker:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        on_alert: AlertCallback | None = None,
        on_status: StatusNotify | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.on_alert = on_alert
        self.on_status = on_status
        self.craigslist = CraigslistAdapter()
        self.facebook = FacebookAdapter(settings)
        self.verifier = VerifierPipeline(db, settings)
        self._running = False
        self.last_error: str | None = None
        self.last_run_at: str | None = None
        self.db.set_meta("worker_status", "idle")
        self.db.set_meta("fb_session_ok", "1" if self.facebook.session_ok() else "0")

    def fb_circuit_open(self) -> bool:
        until = _parse_iso(self.db.get_meta("fb_circuit_until"))
        if not until:
            return False
        return _utc_now() < until

    def fb_circuit_remaining_hours(self) -> float | None:
        until = _parse_iso(self.db.get_meta("fb_circuit_until"))
        if not until:
            return None
        remaining = (until - _utc_now()).total_seconds() / 3600.0
        return max(0.0, remaining) if remaining > 0 else None

    async def open_fb_circuit(self, reason: str) -> None:
        until = _utc_now() + timedelta(hours=self.settings.fb_circuit_hours)
        self.db.set_meta("fb_circuit_until", until.isoformat())
        self.db.set_meta("fb_circuit_reason", reason)
        self.db.set_meta("fb_session_ok", "0")
        self.facebook.mark_session(False)
        self.last_error = f"facebook circuit open: {reason}"
        self.db.set_meta("last_error", self.last_error)
        logger.warning(
            "FB circuit open until %s (%s)", until.isoformat(), reason
        )
        if self.on_status:
            hours = self.settings.fb_circuit_hours
            try:
                await self.on_status(
                    f"Facebook paused for ~{hours:g}h ({reason}). "
                    f"Craigslist keeps running. Re-login with "
                    f"`python -m scout.sources.fb_login`, then reset the circuit "
                    f"from the dashboard or POST /api/fb/reset-circuit."
                )
            except Exception:
                logger.exception("status notify failed")

    def reset_fb_circuit(self) -> None:
        self.db.set_meta("fb_circuit_until", "")
        self.db.set_meta("fb_circuit_reason", "")
        logger.info("FB circuit reset")

    def _fb_due_for_hunt(self, hunt_id: int) -> bool:
        key = f"fb_last_poll:{hunt_id}"
        last = _parse_iso(self.db.get_meta(key))
        if not last:
            return True
        elapsed = (_utc_now() - last).total_seconds()
        return elapsed >= self.settings.fb_poll_minutes * 60

    def _mark_fb_polled(self, hunt_id: int) -> None:
        self.db.set_meta(f"fb_last_poll:{hunt_id}", _utc_now().isoformat())

    def _hunt_due(self, hunt: dict[str, Any]) -> bool:
        last = _parse_iso(hunt.get("last_polled_at"))
        if not last:
            return True
        interval = int(hunt.get("poll_interval_minutes") or self.settings.default_poll_minutes)
        return (_utc_now() - last).total_seconds() >= interval * 60

    @property
    def status(self) -> dict[str, Any]:
        circuit_open = self.fb_circuit_open()
        return {
            "running": self._running,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error or self.db.get_meta("last_error"),
            "worker_status": self.db.get_meta("worker_status", "idle"),
            "fb_session_ok": self.facebook.session_ok() and not circuit_open,
            "fb_circuit_open": circuit_open,
            "fb_circuit_until": self.db.get_meta("fb_circuit_until") or None,
            "fb_circuit_reason": self.db.get_meta("fb_circuit_reason") or None,
            "fb_circuit_remaining_hours": self.fb_circuit_remaining_hours(),
            "fb_poll_minutes": self.settings.fb_poll_minutes,
            "groq_configured": bool(self.settings.groq_api_key),
            "gemini_configured": bool(self.settings.gemini_api_key),
            "discord_configured": bool(
                self.settings.discord_bot_token and self.settings.discord_channel_id
            ),
        }

    async def poll_once(self, *, force: bool = False) -> dict[str, Any]:
        self._running = True
        self.db.set_meta("worker_status", "running")
        stats = {
            "hunts": 0,
            "listings": 0,
            "alerts": 0,
            "errors": 0,
            "fb_searches": 0,
            "fb_skipped_circuit": 0,
            "fb_skipped_interval": 0,
        }
        fb_session: FacebookSession | None = None
        try:
            hunts = [h for h in self.db.list_hunts() if h.get("active")]
            # Expire circuit automatically
            if not self.fb_circuit_open() and self.db.get_meta("fb_circuit_until"):
                # leftover stamp after expiry — clear reason noise
                if self.fb_circuit_remaining_hours() is None:
                    self.db.set_meta("fb_circuit_until", "")
                    self.db.set_meta("fb_circuit_reason", "")

            needs_fb = [
                h
                for h in hunts
                if "facebook" in (h.get("sources") or [])
                and (force or self._fb_due_for_hunt(h["id"]))
            ]
            circuit_open = self.fb_circuit_open()
            if needs_fb and circuit_open:
                stats["fb_skipped_circuit"] = len(needs_fb)
                needs_fb = []
            elif not circuit_open:
                skipped = [
                    h
                    for h in hunts
                    if "facebook" in (h.get("sources") or [])
                    and not force
                    and not self._fb_due_for_hunt(h["id"])
                ]
                stats["fb_skipped_interval"] = len(skipped)

            if needs_fb:
                try:
                    fb_session = await self.facebook.open_session().__aenter__()
                    await fb_session.smoke_check()
                except FacebookBlockedError as e:
                    await self.open_fb_circuit(str(e))
                    if fb_session:
                        await fb_session.__aexit__(None, None, None)
                        fb_session = None
                    needs_fb = []
                    stats["fb_skipped_circuit"] += 1
                except Exception as e:
                    await self.open_fb_circuit(f"session_error:{e}")
                    if fb_session:
                        await fb_session.__aexit__(None, None, None)
                        fb_session = None
                    needs_fb = []
                    stats["errors"] += 1

            fb_ids = {h["id"] for h in needs_fb}
            first_fb = True

            for hunt in hunts:
                due = force or self._hunt_due(hunt)
                want_fb = hunt["id"] in fb_ids and fb_session is not None
                want_cl = "craigslist" in (hunt.get("sources") or []) and due
                if not due and not want_fb:
                    continue
                try:
                    if want_fb and not first_fb:
                        stagger = self.settings.fb_stagger_seconds
                        delay = random.uniform(stagger * 0.7, stagger * 1.4)
                        logger.info("staggering FB search by %.1fs", delay)
                        await asyncio.sleep(delay)
                    await self._poll_hunt(
                        hunt,
                        stats,
                        fb_session=fb_session if want_fb else None,
                        run_craigslist=want_cl or (due and "craigslist" in (hunt.get("sources") or [])),
                        run_facebook=want_fb,
                    )
                    if want_fb:
                        first_fb = False
                        stats["fb_searches"] += 1
                        self._mark_fb_polled(hunt["id"])
                except FacebookBlockedError as e:
                    await self.open_fb_circuit(str(e))
                    if fb_session:
                        await fb_session.__aexit__(None, None, None)
                        fb_session = None
                    stats["errors"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    self.last_error = str(e)
                    self.db.set_meta("last_error", str(e))
                    logger.exception("poll failed for hunt %s", hunt["id"])

            self.last_run_at = _utc_now().isoformat()
            self.db.set_meta("last_run_at", self.last_run_at)
            self.db.set_meta("worker_status", "idle")
            self.db.set_meta(
                "fb_session_ok",
                "1" if self.facebook.session_ok() and not self.fb_circuit_open() else "0",
            )
        finally:
            if fb_session:
                await fb_session.__aexit__(None, None, None)
            self._running = False
        return stats

    async def _poll_hunt(
        self,
        hunt: dict[str, Any],
        stats: dict[str, Any],
        *,
        fb_session: FacebookSession | None,
        run_craigslist: bool,
        run_facebook: bool,
    ) -> None:
        stats["hunts"] += 1
        if hunt.get("home_lat") is None or hunt.get("home_lng") is None:
            pt = await geocode_zip(hunt["home_zip"])
            if pt:
                self.db.update_hunt(
                    hunt["id"], {"home_lat": pt.lat, "home_lng": pt.lng}
                )
                hunt = self.db.get_hunt(hunt["id"]) or hunt

        sources = hunt.get("sources") or ["craigslist"]
        raw_listings = []
        if run_craigslist and "craigslist" in sources:
            raw_listings.extend(
                await self.craigslist.search(
                    hunt["query"], hunt["home_zip"], hunt.get("max_price")
                )
            )
        if run_facebook and "facebook" in sources and fb_session is not None:
            raw_listings.extend(
                await fb_session.search(
                    hunt["query"], hunt["home_zip"], hunt.get("max_price")
                )
            )

        raw_listings.sort(key=lambda r: 0 if r.source == "facebook" else 1)

        for raw in raw_listings:
            listing = self.db.upsert_listing(asdict(raw))
            stats["listings"] += 1
            result = await self.verifier.evaluate(hunt, listing)
            if not result or not result.should_alert:
                continue
            ev = result.evaluation
            if self.db.has_alert(ev["id"]):
                continue
            msg_id = None
            if self.on_alert:
                try:
                    msg_id = await self.on_alert(hunt, listing, ev)
                except Exception:
                    logger.exception("alert callback failed")
            self.db.record_alert(ev["id"], msg_id)
            stats["alerts"] += 1

        if run_craigslist or run_facebook:
            self.db.set_hunt_polled(hunt["id"])
