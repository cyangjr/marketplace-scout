from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from scout.config import get_settings

router = APIRouter()


class HuntCreate(BaseModel):
    query: str
    max_price: float | None = None
    max_miles: float | None = None
    home_zip: str | None = None
    sources: list[str] = Field(default_factory=lambda: ["craigslist"])
    exclude_keywords: list[str] = Field(default_factory=list)
    image_critical: bool = False
    poll_interval_minutes: int | None = None
    active: bool = True


class HuntUpdate(BaseModel):
    query: str | None = None
    max_price: float | None = None
    max_miles: float | None = None
    home_zip: str | None = None
    sources: list[str] | None = None
    exclude_keywords: list[str] | None = None
    image_critical: bool | None = None
    poll_interval_minutes: int | None = None
    active: bool | None = None


class MatchFlags(BaseModel):
    dismissed: bool | None = None
    saved: bool | None = None


@router.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    db = request.app.state.db
    worker = request.app.state.worker
    settings = get_settings()
    return {
        "ok": True,
        "status": worker.status,
        "hunt_count": len(db.list_hunts()),
        "home_zip_default": settings.home_zip,
    }


@router.get("/api/status")
async def status(request: Request) -> dict[str, Any]:
    return request.app.state.worker.status


@router.get("/api/hunts")
async def list_hunts(request: Request) -> list[dict[str, Any]]:
    db = request.app.state.db
    out = []
    for h in db.list_hunts():
        row = dict(h)
        row["match_count"] = db.hunt_match_count(h["id"])
        out.append(row)
    return out


@router.post("/api/hunts")
async def create_hunt(body: HuntCreate, request: Request) -> dict[str, Any]:
    db = request.app.state.db
    settings = get_settings()
    data = body.model_dump()
    data["home_zip"] = data["home_zip"] or settings.home_zip
    data["max_miles"] = (
        data["max_miles"]
        if data["max_miles"] is not None
        else settings.default_max_miles
    )
    data["poll_interval_minutes"] = (
        data["poll_interval_minutes"]
        if data["poll_interval_minutes"] is not None
        else settings.default_poll_minutes
    )
    hunt = db.create_hunt(data)
    hunt["match_count"] = 0
    return hunt


@router.patch("/api/hunts/{hunt_id}")
async def update_hunt(hunt_id: int, body: HuntUpdate, request: Request) -> dict[str, Any]:
    db = request.app.state.db
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    updated = db.update_hunt(hunt_id, payload)
    if not updated:
        raise HTTPException(404, "hunt not found")
    updated["match_count"] = db.hunt_match_count(hunt_id)
    return updated


@router.post("/api/hunts/{hunt_id}/pause")
async def pause_hunt(hunt_id: int, request: Request) -> dict[str, Any]:
    db = request.app.state.db
    updated = db.update_hunt(hunt_id, {"active": False})
    if not updated:
        raise HTTPException(404, "hunt not found")
    updated["match_count"] = db.hunt_match_count(hunt_id)
    return updated


@router.post("/api/hunts/{hunt_id}/resume")
async def resume_hunt(hunt_id: int, request: Request) -> dict[str, Any]:
    db = request.app.state.db
    updated = db.update_hunt(hunt_id, {"active": True})
    if not updated:
        raise HTTPException(404, "hunt not found")
    updated["match_count"] = db.hunt_match_count(hunt_id)
    return updated


@router.get("/api/matches")
async def list_matches(
    request: Request,
    saved: bool = False,
    include_dismissed: bool = False,
) -> list[dict[str, Any]]:
    return request.app.state.db.list_matches(
        saved_only=saved, include_dismissed=include_dismissed
    )


@router.post("/api/matches/{evaluation_id}/flags")
async def match_flags(
    evaluation_id: int, body: MatchFlags, request: Request
) -> dict[str, Any]:
    updated = request.app.state.db.set_match_flags(
        evaluation_id, dismissed=body.dismissed, saved=body.saved
    )
    if not updated:
        raise HTTPException(404, "match not found")
    return updated


@router.post("/api/poll")
async def trigger_poll(request: Request, force: bool = False) -> dict[str, Any]:
    worker = request.app.state.worker
    stats = await worker.poll_once(force=force)
    return {"ok": True, "stats": stats, "status": worker.status}


@router.post("/api/fb/reset-circuit")
async def reset_fb_circuit(request: Request) -> dict[str, Any]:
    worker = request.app.state.worker
    worker.reset_fb_circuit()
    return {"ok": True, "status": worker.status}
