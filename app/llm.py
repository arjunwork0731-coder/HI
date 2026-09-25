"""Thin, dependency-light LLM client.

Supports any OpenAI-compatible chat-completions endpoint plus Anthropic's
Messages API. Every call is recorded (role, model, latency, ok/failed) so the
audit layer can show exactly which agent used which model.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx

from .config import Settings, settings as default_settings


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> Any:
    """Parse the first JSON object/array found in an LLM response."""
    if text is None:
        raise ValueError("empty response")
    t = text.strip()
    # strip markdown fences
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    # find first balanced {...} or [...]
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(t)):
                ch = t[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(t[start : i + 1])
                        except Exception:
                            break
            start = t.find(opener, start + 1)
    raise ValueError("no JSON object found in LLM output")


class LLMClient:
    def __init__(self, cfg: Settings | None = None):
        self.cfg = cfg or default_settings
        self.calls: list[dict] = []

    @property
    def available(self) -> bool:
        return self.cfg.llm_enabled

    async def chat(
        self,
        system: str,
        user: str,
        *,
        role: str = "agent",
        model: str | None = None,
        temperature: float = 0.2,
        json_mode: bool = True,
        max_tokens: int = 1800,
    ) -> str:
        if not self.available:
            raise LLMError("LLM not configured (offline mode)")
        model = model or self.cfg.generator_model
        t0 = time.time()
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                if self.cfg.provider == "anthropic":
                    out = await self._anthropic(system, user, model, temperature, max_tokens)
                else:
                    out = await self._openai_compat(system, user, model, temperature, json_mode, max_tokens)
                self.calls.append({"role": role, "model": model, "ms": int((time.time() - t0) * 1000), "ok": True})
                return out
            except Exception as e:  # network / rate limit / 5xx
                last_err = e
                # json_mode is not supported by every model - retry without it
                if json_mode and "response_format" in str(e):
                    json_mode = False
                await asyncio.sleep(1.5 * (attempt + 1))
        self.calls.append({"role": role, "model": model, "ms": int((time.time() - t0) * 1000), "ok": False, "error": str(last_err)[:200]})
        raise LLMError(f"LLM call failed: {last_err}")

    async def chat_json(self, system: str, user: str, **kw) -> Any:
        raw = await self.chat(system + "\n\nRespond with a single valid JSON object only.", user, **kw)
        try:
            return extract_json(raw)
        except ValueError:
            # one repair attempt
            raw2 = await self.chat(
                "You convert text into valid JSON. Output JSON only.",
                f"Convert this into the intended JSON object:\n{raw[:6000]}",
                role=kw.get("role", "agent") + ":repair",
                model=kw.get("model"),
            )
            return extract_json(raw2)

    async def _openai_compat(self, system, user, model, temperature, json_mode, max_tokens) -> str:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.cfg.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.cfg.llm_timeout) as client:
            r = await client.post(f"{self.cfg.base_url.rstrip('/')}/chat/completions", json=body, headers=headers)
            if r.status_code >= 400:
                raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
        return data["choices"][0]["message"]["content"] or ""

    async def _anthropic(self, system, user, model, temperature, max_tokens) -> str:
        body = {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "x-api-key": self.cfg.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.cfg.llm_timeout) as client:
            r = await client.post(f"{self.cfg.base_url.rstrip('/')}/messages", json=body, headers=headers)
            if r.status_code >= 400:
                raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
        return "".join(b.get("text", "") for b in data.get("content", []))
