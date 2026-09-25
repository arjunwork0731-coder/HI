"""Runtime configuration, read from environment variables.

The engine works in two modes:
  * LLM mode     - an OpenAI-compatible (Groq / OpenAI / Gemini / OpenRouter / Together)
                   or Anthropic endpoint powers the generator agents and the
                   secondary (LLM-judge) verification path.
  * offline mode - no API key. Generator agents fall back to deterministic,
                   extractive strategies; verification runs only on the
                   deterministic paths. Useful for demos without a key and for CI.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


# Provider presets: (base_url, default generator model, default verifier model)
PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", "gpt-4o-mini"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash", "gemini-2.0-flash"),
    "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct", "qwen/qwen-2.5-72b-instruct"),
    "together": ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo", "Qwen/Qwen2.5-72B-Instruct-Turbo"),
    "anthropic": ("https://api.anthropic.com/v1", "claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
}

_KEY_ENV = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "together": "TOGETHER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def _detect_provider() -> tuple[str | None, str | None]:
    explicit = os.getenv("LLM_PROVIDER", "").strip().lower() or None
    if explicit:
        key = os.getenv("LLM_API_KEY") or os.getenv(_KEY_ENV.get(explicit, ""), "")
        return explicit, key or None
    if os.getenv("LLM_API_KEY") and os.getenv("LLM_BASE_URL"):
        return "custom", os.getenv("LLM_API_KEY")
    for prov, env in _KEY_ENV.items():
        if os.getenv(env):
            return prov, os.getenv(env)
    return None, None


@dataclass
class Settings:
    provider: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    generator_model: str | None = None
    verifier_model: str | None = None
    llm_timeout: float = float(os.getenv("LLM_TIMEOUT", "45"))
    max_rounds: int = int(os.getenv("MAX_ROUNDS", "3"))
    enable_web: bool = _env_bool("ENABLE_WEB", True)
    sandbox_timeout: float = float(os.getenv("SANDBOX_TIMEOUT", "5"))
    accept_threshold: float = float(os.getenv("ACCEPT_THRESHOLD", "0.55"))
    extra: dict = field(default_factory=dict)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.api_key) and not _env_bool("FORCE_OFFLINE", False)

    @property
    def mode(self) -> str:
        return "llm" if self.llm_enabled else "offline"


def load_settings() -> Settings:
    prov, key = _detect_provider()
    s = Settings(provider=prov, api_key=key)
    preset = PROVIDERS.get(prov or "", (None, None, None))
    s.base_url = os.getenv("LLM_BASE_URL") or preset[0]
    s.generator_model = os.getenv("LLM_MODEL") or preset[1]
    s.verifier_model = os.getenv("VERIFIER_MODEL") or preset[2] or s.generator_model
    return s


settings = load_settings()
