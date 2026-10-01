from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from scout.config import Settings
from scout.db import Database
from scout.verify.distance import GeoPoint, geocode_text, geocode_zip, haversine_miles
from scout.verify.filters import hard_filter
from scout.verify.gemini_vision import GeminiVisionVerifier
from scout.verify.groq_text import TextCascade
from scout.verify.pricing import price_outlier

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    evaluation: dict[str, Any]
    should_alert: bool


class VerifierPipeline:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.text = TextCascade(settings)
        self.gemini = GeminiVisionVerifier(settings)

    async def evaluate(self, hunt: dict[str, Any], listing: dict[str, Any]) -> PipelineResult | None:
        existing = self.db.get_evaluation(hunt["id"], listing["id"])
        if existing:
            return PipelineResult(existing, should_alert=False)

        # Tier 1
        fr = hard_filter(
            title=listing["title"],
            raw_text=listing.get("raw_text") or "",
            price=listing.get("price"),
            max_price=hunt.get("max_price"),
            exclude_keywords=hunt.get("exclude_keywords") or [],
        )
        if not fr.passed:
            ev = self.db.save_evaluation(
                {
                    "hunt_id": hunt["id"],
                    "listing_id": listing["id"],
                    "tier_used": "tier1",
                    "is_match": False,
                    "confidence": 0.0,
                    "item_identity": "",
                    "red_flags": [fr.reason],
                    "reason": fr.reason,
                    "drive_miles": None,
                    "price_outlier": None,
                    "decision": "skip",
                }
            )
            return PipelineResult(ev, should_alert=False)

        # Tier 2: Groq text, Gemini text if Groq's model is gone or rate-limited, else heuristic
        text = await self.text.verify(
            hunt_query=hunt["query"],
            title=listing["title"],
            body=listing.get("raw_text") or "",
            max_price=hunt.get("max_price"),
        )

        is_match = text.is_match
        confidence = text.confidence
        reason = text.reason
        tier_used = "tier2"
        red_flags = list(text.red_flags)
        item_identity = text.item_identity

        low = self.settings.confidence_ambiguous_low
        high = self.settings.confidence_ambiguous_high
        ambiguous = low <= confidence <= high
        needs_vision = hunt.get("image_critical") or (ambiguous and text.is_match is not False)

        # Also run vision if match is claimed but confidence mid, or image_critical always on survivors
        if needs_vision and listing.get("images"):
            vision = await self.gemini.verify(
                hunt_query=hunt["query"],
                title=listing["title"],
                image_url=listing["images"][0],
            )
            if self.gemini.available and vision.reason != "no GEMINI_API_KEY; skipping vision":
                tier_used = "tier3"
                # Blend: vision can veto or confirm
                if vision.is_match:
                    is_match = True
                    confidence = max(confidence, vision.confidence)
                    reason = f"{reason} | vision: {vision.reason}"
                else:
                    # if vision says no with decent confidence, veto
                    if vision.confidence >= 0.6:
                        is_match = False
                        confidence = vision.confidence
                        reason = f"vision veto: {vision.reason}"
                        red_flags.append("vision_mismatch")
                    else:
                        reason = f"{reason} | vision uncertain: {vision.reason}"

        if not is_match or confidence < self.settings.confidence_alert_threshold:
            ev = self.db.save_evaluation(
                {
                    "hunt_id": hunt["id"],
                    "listing_id": listing["id"],
                    "tier_used": tier_used,
                    "is_match": is_match,
                    "confidence": confidence,
                    "item_identity": item_identity,
                    "red_flags": red_flags,
                    "reason": reason,
                    "drive_miles": None,
                    "price_outlier": None,
                    "decision": "skip",
                }
            )
            return PipelineResult(ev, should_alert=False)

        # Distance
        home = None
        if hunt.get("home_lat") is not None and hunt.get("home_lng") is not None:
            home = GeoPoint(float(hunt["home_lat"]), float(hunt["home_lng"]))
        else:
            home = await geocode_zip(hunt["home_zip"])

        drive_miles = None
        if home:
            listing_pt = None
            if listing.get("lat") is not None and listing.get("lng") is not None:
                listing_pt = GeoPoint(float(listing["lat"]), float(listing["lng"]))
            elif listing.get("location_text"):
                listing_pt = await geocode_text(
                    listing["location_text"], near_zip=hunt["home_zip"]
                )
            if listing_pt:
                drive_miles = haversine_miles(home, listing_pt)
                if drive_miles > float(hunt["max_miles"]):
                    ev = self.db.save_evaluation(
                        {
                            "hunt_id": hunt["id"],
                            "listing_id": listing["id"],
                            "tier_used": tier_used,
                            "is_match": True,
                            "confidence": confidence,
                            "item_identity": item_identity,
                            "red_flags": red_flags + ["too_far"],
                            "reason": f"{reason} | {drive_miles:.1f} mi > max {hunt['max_miles']}",
                            "drive_miles": drive_miles,
                            "price_outlier": None,
                            "decision": "skip",
                        }
                    )
                    return PipelineResult(ev, should_alert=False)

        outlier = price_outlier(
            listing.get("price"),
            self.db.recent_match_prices(hunt["id"]),
        )

        ev = self.db.save_evaluation(
            {
                "hunt_id": hunt["id"],
                "listing_id": listing["id"],
                "tier_used": tier_used,
                "is_match": True,
                "confidence": confidence,
                "item_identity": item_identity,
                "red_flags": red_flags,
                "reason": reason,
                "drive_miles": drive_miles,
                "price_outlier": outlier,
                "decision": "alert",
            }
        )
        return PipelineResult(ev, should_alert=True)
