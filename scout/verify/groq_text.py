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


def _user_message(hunt_query: str, title: str, body: str, max_price: float | None) -> str:
    return (
        f"Hunt: {hunt_query}\n"
        f"Max price: {max_price if max_price is not None else 'n/a'}\n\n"
        f"Listing title: {title}\n"
        f"Listing body: {body[:3000]}\n"
    )


def heuristic_verdict(
    hunt_query: str,
    title: str,
    body: str,
    *,
    reason: str | None = None,
) -> TextVerdict:
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
        reason=reason or "heuristic fallback (no GROQ_API_KEY)",
    )


def _verdict_from_raw(raw: str, *, source: str) -> TextVerdict:
    try:
        data = _extract_json(raw)
    except Exception:
        logger.warning("%s returned non-JSON: %s", source, raw[:200])
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


def _status_code(exc: BaseException) -> int | None:
    candidates = [
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ]
    for status in candidates:
        if isinstance(status, int):
            return status
    return None


def _error_blob(exc: BaseException) -> str:
    parts = [str(exc)]
    message = getattr(exc, "message", None)
    if isinstance(message, str):
        parts.append(message)
    body = getattr(exc, "body", None)
    if body is not None:
        parts.append(str(body))
    response = getattr(exc, "response", None)
    text = getattr(response, "text", None)
    if isinstance(text, str):
        parts.append(text)
    return "\n".join(parts).lower()


def _groq_fallback_eligible(exc: BaseException) -> bool:
    """Model missing/decommissioned or rate-limited — safe to try Gemini text."""
    if _status_code(exc) in (404, 429):
        return True
    blob = _error_blob(exc)
    return "model_decommissioned" in blob or "does not exist" in blob


def _short_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    if not text:
        text = exc.__class__.__name__
    return text[:400]


def _tag(verdict: TextVerdict, provider: str) -> TextVerdict:
    reason = verdict.reason.strip()
    verdict.reason = f"{provider}: {reason}" if reason else provider
    return verdict


class GroqTextVerifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return bool(self.settings.groq_api_key)

    async def verify(self, hunt_query: str, title: str, body: str, max_price: float | None) -> TextVerdict:
        if not self.available:
            # Offline heuristic fallback for local dev without keys
            return heuristic_verdict(hunt_query, title, body)

        from groq import AsyncGroq

        client = AsyncGroq(api_key=self.settings.groq_api_key)
        user = _user_message(hunt_query, title, body, max_price)
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
        return _verdict_from_raw(raw, source="Groq")


async def _gemini_text_verdict(
    settings: Settings,
    hunt_query: str,
    title: str,
    body: str,
    max_price: float | None,
) -> TextVerdict:
    """Text-only Gemini verdict. Does not download or send images."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    user = _user_message(hunt_query, title, body, max_price)
    resp = client.models.generate_content(
        model=settings.gemini_model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            temperature=0.1,
            max_output_tokens=400,
        ),
    )
    raw = resp.text or "{}"
    return _verdict_from_raw(raw, source="Gemini")


class TextCascade:
    """Tier-2 text verdict: Groq, then Gemini text, then the keyword heuristic."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.groq = GroqTextVerifier(settings)

    async def verify(
        self,
        hunt_query: str,
        title: str,
        body: str,
        max_price: float | None,
    ) -> TextVerdict:
        errors: list[str] = []
        if self.groq.available:
            try:
                verdict = await self.groq.verify(hunt_query, title, body, max_price)
                return _tag(verdict, "groq")
            except Exception as exc:
                errors.append(f"groq: {_short_error(exc)}")
                can_try_gemini = _groq_fallback_eligible(exc) and bool(self.settings.gemini_api_key)
                if not can_try_gemini:
                    logger.warning("Groq text failed, using heuristic: %s", errors[-1])
                    return heuristic_verdict(
                        hunt_query,
                        title,
                        body,
                        reason=f"heuristic fallback ({'; '.join(errors)})",
                    )
                logger.warning("Groq text failed (%s); trying Gemini text", errors[-1])

        if self.settings.gemini_api_key:
            try:
                verdict = await _gemini_text_verdict(
                    self.settings, hunt_query, title, body, max_price
                )
                return _tag(verdict, "gemini")
            except Exception as exc:
                errors.append(f"gemini: {_short_error(exc)}")
                logger.warning("Gemini text failed, using heuristic: %s", errors[-1])
                return heuristic_verdict(
                    hunt_query,
                    title,
                    body,
                    reason=f"heuristic fallback ({'; '.join(errors)})",
                )

        return heuristic_verdict(hunt_query, title, body)
