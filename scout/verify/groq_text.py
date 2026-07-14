from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from scout.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class TextVerdict:
    is_match: bool
    confidence: float
    item_identity: str
    red_flags: list[str]
    reason: str
    raw: str = ""


SYSTEM = """You are a marketplace listing verifier for a personal deal-scout agent.
Given a hunt description and a listing title/body, decide if the listing is a true match for what the buyer wants.
Ignore marketplace filter noise. Be strict about product identity (e.g. Aeron chair vs generic office chair; standing desk vs regular desk).
Respond with ONLY valid JSON:
{
  "is_match": boolean,
  "confidence": number between 0 and 1,
  "item_identity": string,
  "red_flags": string[],
  "reason": string
}
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


class GroqTextVerifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return bool(self.settings.groq_api_key)

    async def verify(self, hunt_query: str, title: str, body: str, max_price: float | None) -> TextVerdict:
        if not self.available:
            # Offline heuristic fallback for local dev without keys
            q_tokens = [t for t in re.split(r"\W+", hunt_query.lower()) if len(t) > 2]
            blob = f"{title} {body}".lower()
            hits = sum(1 for t in q_tokens if t in blob)
            conf = min(0.95, hits / max(len(q_tokens), 1))
            is_match = conf >= 0.5
            return TextVerdict(
                is_match=is_match,
                confidence=conf,
                item_identity=title,
                red_flags=[],
                reason="heuristic fallback (no GROQ_API_KEY)",
            )

        from groq import AsyncGroq

        client = AsyncGroq(api_key=self.settings.groq_api_key)
        user = (
            f"Hunt: {hunt_query}\n"
            f"Max price: {max_price if max_price is not None else 'n/a'}\n\n"
            f"Listing title: {title}\n"
            f"Listing body: {body[:3000]}\n"
        )
        resp = await client.chat.completions.create(
            model=self.settings.groq_model,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
            max_tokens=400,
        )
        raw = resp.choices[0].message.content or "{}"
        try:
            data = _extract_json(raw)
        except Exception:
            logger.warning("Groq returned non-JSON: %s", raw[:200])
            return TextVerdict(
                is_match=False,
                confidence=0.0,
                item_identity="",
                red_flags=["parse_error"],
                reason="failed to parse model output",
                raw=raw,
            )
        return TextVerdict(
            is_match=bool(data.get("is_match")),
            confidence=float(data.get("confidence") or 0),
            item_identity=str(data.get("item_identity") or ""),
            red_flags=list(data.get("red_flags") or []),
            reason=str(data.get("reason") or ""),
            raw=raw,
        )
