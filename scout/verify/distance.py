from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_geocode_cache: dict[str, tuple[float, float] | None] = {}


@dataclass
class GeoPoint:
    lat: float
    lng: float


def haversine_miles(a: GeoPoint, b: GeoPoint) -> float:
    r = 3958.8
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlng = math.radians(b.lng - a.lng)
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(h))


async def geocode_zip(zip_code: str) -> GeoPoint | None:
    z = re.sub(r"\D", "", zip_code)[:5]
    if not z:
        return None
    key = f"zip:{z}"
    if key in _geocode_cache:
        pt = _geocode_cache[key]
        return GeoPoint(*pt) if pt else None

    url = "https://nominatim.openstreetmap.org/search"
    params = {"postalcode": z, "country": "us", "format": "json", "limit": 1}
    headers = {"User-Agent": "marketplace-scout/0.1 (personal deal scout)"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        if not data:
            _geocode_cache[key] = None
            return None
        pt = (float(data[0]["lat"]), float(data[0]["lon"]))
        _geocode_cache[key] = pt
        return GeoPoint(*pt)
    except Exception:
        logger.exception("geocode_zip failed for %s", z)
        _geocode_cache[key] = None
        return None


async def geocode_text(location_text: str, near_zip: str | None = None) -> GeoPoint | None:
    if not location_text or not location_text.strip():
        return None
    # Strip common CL hood wrappers
    cleaned = location_text.strip().strip("()")
    key = f"text:{cleaned.lower()}|{near_zip or ''}"
    if key in _geocode_cache:
        pt = _geocode_cache[key]
        return GeoPoint(*pt) if pt else None

    q = cleaned
    if near_zip:
        q = f"{cleaned} {near_zip}"
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": q, "countrycodes": "us", "format": "json", "limit": 1}
    headers = {"User-Agent": "marketplace-scout/0.1 (personal deal scout)"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        if not data:
            _geocode_cache[key] = None
            return None
        pt = (float(data[0]["lat"]), float(data[0]["lon"]))
        _geocode_cache[key] = pt
        return GeoPoint(*pt)
    except Exception:
        logger.exception("geocode_text failed for %r", location_text)
        _geocode_cache[key] = None
        return None


def dump_cache() -> str:
    return json.dumps({k: v for k, v in _geocode_cache.items()})
