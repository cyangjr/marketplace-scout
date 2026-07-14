from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

from scout.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class VisionVerdict:
    is_match: bool
    confidence: float
    reason: str
    raw: str = ""


SYSTEM = """You verify marketplace listing photos for a deal-scout agent.
Decide if the image shows the item described in the hunt.
Respond with ONLY valid JSON:
{"is_match": boolean, "confidence": number, "reason": string}
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


class GeminiVisionVerifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return bool(self.settings.gemini_api_key)

    async def verify(
        self,
        hunt_query: str,
        title: str,
        image_url: str,
    ) -> VisionVerdict:
        if not self.available:
            return VisionVerdict(
                is_match=False,
                confidence=0.0,
                reason="no GEMINI_API_KEY; skipping vision",
            )

        # Download image bytes
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                img_resp = await client.get(image_url)
                img_resp.raise_for_status()
                image_bytes = img_resp.content
                mime = img_resp.headers.get("content-type", "image/jpeg").split(";")[0]
                if not mime.startswith("image/"):
                    mime = "image/jpeg"
        except Exception as e:
            logger.warning("failed to download image %s: %s", image_url, e)
            return VisionVerdict(
                is_match=False,
                confidence=0.0,
                reason=f"image download failed: {e}",
            )

        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.settings.gemini_api_key)
            prompt = (
                f"{SYSTEM}\n\nHunt: {hunt_query}\nListing title: {title}\n"
                "Does this photo match the hunt?"
            )
            resp = client.models.generate_content(
                model=self.settings.gemini_model,
                contents=[
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_bytes(data=image_bytes, mime_type=mime),
                            types.Part.from_text(text=prompt),
                        ],
                    )
                ],
            )
            raw = resp.text or "{}"
            data = _extract_json(raw)
            return VisionVerdict(
                is_match=bool(data.get("is_match")),
                confidence=float(data.get("confidence") or 0),
                reason=str(data.get("reason") or ""),
                raw=raw,
            )
        except Exception as e:
            logger.exception("Gemini vision failed")
            return VisionVerdict(
                is_match=False,
                confidence=0.0,
                reason=f"vision error: {e}",
            )
