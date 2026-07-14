"""Circuit breaker helpers for Facebook polling."""

from __future__ import annotations

import asyncio
from pathlib import Path

from scout.config import Settings
from scout.db import Database
from scout.worker import ScoutWorker


def test_fb_circuit_open_and_reset(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    settings = Settings(fb_circuit_hours=2)
    worker = ScoutWorker(db, settings)
    assert not worker.fb_circuit_open()

    asyncio.run(worker.open_fb_circuit("login_wall"))
    assert worker.fb_circuit_open()
    assert worker.fb_circuit_remaining_hours() is not None
    assert worker.status["fb_circuit_open"] is True
    assert worker.status["fb_circuit_reason"] == "login_wall"

    worker.reset_fb_circuit()
    assert not worker.fb_circuit_open()
    assert worker.status["fb_circuit_open"] is False
