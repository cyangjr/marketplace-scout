"""Craigslist public HTML search: fixtures only, no live network."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs, urlparse

import pytest

from scout.config import Settings
from scout.sources import RawListing
from scout.sources import craigslist
from scout.sources.craigslist import (
    SEARCH_PAGE_SIZE,
    CraigslistAdapter,
    build_search_url,
    parse_posting_page,
    parse_search_results,
)
from scout.worker import enrich_craigslist_listings, needs_craigslist_detail

RESULTS_HTML = """
<html><body>
<ol class="cl-static-search-results">
  <li class="cl-static-search-result">
    <a href="https://newyork.craigslist.org/mnh/fuo/d/new-york-herman-miller-aeron/7788990011.html">
      <div class="title">Herman Miller Aeron</div>
      <div class="price">$350</div>
      <div class="location">(Midtown)</div>
    </a>
  </li>
</ol>
</body></html>
"""

POSTING_HTML = """
<html><body>
<h1 class="postingtitle">
  <span class="price">$275</span>
  <span class="postingtitletext">
    <span id="titletextonly">Herman Miller Aeron Size B</span>
  </span>
</h1>
<section id="postingbody">
  <div class="print-information print-qrcode-container">
    <p class="print-qrcode-label">QR Code Link to This Post</p>
  </div>
  QR Code Link to This Post
  Excellent condition. Size B, fully loaded.
  Pickup in Manhattan.
</section>
<div id="map" class="viewposting" data-latitude="40.7484" data-longitude="-73.9857"></div>
<div id="thumbs">
  <a class="thumb" href="https://images.craigslist.org/aeron_full.jpg">
    <img src="https://images.craigslist.org/aeron_thumb.jpg" alt="">
  </a>
</div>
</body></html>
"""


def _results_page(*items: tuple[str, str]) -> str:
    cards = []
    for post_id, title in items:
        cards.append(
            f"""
            <li class="cl-static-search-result">
              <a href="https://newyork.craigslist.org/mnh/fuo/d/item/{post_id}.html">
                <div class="title">{title}</div>
                <div class="price">$100</div>
              </a>
            </li>
            """
        )
    return (
        "<html><body><ol class=\"cl-static-search-results\">"
        + "".join(cards)
        + "</ol></body></html>"
    )


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _Client:
    def __init__(self, pages: dict[int, str], calls: list[str], headers: list[dict]) -> None:
        self.pages = pages
        self.calls = calls
        self.headers = headers

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def get(self, url: str, headers: dict | None = None) -> _Response:
        self.calls.append(url)
        self.headers.append(dict(headers or {}))
        offset = int(parse_qs(urlparse(url).query).get("s", ["0"])[0])
        return _Response(self.pages.get(offset, "<html></html>"))


def _install_client(monkeypatch: pytest.MonkeyPatch, pages: dict[int, str]) -> _Client:
    client = _Client(pages, [], [])

    def factory(*_args: object, **_kwargs: object) -> _Client:
        return client

    monkeypatch.setattr(craigslist.httpx, "AsyncClient", factory)
    return client


def _card(**overrides: object) -> RawListing:
    data: dict[str, object] = {
        "source": "craigslist",
        "external_id": "1",
        "url": "https://newyork.craigslist.org/mnh/fuo/d/item/1.html",
        "title": "Herman Miller Aeron",
        "price": 350.0,
        "raw_text": "Herman Miller Aeron",
    }
    data.update(overrides)
    return RawListing(**data)  # type: ignore[arg-type]


def test_parse_search_results_extracts_title_price_url_id():
    rows = parse_search_results(RESULTS_HTML, "newyork")
    assert len(rows) == 1
    row = rows[0]
    assert row.title == "Herman Miller Aeron"
    assert row.price == 350
    assert row.url == (
        "https://newyork.craigslist.org/mnh/fuo/d/new-york-herman-miller-aeron/7788990011.html"
    )
    assert row.external_id == "7788990011"
    assert row.raw_text == row.title
    assert row.location_text == "Midtown"


def test_build_search_url_includes_postal_distance_and_price():
    url = build_search_url("newyork", "herman miller", "10001", 400, 25, 0)
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "newyork.craigslist.org"
    assert parsed.path == "/search/sss"
    query = parse_qs(parsed.query)
    assert query["query"] == ["herman miller"]
    assert query["sort"] == ["date"]
    assert query["postal"] == ["10001"]
    assert query["search_distance"] == ["25"]
    assert query["max_price"] == ["400"]
    assert query["s"] == ["0"]
    assert "sapi.craigslist.org" not in url

    open_price = parse_qs(
        urlparse(build_search_url("sfbay", "desk", "94107", None, 10.5, SEARCH_PAGE_SIZE)).query
    )
    assert "max_price" not in open_price
    assert open_price["postal"] == ["94107"]
    assert open_price["search_distance"] == ["10.5"]
    assert open_price["s"] == [str(SEARCH_PAGE_SIZE)]


def test_search_request_includes_postal_and_distance(monkeypatch: pytest.MonkeyPatch):
    client = _install_client(monkeypatch, {0: RESULTS_HTML})
    rows = asyncio.run(
        CraigslistAdapter().search(
            "aeron chair", "10001", 400, max_miles=25, max_pages=1
        )
    )
    assert len(client.calls) == 1
    parsed = urlparse(client.calls[0])
    assert parsed.netloc == "newyork.craigslist.org"
    assert parsed.path == "/search/sss"
    query = parse_qs(parsed.query)
    assert query["postal"] == ["10001"]
    assert query["search_distance"] == ["25"]
    assert query["query"] == ["aeron chair"]
    assert query["sort"] == ["date"]
    assert query["max_price"] == ["400"]
    assert query["s"] == ["0"]
    assert "Chrome/120.0.0.0" in client.headers[0]["User-Agent"]
    assert rows[0].external_id == "7788990011"


def test_search_paginates_stops_and_dedupes(monkeypatch: pytest.MonkeyPatch):
    pages = {
        0: _results_page(("111", "First"), ("222", "Second")),
        SEARCH_PAGE_SIZE: _results_page(("222", "Second again"), ("333", "Third")),
        SEARCH_PAGE_SIZE * 2: _results_page(("444", "Fourth")),
    }
    client = _install_client(monkeypatch, pages)
    rows = asyncio.run(
        CraigslistAdapter().search(
            "chair",
            "10001",
            None,
            max_miles=10,
            max_pages=2,
            max_results=120,
        )
    )
    assert [row.external_id for row in rows] == ["111", "222", "333"]
    offsets = [parse_qs(urlparse(url).query)["s"][0] for url in client.calls]
    assert offsets == ["0", str(SEARCH_PAGE_SIZE)]

    client = _install_client(monkeypatch, pages)
    capped = asyncio.run(
        CraigslistAdapter().search(
            "chair",
            "10001",
            None,
            max_miles=10,
            max_pages=3,
            max_results=2,
        )
    )
    assert [row.external_id for row in capped] == ["111", "222"]
    assert len(client.calls) == 1

    duplicate_tail = {
        0: _results_page(("111", "First")),
        SEARCH_PAGE_SIZE: _results_page(("111", "First again")),
        SEARCH_PAGE_SIZE * 2: _results_page(("999", "Should not fetch")),
    }
    client = _install_client(monkeypatch, duplicate_tail)
    stopped = asyncio.run(
        CraigslistAdapter().search(
            "chair",
            "10001",
            None,
            max_miles=10,
            max_pages=3,
            max_results=50,
        )
    )
    assert [row.external_id for row in stopped] == ["111"]
    assert len(client.calls) == 2


def test_parse_posting_page_extracts_body_price_and_coordinates():
    card = RawListing(
        source="craigslist",
        external_id="7788990011",
        url="https://newyork.craigslist.org/mnh/fuo/d/new-york-herman-miller-aeron/7788990011.html",
        title="Herman Miller Aeron",
        price=1.0,
        raw_text="Herman Miller Aeron",
        images=["https://images.craigslist.org/card.jpg"],
    )
    listing = parse_posting_page(POSTING_HTML, card)
    assert listing.title == "Herman Miller Aeron Size B"
    assert listing.price == 275
    assert listing.raw_text == (
        "Excellent condition. Size B, fully loaded.\nPickup in Manhattan."
    )
    assert "QR Code Link to This Post" not in listing.raw_text
    assert listing.lat == 40.7484
    assert listing.lng == -73.9857
    assert listing.images == ["https://images.craigslist.org/aeron_full.jpg"]
    assert listing.external_id == card.external_id
    assert listing.url == card.url


def test_needs_detail_skips_when_hard_filter_fails():
    assert not needs_craigslist_detail(
        _card(title="Aeron parts only", raw_text="Aeron parts only"),
        max_price=400,
    )
    assert not needs_craigslist_detail(
        _card(price=500),
        max_price=400,
    )
    assert not needs_craigslist_detail(
        _card(title="WTB Aeron", raw_text="WTB Aeron"),
        max_price=400,
    )
    assert not needs_craigslist_detail(
        _card(),
        max_price=400,
        exclude_keywords=["aeron"],
    )
    assert needs_craigslist_detail(_card(), max_price=400)
    assert needs_craigslist_detail(_card(raw_text=""), max_price=400)
    assert not needs_craigslist_detail(
        _card(raw_text="Size B, includes lumbar."),
        max_price=400,
    )
    assert not needs_craigslist_detail(
        _card(source="facebook", raw_text="Herman Miller Aeron"),
        max_price=400,
    )


def test_enrich_skips_filtered_rows_and_keeps_card_on_failure(caplog: pytest.LogCaptureFixture):
    rejected = _card(
        external_id="bad",
        title="chair parts only",
        raw_text="chair parts only",
        url="https://newyork.craigslist.org/bad.html",
    )
    first = _card(external_id="1", url="https://newyork.craigslist.org/1.html")
    second = _card(external_id="2", url="https://newyork.craigslist.org/2.html")
    third = _card(external_id="3", url="https://newyork.craigslist.org/3.html")
    fetched: list[str] = []
    sleeps: list[float] = []

    async def fetch_detail(listing: RawListing) -> RawListing:
        fetched.append(listing.external_id)
        if listing.external_id == "2":
            raise RuntimeError("posting removed")
        return RawListing(
            source=listing.source,
            external_id=listing.external_id,
            url=listing.url,
            title=listing.title,
            price=listing.price,
            raw_text="Loaded size B with lumbar.",
        )

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with caplog.at_level(logging.WARNING):
        out = asyncio.run(
            enrich_craigslist_listings(
                [rejected, first, second, third],
                max_price=400,
                exclude_keywords=[],
                fetch_detail=fetch_detail,
                detail_limit=2,
                delay_seconds=0.4,
                sleep=fake_sleep,
            )
        )

    assert fetched == ["1", "2"]
    assert sleeps == [0.4]
    assert out[0].raw_text == "chair parts only"
    assert out[1].raw_text == "Loaded size B with lumbar."
    assert out[2].url == second.url
    assert out[2].raw_text == second.raw_text
    assert out[3].external_id == "3"
    assert out[3].raw_text == third.raw_text
    assert "craigslist detail fetch failed" in caplog.text
    assert second.url in caplog.text


def test_craigslist_settings_env_names(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CL_MAX_PAGES", "4")
    monkeypatch.setenv("CL_MAX_RESULTS", "80")
    monkeypatch.setenv("CL_DETAIL_LIMIT", "7")
    monkeypatch.setenv("CL_DETAIL_DELAY_SECONDS", "0.2")
    settings = Settings(_env_file=None)
    assert settings.cl_max_pages == 4
    assert settings.cl_max_results == 80
    assert settings.cl_detail_limit == 7
    assert settings.cl_detail_delay_seconds == 0.2
