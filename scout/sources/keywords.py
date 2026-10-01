"""Recall helpers shared by national deal feeds."""

from __future__ import annotations

import re

_STOPWORDS = {"a", "the", "and", "for", "with", "under"}
_PRICE_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")


def query_tokens(query: str) -> list[str]:
    """Tokenize on non-letters, drop short tokens and tiny stopwords."""
    tokens: list[str] = []
    for raw in re.split(r"[^A-Za-z]+", query):
        token = raw.lower()
        if len(token) <= 2 or token in _STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def recall_match(query: str, text: str) -> bool:
    """Keep the item if any remaining query token appears in text.

    A one-token query must match that token. Tokens that were all dropped
    match nothing — this is a recall filter, not a pass-through.
    """
    tokens = query_tokens(query)
    if not tokens:
        return False
    blob = text.lower()
    return any(token in blob for token in tokens)


def parse_dollar_price(text: str | None) -> float | None:
    if not text:
        return None
    match = _PRICE_RE.search(text)
    if not match:
        return None
    whole = match.group(1).replace(",", "")
    frac = match.group(2) or "0"
    try:
        return float(f"{whole}.{frac}")
    except ValueError:
        return None


def price_over_max(price: float | None, max_price: float | None) -> bool:
    return price is not None and max_price is not None and price > max_price
