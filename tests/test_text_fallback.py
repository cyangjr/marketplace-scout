"""Tier-2 text cascade: Groq, Gemini text fallback, keyword heuristic. No live APIs."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from scout.config import Settings
from scout.db import Database
from scout.verify import VerifierPipeline
from scout.verify.groq_text import SYSTEM, TextCascade, _user_message


def _run(coro):
    return asyncio.run(coro)


def _settings(**overrides) -> Settings:
    data = {
        "groq_api_key": "",
        "gemini_api_key": "",
        "groq_model": "openai/gpt-oss-120b",
        "gemini_model": "gemini-3.5-flash",
    }
    data.update(overrides)
    return Settings(**data)


class _ProviderError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: object | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _install(monkeypatch, *, groq_content=None, groq_exc=None, gemini_text=None, gemini_exc=None):
    state = {"groq": 0, "gemini": 0, "groq_kwargs": None, "gemini_kwargs": None}

    class Completions:
        async def create(self, **kwargs):
            state["groq"] += 1
            state["groq_kwargs"] = kwargs
            if groq_exc is not None:
                raise groq_exc
            message = SimpleNamespace(content=groq_content)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class AsyncGroq:
        def __init__(self, api_key: str | None = None):
            self.api_key = api_key
            self.chat = SimpleNamespace(completions=Completions())

    class Models:
        def generate_content(self, **kwargs):
            state["gemini"] += 1
            state["gemini_kwargs"] = kwargs
            if gemini_exc is not None:
                raise gemini_exc
            return SimpleNamespace(text=gemini_text)

    class Client:
        def __init__(self, api_key: str | None = None):
            self.api_key = api_key
            self.models = Models()

    import groq
    from google import genai

    monkeypatch.setattr(groq, "AsyncGroq", AsyncGroq)
    monkeypatch.setattr(genai, "Client", Client)
    return state


GEMINI_VERDICT = {
    "is_match": True,
    "confidence": 0.82,
    "item_identity": "Herman Miller Aeron",
    "red_flags": ["scratches"],
    "reason": "title matches the hunt",
}


def test_default_model_ids(monkeypatch):
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.groq_model == "openai/gpt-oss-120b"
    assert settings.gemini_model == "gemini-3.5-flash"


def test_groq_success_does_not_call_gemini(monkeypatch):
    payload = {
        "is_match": True,
        "confidence": 0.91,
        "item_identity": "Aeron",
        "red_flags": ["wheels"],
        "reason": "exact chair",
    }
    state = _install(monkeypatch, groq_content=json.dumps(payload), gemini_text="unused")
    verdict = _run(
        TextCascade(_settings(groq_api_key="groq-key", gemini_api_key="gem-key")).verify(
            "herman miller aeron",
            "Herman Miller Aeron",
            "size B",
            400,
        )
    )
    assert verdict.is_match is True
    assert verdict.confidence == 0.91
    assert verdict.item_identity == "Aeron"
    assert verdict.red_flags == ["wheels"]
    assert verdict.reason == "groq: exact chair"
    assert state["groq"] == 1
    assert state["gemini"] == 0
    assert state["groq_kwargs"]["model"] == "openai/gpt-oss-120b"
    assert state["groq_kwargs"]["messages"][0]["content"] == SYSTEM
    assert "reasoning_effort" not in state["groq_kwargs"]


@pytest.mark.parametrize(
    "exc",
    [
        _ProviderError("model missing", status_code=404),
        _ProviderError("HTTP 429 rate limited", status_code=429),
        _ProviderError("The model `llama-3.3-70b-versatile` does not exist", status_code=400),
        _ProviderError("gone", status_code=400, body={"error": {"code": "model_decommissioned"}}),
    ],
)
def test_groq_missing_or_rate_limited_uses_gemini(monkeypatch, exc):
    fenced = "```json\n" + json.dumps(GEMINI_VERDICT) + "\n```"
    state = _install(monkeypatch, groq_exc=exc, gemini_text=fenced)
    settings = _settings(groq_api_key="groq-key", gemini_api_key="gem-key")
    verdict = _run(
        TextCascade(settings).verify("herman miller aeron", "Herman Miller Aeron", "size B", 350)
    )
    assert state["groq"] == 1
    assert state["gemini"] == 1
    assert verdict.is_match is True
    assert verdict.confidence == 0.82
    assert verdict.item_identity == "Herman Miller Aeron"
    assert verdict.red_flags == ["scratches"]
    assert verdict.reason == "gemini: title matches the hunt"
    kwargs = state["gemini_kwargs"]
    assert kwargs["model"] == "gemini-3.5-flash"
    assert kwargs["contents"] == _user_message("herman miller aeron", "Herman Miller Aeron", "size B", 350)
    assert isinstance(kwargs["contents"], str)
    assert kwargs["config"].system_instruction == SYSTEM


def test_both_providers_down_uses_heuristic(monkeypatch):
    state = _install(
        monkeypatch,
        groq_exc=_ProviderError("HTTP 429 rate limited", status_code=429),
        gemini_exc=RuntimeError("gemini unavailable"),
    )
    verdict = _run(
        TextCascade(_settings(groq_api_key="groq-key", gemini_api_key="gem-key")).verify(
            "herman miller aeron",
            "Herman Miller Aeron",
            "size B lumbar",
            400,
        )
    )
    assert state["groq"] == 1
    assert state["gemini"] == 1
    assert verdict.is_match is True
    assert verdict.confidence == 0.95
    assert "heuristic fallback" in verdict.reason
    assert "HTTP 429 rate limited" in verdict.reason
    assert "gemini unavailable" in verdict.reason


def test_groq_other_error_does_not_call_gemini(monkeypatch):
    state = _install(
        monkeypatch,
        groq_exc=_ProviderError("HTTP 500 upstream exploded", status_code=500),
        gemini_text=json.dumps(GEMINI_VERDICT),
    )
    verdict = _run(
        TextCascade(_settings(groq_api_key="groq-key", gemini_api_key="gem-key")).verify(
            "herman miller aeron",
            "Herman Miller Aeron",
            "",
            None,
        )
    )
    assert state["groq"] == 1
    assert state["gemini"] == 0
    assert verdict.is_match is True
    assert verdict.confidence == 0.95
    assert verdict.reason.startswith("heuristic fallback")
    assert "HTTP 500 upstream exploded" in verdict.reason


def test_no_keys_uses_heuristic(monkeypatch):
    state = _install(monkeypatch, groq_content="{}", gemini_text="{}")
    verdict = _run(
        TextCascade(_settings()).verify("herman miller aeron", "Herman Miller Aeron chair", "", None)
    )
    assert state["groq"] == 0
    assert state["gemini"] == 0
    assert verdict.reason == "heuristic fallback (no GROQ_API_KEY)"
    assert verdict.is_match is True
    assert verdict.confidence == 0.95


def test_gemini_text_when_groq_unconfigured(monkeypatch):
    state = _install(monkeypatch, gemini_text=json.dumps(GEMINI_VERDICT))
    verdict = _run(
        TextCascade(_settings(gemini_api_key="gem-key")).verify(
            "herman miller aeron",
            "generic office chair",
            "",
            100,
        )
    )
    assert state["groq"] == 0
    assert state["gemini"] == 1
    assert verdict.reason == "gemini: title matches the hunt"
    assert verdict.is_match is True
    assert isinstance(state["gemini_kwargs"]["contents"], str)


def test_pipeline_records_gemini_text_as_tier2(tmp_path, monkeypatch):
    state = _install(
        monkeypatch,
        groq_exc=_ProviderError("model missing", status_code=404),
        gemini_text=json.dumps(
            {
                "is_match": True,
                "confidence": 0.55,
                "item_identity": "Aeron",
                "red_flags": [],
                "reason": "text match",
            }
        ),
    )
    db = Database(tmp_path / "scout.db")
    pipeline = VerifierPipeline(db, _settings(groq_api_key="groq-key", gemini_api_key="gem-key"))
    hunt = db.create_hunt(
        {
            "query": "herman miller aeron",
            "max_price": 400,
            "home_zip": "10001",
            "max_miles": 25,
        }
    )
    listing = db.upsert_listing(
        {
            "source": "craigslist",
            "external_id": "abc",
            "url": "https://example.test/abc",
            "title": "Herman Miller Aeron",
            "price": 300,
            "raw_text": "size B",
            "images": [],
        }
    )
    result = _run(pipeline.evaluate(hunt, listing))
    assert result is not None
    assert result.should_alert is False
    assert result.evaluation["tier_used"] == "tier2"
    assert result.evaluation["reason"] == "gemini: text match"
    assert result.evaluation["is_match"] is True
    assert result.evaluation["confidence"] == 0.55
    assert state["groq"] == 1
    assert state["gemini"] == 1
