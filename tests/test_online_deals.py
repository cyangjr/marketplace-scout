"""Online deal feeds and the hunt kind that skips the mile check."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scout.api.routes import router
from scout.config import Settings
from scout.db import Database
from scout.sources import RawListing
from scout.sources.keywords import query_tokens
from scout.sources.reddit import (
    RedditAdapter,
    parse_reddit_atom,
    parse_reddit_payload,
    parse_subreddits,
)
from scout.sources.slickdeals import SlickdealsAdapter, parse_slickdeals_rss
from scout.verify import VerifierPipeline
from scout.worker import ScoutWorker

FIXTURES = Path(__file__).parent / "fixtures"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        groq_api_key="",
        gemini_api_key="",
        database_path=str(tmp_path / "unused.db"),
        playwright_profile_dir=str(tmp_path / "profile"),
        reddit_subreddits="deals,buildapcsales",
    )


def test_query_tokens_drop_short_and_stopwords():
    assert query_tokens("under the chair for a tv") == ["chair"]
    assert query_tokens("Aeron") == ["aeron"]


def test_slickdeals_parser_keeps_match_drops_unrelated():
    xml_text = (FIXTURES / "slickdeals_sample.xml").read_text(encoding="utf-8")
    kept = parse_slickdeals_rss(xml_text, "Aeron chair", None)
    titles = [item.title for item in kept]
    assert any("Aeron Chair" in title for title in titles)
    assert all("toaster" not in title.lower() for title in titles)

    one_token = parse_slickdeals_rss(xml_text, "chair", None)
    assert any("Aeron Chair" in item.title for item in one_token)
    assert all("lumbar" not in item.title.lower() for item in one_token)

    chair = next(item for item in kept if "Aeron Chair" in item.title)
    assert chair.source == "slickdeals"
    assert chair.external_id == "https://slickdeals.net/f/18001-aeron"
    assert chair.url == "https://slickdeals.net/f/18001-aeron"
    assert chair.price == 189.0
    assert chair.images == [
        "https://static.slickdealscdn.com/enc.jpg",
        "https://static.slickdealscdn.com/aeron.jpg",
    ]
    assert "Refurbished size B" in chair.raw_text
    assert "<img" not in chair.raw_text

    priced = parse_slickdeals_rss(xml_text, "aeron", 50)
    priced_titles = [item.title for item in priced]
    assert not any("$189" in title for title in priced_titles)
    assert any("lumbar" in title.lower() for title in priced_titles)
    assert any("price hidden" in title.lower() for title in priced_titles)

    assert parse_slickdeals_rss("<not-xml", "aeron", None) == []


def test_reddit_parser_maps_post_and_skips_stickied_and_bad_thumbs():
    payload = json.loads((FIXTURES / "reddit_new.json").read_text(encoding="utf-8"))
    listings = parse_reddit_payload(payload, "aeron chair", None)
    by_id = {item.external_id: item for item in listings}

    assert "sticky1" not in by_id
    assert "postblender" not in by_id

    post = by_id["post1"]
    assert post.source == "reddit"
    assert post.url == "https://www.reddit.com/r/deals/comments/post1/herman_miller_aeron/"
    assert post.title.startswith("Herman Miller Aeron")
    assert post.raw_text == "Office sale, size B"
    assert post.price == 220.0
    assert post.images == ["https://b.thumbs.redditmedia.com/aeron.jpg"]

    for external_id in ("postself", "postdefault", "postnsfw"):
        assert external_id in by_id
        assert by_id[external_id].images == []


ATOM_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>t3_atom1</id>
    <title>Herman Miller Aeron $220</title>
    <link rel="alternate" href="https://www.reddit.com/r/deals/comments/atom1/aeron/"/>
    <content type="html">&lt;p&gt;Size B pickup&lt;/p&gt;&lt;img src="https://preview.redd.it/aeron.jpg"&gt;</content>
    <thumbnail url="https://b.thumbs.redditmedia.com/aeron.jpg"/>
  </entry>
  <entry>
    <id>t3_atom2</id>
    <title>Blender $40</title>
    <link rel="alternate" href="https://www.reddit.com/r/deals/comments/atom2/blender/"/>
    <content type="html">kitchen</content>
  </entry>
</feed>
"""


def test_reddit_atom_parser_maps_entries():
    listings = parse_reddit_atom(ATOM_SAMPLE, "aeron", None)
    assert len(listings) == 1
    post = listings[0]
    assert post.external_id == "atom1"
    assert post.url.endswith("/aeron/")
    assert post.price == 220.0
    assert "Size B pickup" in post.raw_text
    assert post.images[0] == "https://b.thumbs.redditmedia.com/aeron.jpg"
    assert "https://preview.redd.it/aeron.jpg" in post.images
    capped = parse_reddit_atom(ATOM_SAMPLE, "aeron", 100)
    assert capped == []


def test_slickdeals_reads_images_from_encoded_html():
    xml_text = """<?xml version="1.0"?>
    <rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
      <title>Laptop sleeve $8.49</title>
      <link>https://slickdeals.net/f/9</link>
      <description>no image here</description>
      <content:encoded><![CDATA[<img src="https://static.slickdealscdn.com/sleeve.jpg">]]></content:encoded>
      <guid>thread-9</guid>
    </item></channel></rss>
    """
    rows = parse_slickdeals_rss(xml_text, "laptop", None)
    assert len(rows) == 1
    assert rows[0].images == ["https://static.slickdealscdn.com/sleeve.jpg"]
    assert rows[0].price == 8.49


def test_reddit_subreddits_capped():
    assert parse_subreddits("deals,buildapcsales,frugal,hardwareswap,extra") == [
        "deals",
        "buildapcsales",
        "frugal",
        "hardwareswap",
    ]
    adapter = RedditAdapter(
        Settings(reddit_subreddits="deals, buildapcsales, frugal, bikes, games")
    )
    assert adapter.subreddits == ["deals", "buildapcsales", "frugal", "bikes"]


def test_feed_outage_returns_empty(monkeypatch):
    class DownClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params=None):
            del url, params
            raise httpx.ConnectError("down")

    monkeypatch.setattr("scout.sources.slickdeals.httpx.AsyncClient", DownClient)
    monkeypatch.setattr("scout.sources.reddit.httpx.AsyncClient", DownClient)
    slick = asyncio.run(SlickdealsAdapter().search("chair", "10001", None))
    reddit = asyncio.run(
        RedditAdapter(subreddits=["deals", "buildapcsales"]).search("chair", "10001", None)
    )
    assert slick == []
    assert reddit == []


def test_existing_db_gains_kind_column(tmp_path: Path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE hunts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            max_price REAL,
            max_miles REAL NOT NULL DEFAULT 25,
            home_zip TEXT NOT NULL,
            home_lat REAL,
            home_lng REAL,
            sources TEXT NOT NULL DEFAULT '["craigslist"]',
            exclude_keywords TEXT NOT NULL DEFAULT '[]',
            image_critical INTEGER NOT NULL DEFAULT 0,
            poll_interval_minutes INTEGER NOT NULL DEFAULT 15,
            active INTEGER NOT NULL DEFAULT 1,
            last_polled_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO hunts (
            query, max_price, max_miles, home_zip, sources, exclude_keywords,
            image_critical, poll_interval_minutes, active, created_at, updated_at
        ) VALUES ('chair', 100, 25, '10001', '["craigslist"]', '[]', 0, 15, 1, 't', 't')
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    legacy = db.list_hunts()[0]
    assert legacy["kind"] == "local"
    created = db.create_hunt(
        {
            "query": "ssd",
            "home_zip": "10001",
            "kind": "online",
            "sources": ["slickdeals"],
        }
    )
    assert created["kind"] == "online"
    updated = db.update_hunt(created["id"], {"kind": "local"})
    assert updated is not None
    assert updated["kind"] == "local"


def test_api_rejects_unknown_kind(tmp_path: Path):
    app = FastAPI()
    app.include_router(router)
    app.state.db = Database(tmp_path / "api.db")
    client = TestClient(app)

    bad = client.post(
        "/api/hunts",
        json={"query": "chair", "home_zip": "10001", "kind": "pickup"},
    )
    assert bad.status_code == 422

    created = client.post(
        "/api/hunts",
        json={
            "query": "chair",
            "home_zip": "10001",
            "kind": "online",
            "sources": ["reddit"],
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["kind"] == "online"

    local = client.post("/api/hunts", json={"query": "desk", "home_zip": "10001"})
    assert local.status_code == 200
    assert local.json()["kind"] == "local"

    rejected = client.patch(f"/api/hunts/{body['id']}", json={"kind": "national"})
    assert rejected.status_code == 422


def _matching_listing(db: Database, **extra) -> dict:
    data = {
        "source": "slickdeals",
        "external_id": extra.pop("external_id", "deal-1"),
        "url": "https://slickdeals.net/f/1",
        "title": "Herman Miller Aeron chair $200",
        "price": 200,
        "raw_text": "Aeron chair",
        "images": [],
        "lat": None,
        "lng": None,
        "location_text": "Los Angeles",
    }
    data.update(extra)
    return db.upsert_listing(data)


def test_online_hunt_does_not_skip_for_missing_coordinates(tmp_path: Path, monkeypatch):
    async def _no_geocode(*_args, **_kwargs):
        raise AssertionError("online hunt must not geocode")

    monkeypatch.setattr("scout.verify.geocode_zip", _no_geocode)
    monkeypatch.setattr("scout.verify.geocode_text", _no_geocode)

    db = Database(tmp_path / "online.db")
    pipeline = VerifierPipeline(db, _settings(tmp_path))
    hunt = db.create_hunt(
        {
            "query": "aeron chair",
            "max_price": 400,
            "max_miles": 5,
            "home_zip": "10001",
            "kind": "online",
            "sources": ["slickdeals"],
        }
    )
    listing = _matching_listing(db)
    result = asyncio.run(pipeline.evaluate(hunt, listing))
    assert result is not None
    assert result.should_alert
    assert result.evaluation["decision"] == "alert"
    assert result.evaluation["drive_miles"] is None
    assert "too_far" not in result.evaluation["red_flags"]


def test_online_hunt_ignores_far_coordinates(tmp_path: Path, monkeypatch):
    async def _no_geocode(*_args, **_kwargs):
        raise AssertionError("online hunt must not geocode")

    monkeypatch.setattr("scout.verify.geocode_zip", _no_geocode)
    monkeypatch.setattr("scout.verify.geocode_text", _no_geocode)

    db = Database(tmp_path / "far.db")
    pipeline = VerifierPipeline(db, _settings(tmp_path))
    hunt = db.create_hunt(
        {
            "query": "aeron chair",
            "max_price": 400,
            "max_miles": 10,
            "home_zip": "10001",
            "home_lat": 40.7484,
            "home_lng": -73.9857,
            "kind": "online",
            "sources": ["reddit"],
        }
    )
    listing = _matching_listing(
        db,
        external_id="far",
        lat=34.05,
        lng=-118.25,
        location_text=None,
    )
    result = asyncio.run(pipeline.evaluate(hunt, listing))
    assert result is not None
    assert result.should_alert
    assert result.evaluation["drive_miles"] is None


def test_local_hunt_still_rejects_far_listings(tmp_path: Path):
    db = Database(tmp_path / "local.db")
    pipeline = VerifierPipeline(db, _settings(tmp_path))
    hunt = db.create_hunt(
        {
            "query": "aeron chair",
            "max_price": 400,
            "max_miles": 25,
            "home_zip": "10001",
            "home_lat": 40.7484,
            "home_lng": -73.9857,
            "kind": "local",
            "sources": ["craigslist"],
        }
    )
    listing = _matching_listing(
        db,
        source="craigslist",
        external_id="cl-far",
        url="https://newyork.craigslist.org/1.html",
        lat=34.05,
        lng=-118.25,
        location_text=None,
    )
    result = asyncio.run(pipeline.evaluate(hunt, listing))
    assert result is not None
    assert not result.should_alert
    assert result.evaluation["decision"] == "skip"
    assert "too_far" in result.evaluation["red_flags"]
    assert result.evaluation["drive_miles"] > 25


def test_online_poll_sets_polled_without_facebook(tmp_path: Path, monkeypatch):
    async def _no_geocode(*_args, **_kwargs):
        raise AssertionError("online hunt must not geocode")

    monkeypatch.setattr("scout.worker.geocode_zip", _no_geocode)
    monkeypatch.setattr("scout.verify.geocode_zip", _no_geocode)
    monkeypatch.setattr("scout.verify.geocode_text", _no_geocode)

    db = Database(tmp_path / "worker.db")
    worker = ScoutWorker(db, _settings(tmp_path))
    hunt = db.create_hunt(
        {
            "query": "aeron chair",
            "max_price": 400,
            "home_zip": "10001",
            "kind": "online",
            "sources": ["slickdeals", "reddit", "facebook"],
        }
    )

    async def slick_search(query, home_zip, max_price):
        del query, home_zip, max_price
        return [
            RawListing(
                source="slickdeals",
                external_id="sd-1",
                url="https://slickdeals.net/f/sd-1",
                title="Herman Miller Aeron chair $180",
                price=180,
                raw_text="Aeron chair",
            )
        ]

    async def reddit_search(query, home_zip, max_price):
        del query, home_zip, max_price
        return []

    async def local_search(*_args, **_kwargs):
        raise AssertionError("unselected local source should not run")

    def open_facebook():
        raise AssertionError("online hunt should not open Facebook")

    worker.slickdeals.search = slick_search
    worker.reddit.search = reddit_search
    worker.craigslist.search = local_search
    worker.facebook.open_session = open_facebook  # type: ignore[method-assign]

    stats = asyncio.run(worker.poll_once(force=True))
    refreshed = db.get_hunt(hunt["id"])
    assert refreshed is not None
    assert refreshed["last_polled_at"]
    assert stats["fb_searches"] == 0
    assert stats["listings"] == 1
    assert stats["errors"] == 0


def test_missing_kind_stays_local(tmp_path: Path):
    db = Database(tmp_path / "missing-kind.db")
    settings = _settings(tmp_path)
    worker = ScoutWorker(db, settings)
    hunt = db.create_hunt(
        {
            "query": "aeron chair",
            "home_zip": "10001",
            "home_lat": 40.75,
            "home_lng": -73.99,
            "sources": ["craigslist"],
        }
    )
    assert hunt["kind"] == "local"
    hunt.pop("kind")
    assert worker._hunt_kind(hunt) == "local"

    called = {"craigslist": 0}

    async def cl_search(query, home_zip, max_price):
        del query, home_zip, max_price
        called["craigslist"] += 1
        return []

    async def online_search(*_args, **_kwargs):
        raise AssertionError("online source should not run")

    worker.craigslist.search = cl_search
    worker.slickdeals.search = online_search
    worker.reddit.search = online_search

    asyncio.run(
        worker._poll_hunt(
            hunt,
            {"hunts": 0, "listings": 0, "alerts": 0},
            fb_session=None,
            run_craigslist=True,
            run_facebook=False,
            run_slickdeals=False,
            run_reddit=False,
        )
    )
    assert called["craigslist"] == 1
    assert db.get_hunt(hunt["id"])["last_polled_at"]
