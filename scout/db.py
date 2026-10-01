from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS hunts (
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
                    kind TEXT NOT NULL DEFAULT 'local',
                    last_polled_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS listings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    price REAL,
                    location_text TEXT,
                    lat REAL,
                    lng REAL,
                    images TEXT NOT NULL DEFAULT '[]',
                    raw_text TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(source, external_id)
                );

                CREATE TABLE IF NOT EXISTS evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hunt_id INTEGER NOT NULL,
                    listing_id INTEGER NOT NULL,
                    tier_used TEXT NOT NULL,
                    is_match INTEGER NOT NULL,
                    confidence REAL NOT NULL,
                    item_identity TEXT,
                    red_flags TEXT NOT NULL DEFAULT '[]',
                    reason TEXT NOT NULL DEFAULT '',
                    drive_miles REAL,
                    price_outlier TEXT,
                    decision TEXT NOT NULL,
                    dismissed INTEGER NOT NULL DEFAULT 0,
                    saved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(hunt_id, listing_id),
                    FOREIGN KEY (hunt_id) REFERENCES hunts(id) ON DELETE CASCADE,
                    FOREIGN KEY (listing_id) REFERENCES listings(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evaluation_id INTEGER NOT NULL UNIQUE,
                    discord_message_id TEXT,
                    sent_at TEXT NOT NULL,
                    FOREIGN KEY (evaluation_id) REFERENCES evaluations(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(hunts)")
            }
            if "kind" not in columns:
                conn.execute(
                    "ALTER TABLE hunts ADD COLUMN kind TEXT NOT NULL DEFAULT 'local'"
                )

    # --- meta / status ---

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    # --- hunts ---

    def create_hunt(self, data: dict[str, Any]) -> dict[str, Any]:
        now = _utc_now()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO hunts (
                    query, max_price, max_miles, home_zip, home_lat, home_lng,
                    sources, exclude_keywords, image_critical, poll_interval_minutes,
                    active, kind, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    data["query"],
                    data.get("max_price"),
                    data.get("max_miles", 25),
                    data["home_zip"],
                    data.get("home_lat"),
                    data.get("home_lng"),
                    json.dumps(data.get("sources", ["craigslist"])),
                    json.dumps(data.get("exclude_keywords", [])),
                    1 if data.get("image_critical") else 0,
                    data.get("poll_interval_minutes", 15),
                    data.get("kind") or "local",
                    now,
                    now,
                ),
            )
            hunt_id = cur.lastrowid
        return self.get_hunt(hunt_id)  # type: ignore[return-value]

    def update_hunt(self, hunt_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
        existing = self.get_hunt(hunt_id)
        if not existing:
            return None
        merged = {**existing, **data}
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE hunts SET
                    query = ?, max_price = ?, max_miles = ?, home_zip = ?,
                    home_lat = ?, home_lng = ?, sources = ?, exclude_keywords = ?,
                    image_critical = ?, poll_interval_minutes = ?, active = ?,
                    kind = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    merged["query"],
                    merged.get("max_price"),
                    merged["max_miles"],
                    merged["home_zip"],
                    merged.get("home_lat"),
                    merged.get("home_lng"),
                    json.dumps(merged["sources"])
                    if not isinstance(merged["sources"], str)
                    else merged["sources"],
                    json.dumps(merged["exclude_keywords"])
                    if not isinstance(merged["exclude_keywords"], str)
                    else merged["exclude_keywords"],
                    1 if merged.get("image_critical") else 0,
                    merged["poll_interval_minutes"],
                    1 if merged.get("active", True) else 0,
                    merged.get("kind") or "local",
                    _utc_now(),
                    hunt_id,
                ),
            )
        return self.get_hunt(hunt_id)

    def list_hunts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM hunts ORDER BY id DESC"
            ).fetchall()
        return [self._hunt_row(r) for r in rows]

    def get_hunt(self, hunt_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM hunts WHERE id = ?", (hunt_id,)
            ).fetchone()
        return self._hunt_row(row) if row else None

    def set_hunt_polled(self, hunt_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE hunts SET last_polled_at = ?, updated_at = ? WHERE id = ?",
                (_utc_now(), _utc_now(), hunt_id),
            )

    def hunt_match_count(self, hunt_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM evaluations
                WHERE hunt_id = ? AND decision = 'alert' AND dismissed = 0
                """,
                (hunt_id,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def _hunt_row(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["sources"] = json.loads(d["sources"])
        d["exclude_keywords"] = json.loads(d["exclude_keywords"])
        d["active"] = bool(d["active"])
        d["image_critical"] = bool(d["image_critical"])
        d["kind"] = d.get("kind") or "local"
        return d

    # --- listings ---

    def upsert_listing(self, data: dict[str, Any]) -> dict[str, Any]:
        now = _utc_now()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id, first_seen_at FROM listings WHERE source = ? AND external_id = ?",
                (data["source"], data["external_id"]),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE listings SET
                        url = ?, title = ?, price = ?, location_text = ?,
                        lat = ?, lng = ?, images = ?, raw_text = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        data["url"],
                        data["title"],
                        data.get("price"),
                        data.get("location_text"),
                        data.get("lat"),
                        data.get("lng"),
                        json.dumps(data.get("images", [])),
                        data.get("raw_text", ""),
                        now,
                        existing["id"],
                    ),
                )
                listing_id = existing["id"]
            else:
                cur = conn.execute(
                    """
                    INSERT INTO listings (
                        source, external_id, url, title, price, location_text,
                        lat, lng, images, raw_text, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data["source"],
                        data["external_id"],
                        data["url"],
                        data["title"],
                        data.get("price"),
                        data.get("location_text"),
                        data.get("lat"),
                        data.get("lng"),
                        json.dumps(data.get("images", [])),
                        data.get("raw_text", ""),
                        now,
                        now,
                    ),
                )
                listing_id = cur.lastrowid
        return self.get_listing(listing_id)  # type: ignore[return-value]

    def get_listing(self, listing_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["images"] = json.loads(d["images"])
        return d

    # --- evaluations ---

    def get_evaluation(self, hunt_id: int, listing_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluations WHERE hunt_id = ? AND listing_id = ?",
                (hunt_id, listing_id),
            ).fetchone()
        return self._eval_row(row) if row else None

    def save_evaluation(self, data: dict[str, Any]) -> dict[str, Any]:
        now = _utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO evaluations (
                    hunt_id, listing_id, tier_used, is_match, confidence,
                    item_identity, red_flags, reason, drive_miles, price_outlier,
                    decision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hunt_id, listing_id) DO UPDATE SET
                    tier_used = excluded.tier_used,
                    is_match = excluded.is_match,
                    confidence = excluded.confidence,
                    item_identity = excluded.item_identity,
                    red_flags = excluded.red_flags,
                    reason = excluded.reason,
                    drive_miles = excluded.drive_miles,
                    price_outlier = excluded.price_outlier,
                    decision = excluded.decision
                """,
                (
                    data["hunt_id"],
                    data["listing_id"],
                    data["tier_used"],
                    1 if data["is_match"] else 0,
                    data["confidence"],
                    data.get("item_identity"),
                    json.dumps(data.get("red_flags", [])),
                    data.get("reason", ""),
                    data.get("drive_miles"),
                    data.get("price_outlier"),
                    data["decision"],
                    now,
                ),
            )
        return self.get_evaluation(data["hunt_id"], data["listing_id"])  # type: ignore[return-value]

    def list_matches(
        self,
        *,
        include_dismissed: bool = False,
        saved_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["e.decision = 'alert'"]
        params: list[Any] = []
        if not include_dismissed:
            clauses.append("e.dismissed = 0")
        if saved_only:
            clauses.append("e.saved = 1")
        where = " AND ".join(clauses)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT e.*, l.source, l.external_id, l.url, l.title, l.price,
                       l.location_text, l.images, l.raw_text, h.query AS hunt_query
                FROM evaluations e
                JOIN listings l ON l.id = e.listing_id
                JOIN hunts h ON h.id = e.hunt_id
                WHERE {where}
                ORDER BY e.created_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        out = []
        for row in rows:
            d = self._eval_row(row)
            d["source"] = row["source"]
            d["external_id"] = row["external_id"]
            d["url"] = row["url"]
            d["title"] = row["title"]
            d["price"] = row["price"]
            d["location_text"] = row["location_text"]
            d["images"] = json.loads(row["images"])
            d["raw_text"] = row["raw_text"]
            d["hunt_query"] = row["hunt_query"]
            out.append(d)
        return out

    def set_match_flags(
        self, evaluation_id: int, *, dismissed: bool | None = None, saved: bool | None = None
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)
            ).fetchone()
            if not row:
                return None
            d = bool(row["dismissed"]) if dismissed is None else dismissed
            s = bool(row["saved"]) if saved is None else saved
            conn.execute(
                "UPDATE evaluations SET dismissed = ?, saved = ? WHERE id = ?",
                (1 if d else 0, 1 if s else 0, evaluation_id),
            )
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)
            ).fetchone()
        return self._eval_row(row) if row else None

    def recent_match_prices(self, hunt_id: int, limit: int = 30) -> list[float]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT l.price FROM evaluations e
                JOIN listings l ON l.id = e.listing_id
                WHERE e.hunt_id = ? AND e.is_match = 1 AND l.price IS NOT NULL
                ORDER BY e.created_at DESC
                LIMIT ?
                """,
                (hunt_id, limit),
            ).fetchall()
        return [float(r["price"]) for r in rows if r["price"] is not None]

    def _eval_row(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        # only parse fields that exist on bare evaluations
        if "red_flags" in d and isinstance(d["red_flags"], str):
            d["red_flags"] = json.loads(d["red_flags"])
        d["is_match"] = bool(d["is_match"])
        if "dismissed" in d:
            d["dismissed"] = bool(d["dismissed"])
        if "saved" in d:
            d["saved"] = bool(d["saved"])
        return d

    # --- alerts ---

    def has_alert(self, evaluation_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM alerts WHERE evaluation_id = ?", (evaluation_id,)
            ).fetchone()
        return row is not None

    def record_alert(self, evaluation_id: int, discord_message_id: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO alerts(evaluation_id, discord_message_id, sent_at)
                VALUES (?, ?, ?)
                """,
                (evaluation_id, discord_message_id, _utc_now()),
            )
