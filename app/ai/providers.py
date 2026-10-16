"""Swappable AI providers behind one small adapter (tools rule 3).

AI_PROVIDER selects: mock (default, no keys, deterministic), groq, gemini.
Every adapter returns parsed JSON plus usage so the output manifest can report
model, version, tokens and cost. Tests always use the mock.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import get_settings
from app.errors import ApiError

# USD per million tokens (input, output), list prices; free tiers cost 0
PRICES = {
    "openai/gpt-oss-120b": (0.15, 0.75),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
}


@dataclass
class Usage:
    provider: str
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        pin, pout = PRICES.get(self.model, (0.0, 0.0))
        if os.getenv("AI_PRICE_PER_M_INPUT"):
            pin = float(os.environ["AI_PRICE_PER_M_INPUT"])
        if os.getenv("AI_PRICE_PER_M_OUTPUT"):
            pout = float(os.environ["AI_PRICE_PER_M_OUTPUT"])
        return round(self.input_tokens / 1e6 * pin + self.output_tokens / 1e6 * pout, 6)

    def as_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "calls": self.calls,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "seconds": round(self.seconds, 2), "cost_usd": self.cost_usd, "errors": self.errors[:5]}


class Provider:
    name = "base"
    model = ""

    def __init__(self) -> None:
        self.usage = Usage(self.name, self.model)

    async def json(self, system: str, prompt: str, *, task: str, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError


class MockProvider(Provider):
    """Deterministic stand-in: the calling module supplies the heuristic answer
    as `fallback`, so tests and simulation runs need no keys."""

    name = "mock"
    model = "heuristic-v1"

    async def json(self, system: str, prompt: str, *, task: str, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
        self.usage.calls += 1
        return dict(fallback or {})


class GroqProvider(Provider):
    name = "groq"

    def __init__(self) -> None:
        self.model = get_settings().groq_model
        super().__init__()

    async def json(self, system: str, prompt: str, *, task: str, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
        s = get_settings()
        if not s.groq_api_key:
            raise ApiError("AI_PROVIDER_FAILED", "GROQ_API_KEY is not set")
        body = {"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        data = await _post_with_retry("https://api.groq.com/openai/v1/chat/completions", body,
                                      {"Authorization": f"Bearer {s.groq_api_key}"}, self.usage)
        usage = data.get("usage", {})
        self.usage.input_tokens += int(usage.get("prompt_tokens", 0))
        self.usage.output_tokens += int(usage.get("completion_tokens", 0))
        return _parse(data["choices"][0]["message"]["content"])


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self) -> None:
        self.model = get_settings().gemini_model
        super().__init__()

    async def json(self, system: str, prompt: str, *, task: str, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
        s = get_settings()
        if not s.gemini_api_key:
            raise ApiError("AI_PROVIDER_FAILED", "GEMINI_API_KEY is not set")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
        data = await _post_with_retry(url, body, {"x-goog-api-key": s.gemini_api_key}, self.usage)
        meta = data.get("usageMetadata", {})
        self.usage.input_tokens += int(meta.get("promptTokenCount", 0))
        self.usage.output_tokens += int(meta.get("candidatesTokenCount", 0))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return _parse(text)


async def _post_with_retry(url: str, body: dict[str, Any], headers: dict[str, str], usage: Usage) -> dict[str, Any]:
    t0 = time.monotonic()
    usage.calls += 1
    last = ""
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                wait = float(resp.headers.get("retry-after", 2 ** (attempt + 1)))
                await asyncio.sleep(min(wait, 20))
                continue
            if resp.status_code >= 400:
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                break
            usage.seconds += time.monotonic() - t0
            return resp.json()
        except httpx.HTTPError as exc:
            last = type(exc).__name__
    usage.errors.append(last)
    usage.seconds += time.monotonic() - t0
    raise ApiError("AI_PROVIDER_FAILED", f"AI provider call failed: {last}")


def _parse(text: str) -> dict[str, Any]:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0) if m else text)
    except json.JSONDecodeError as exc:
        raise ApiError("AI_PROVIDER_FAILED", "AI provider returned invalid JSON") from exc


def get_provider(name: str | None = None) -> Provider:
    choice = (name or get_settings().ai_provider or "mock").lower()
    if choice == "groq":
        return GroqProvider()
    if choice == "gemini":
        return GeminiProvider()
    return MockProvider()
