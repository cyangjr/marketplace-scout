from __future__ import annotations

import statistics
from typing import Literal

OutlierLabel = Literal["bargain", "suspicious", "normal", None]


def price_outlier(price: float | None, recent_prices: list[float]) -> OutlierLabel:
    if price is None or len(recent_prices) < 3:
        return None
    med = statistics.median(recent_prices)
    if med <= 0:
        return None
    ratio = price / med
    if ratio <= 0.5:
        return "suspicious"
    if ratio <= 0.8:
        return "bargain"
    if ratio >= 1.5:
        return "normal"  # expensive vs peers — still normal label for UI
    return "normal"
