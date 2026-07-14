from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_EXCLUDES = [
    "parts only",
    "for parts",
    "looking for",
    "iso ",
    "in search of",
    "wanted",
    "wtb",
]


@dataclass
class FilterResult:
    passed: bool
    reason: str = ""


def hard_filter(
    *,
    title: str,
    raw_text: str,
    price: float | None,
    max_price: float | None,
    exclude_keywords: list[str] | None = None,
) -> FilterResult:
    text = f"{title}\n{raw_text}".lower()
    excludes = list(DEFAULT_EXCLUDES)
    if exclude_keywords:
        excludes.extend(k.lower() for k in exclude_keywords)

    for kw in excludes:
        kw = kw.strip().lower()
        if kw and kw in text:
            return FilterResult(False, f"excluded keyword: {kw}")

    if max_price is not None and price is not None and price > max_price:
        return FilterResult(False, f"price {price} > max {max_price}")

    # Buyer posts often start with these patterns even without keyword list
    if re.search(r"\b(wtb|iso)\b", text):
        return FilterResult(False, "buyer/ISO listing")

    return FilterResult(True)
