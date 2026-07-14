from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class RawListing:
    source: str
    external_id: str
    url: str
    title: str
    price: float | None = None
    location_text: str | None = None
    lat: float | None = None
    lng: float | None = None
    images: list[str] = field(default_factory=list)
    raw_text: str = ""


class SourceAdapter(Protocol):
    name: str

    async def search(self, query: str, home_zip: str, max_price: float | None) -> list[RawListing]:
        ...
