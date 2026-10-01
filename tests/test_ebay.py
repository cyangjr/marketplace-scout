"""eBay Browse API local-pickup adapter. No live calls."""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from scout.config import Settings
from scout.db import Database
from scout.sources import RawListing
from scout.sources.ebay import EbayAdapter, map_item_summary
from scout.worker import ScoutWorker

TOKEN_PATH = "/identity/v1/oauth2/token"
SEARCH_PATH = "/buy/browse/v1/item_summary/search"

SAMPLE_ITEM = {
    "itemId": "v1|123456789012|0",
    "title": "Herman Miller Aeron Chair",
    "itemWebUrl": "https://www.ebay.com/itm/123456789012",
    "price": {"value": "250.00", "currency": "USD"},
    "condition": "Used",
    "seller": {"username": "chair_seller"},
    "itemLocation": {
        "city": "Brooklyn",
        "stateOrProvince": "NY",
        "postalCode": "11201",
        "country": "US",
    },
    "image": {"imageUrl": "https://i.ebayimg.com/images/g/abc/s-l1600.jpg"},
    "additionalImages": [
        {"imageUrl": "https://i.ebayimg.com/images/g/def/s-l1600.jpg"},
        {"imageUrl": "http://i.ebayimg.com/images/g/ghi/s-l1600.jpg"},
        {"imageUrl": "ftp://files.example/bad.jpg"},
        {"imageUrl": "data:image/jpeg;base64,aaaa"},
    ],
}


def _settings(**overrides: object) -> Settings:
    data: dict[str, object] = {
        "ebay_client_id": "",
        "ebay_client_secret": "",
        "ebay_max_results": 50,
        "default_max_miles": 25,
    }
    data.update(overrides)
    return Settings(**data)  # type: ignore[arg-type]


def _adapter(
    handler,
    *,
    client_id: str = "my-id",
    client_secret: str = "my-secret",
    max_results: int = 50,
) -> EbayAdapter:
    return EbayAdapter(
        _settings(
            ebay_client_id=client_id,
            ebay_client_secret=client_secret,
            ebay_max_results=max_results,
        ),
        transport=httpx.MockTransport(handler),
    )


def test_missing_credentials_returns_empty(caplog):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("unconfigured eBay search must not call the API")

    adapter = _adapter(handler, client_id="", client_secret="")
    with caplog.at_level(logging.INFO, logger="scout.sources.ebay"):
        assert asyncio.run(adapter.search("chair", "10001", None)) == []
        assert asyncio.run(adapter.search("chair", "10001", 40)) == []

    assert calls == []
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert "skipping" in infos[0].message.lower()


def test_missing_one_credential_returns_empty():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    adapter = _adapter(handler, client_id="only-id", client_secret="   ")
    assert asyncio.run(adapter.search("chair", "10001", None, max_miles=10)) == []
    assert calls == []


def test_token_and_search_request():
    token_posts: list[httpx.Request] = []
    searches: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            token_posts.append(request)
            return httpx.Response(
                200,
                json={"access_token": "app-token", "expires_in": 7200, "token_type": "Application Access Token"},
            )
        if request.url.path == SEARCH_PATH:
            searches.append(request)
            return httpx.Response(200, json={"itemSummaries": []})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    adapter = _adapter(handler)
    assert asyncio.run(adapter.search("aeron chair", "94107-1234", None, max_miles=12)) == []
    # Cached until a minute before expiry, so a second search does not mint a token.
    assert asyncio.run(adapter.search("aeron chair", "94107", None, max_miles=12)) == []

    assert len(token_posts) == 1
    token_req = token_posts[0]
    assert token_req.method == "POST"
    expected_basic = "Basic " + base64.b64encode(b"my-id:my-secret").decode()
    assert token_req.headers["authorization"] == expected_basic
    form = parse_qs(token_req.content.decode())
    assert form["grant_type"] == ["client_credentials"]
    assert form["scope"] == ["https://api.ebay.com/oauth/api_scope"]

    assert len(searches) == 2
    search_req = searches[0]
    assert search_req.method == "GET"
    assert search_req.headers["authorization"] == "Bearer app-token"
    assert search_req.headers["x-ebay-c-marketplace-id"] == "EBAY_US"
    assert search_req.url.params["q"] == "aeron chair"
    assert search_req.url.params["limit"] == "50"
    assert search_req.url.params["filter"] == (
        "buyingOptions:{FIXED_PRICE|AUCTION},"
        "pickupCountry:US,"
        "pickupPostalCode:94107,"
        "pickupRadius:12,"
        "pickupRadiusUnit:mi"
    )
    assert "price" not in search_req.url.params["filter"]
    assert "94107" in str(search_req.url)
    assert "pickupRadius" in str(search_req.url)


def test_price_ceiling_included_when_max_price_set():
    filters: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        if request.url.path == SEARCH_PATH:
            filters.append(request.url.params["filter"])
            return httpx.Response(200, json={"itemSummaries": []})
        raise AssertionError(request.url)

    adapter = _adapter(handler)
    assert asyncio.run(adapter.search("lamp", "10001", 19.99, max_miles=25)) == []
    assert filters == [
        "buyingOptions:{FIXED_PRICE|AUCTION},"
        "pickupCountry:US,"
        "pickupPostalCode:10001,"
        "pickupRadius:25,"
        "pickupRadiusUnit:mi,"
        "price:[..19.99],"
        "priceCurrency:USD"
    ]


def test_limit_capped_at_ebay_max():
    limits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        limits.append(request.url.params["limit"])
        return httpx.Response(200, json={"itemSummaries": []})

    adapter = _adapter(handler, max_results=500)
    assert asyncio.run(adapter.search("lamp", "10001", None, max_miles=10)) == []
    assert limits == ["200"]


def test_item_summary_maps_to_raw_listing():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        captured["q"] = request.url.params["q"]
        return httpx.Response(
            200,
            json={
                "itemSummaries": [
                    {"itemId": "missing-title"},
                    SAMPLE_ITEM,
                ]
            },
        )

    adapter = _adapter(handler)
    results = asyncio.run(adapter.search("aeron", "11201", 400, max_miles=15))
    assert len(results) == 1
    listing = results[0]
    assert listing.source == "ebay"
    assert listing.external_id == "v1|123456789012|0"
    assert listing.url == "https://www.ebay.com/itm/123456789012"
    assert listing.title == "Herman Miller Aeron Chair"
    assert listing.price == 250.0
    assert listing.location_text == "Brooklyn, NY"
    assert listing.lat is None
    assert listing.lng is None
    assert listing.images == [
        "https://i.ebayimg.com/images/g/abc/s-l1600.jpg",
        "https://i.ebayimg.com/images/g/def/s-l1600.jpg",
        "http://i.ebayimg.com/images/g/ghi/s-l1600.jpg",
    ]
    assert "Used" in listing.raw_text
    assert "chair_seller" in listing.raw_text
    assert "Brooklyn, NY" in listing.raw_text
    assert captured["q"] == "aeron"


def test_map_item_summary_uses_coordinates_when_present():
    listing = map_item_summary(
        {
            "itemId": "1",
            "title": "Lamp",
            "itemWebUrl": "https://www.ebay.com/itm/1",
            "price": {"value": "15"},
            "itemLocation": {
                "city": "Austin",
                "stateOrProvince": "TX",
                "latitude": "30.27",
                "longitude": "-97.74",
            },
        }
    )
    assert listing is not None
    assert listing.lat == 30.27
    assert listing.lng == -97.74
    assert listing.price == 15.0
    assert listing.location_text == "Austin, TX"


def test_http_error_returns_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        return httpx.Response(503, json={"errors": [{"message": "unavailable"}]})

    adapter = _adapter(handler)
    assert asyncio.run(adapter.search("chair", "10001", 50, max_miles=10)) == []


def test_status_ebay_configured_requires_both_credentials(tmp_path: Path):
    def worker(client_id: str, client_secret: str) -> ScoutWorker:
        db = Database(tmp_path / f"{client_id or 'empty'}-{len(client_secret)}.db")
        return ScoutWorker(
            db,
            _settings(ebay_client_id=client_id, ebay_client_secret=client_secret),
        )

    assert worker("", "").status["ebay_configured"] is False
    assert worker("id-only", "").status["ebay_configured"] is False
    assert worker("  ", "secret").status["ebay_configured"] is False
    assert worker("id", "secret").status["ebay_configured"] is True


def test_ebay_only_hunt_marks_polled_and_upserts(tmp_path: Path):
    db = Database(tmp_path / "scout.db")
    settings = _settings(
        ebay_client_id="",
        ebay_client_secret="",
        playwright_profile_dir=str(tmp_path / "pw"),
    )
    worker = ScoutWorker(db, settings)
    hunt = db.create_hunt(
        {
            "query": "aeron chair",
            "max_price": 400,
            "max_miles": 18,
            "home_zip": "10001",
            "home_lat": 40.75,
            "home_lng": -73.99,
            "sources": ["ebay"],
        }
    )

    async def craigslist_should_not_run(*args, **kwargs):
        raise AssertionError("craigslist must not run for an eBay-only hunt")

    worker.craigslist.search = craigslist_should_not_run  # type: ignore[method-assign]

    stats = asyncio.run(worker.poll_once(force=True))
    updated = db.get_hunt(hunt["id"])
    assert updated is not None
    assert updated["last_polled_at"]
    assert stats["errors"] == 0
    assert stats["listings"] == 0
    assert stats["hunts"] == 1

    seen: dict[str, object] = {}

    async def fake_search(query, home_zip, max_price, *, max_miles=None):
        seen["args"] = (query, home_zip, max_price, max_miles)
        return [
            RawListing(
                source="ebay",
                external_id="v1|99|0",
                url="https://www.ebay.com/itm/99",
                title="Aeron",
                price=200,
                location_text="New York, NY",
            )
        ]

    async def fake_eval(hunt_row, listing):
        seen["evaluated"] = (hunt_row["id"], listing["external_id"])
        return None

    worker.ebay.search = fake_search  # type: ignore[method-assign]
    worker.verifier.evaluate = fake_eval  # type: ignore[method-assign]
    # Force the hunt due again.
    db.set_meta("unused", "1")
    with db.connect() as conn:
        conn.execute("UPDATE hunts SET last_polled_at = NULL WHERE id = ?", (hunt["id"],))

    stats = asyncio.run(worker.poll_once(force=True))
    assert seen["args"] == ("aeron chair", "10001", 400, 18)
    assert seen["evaluated"] == (hunt["id"], "v1|99|0")
    assert stats["listings"] == 1
    assert stats["errors"] == 0
    again = db.get_hunt(hunt["id"])
    assert again is not None and again["last_polled_at"]
