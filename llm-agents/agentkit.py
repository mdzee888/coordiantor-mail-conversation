"""
agentkit.py - tiny framework for LLM agents in a LangGraph flow.

One agent = one `BaseAgent` subclass: set `name`, `schema` (a Pydantic model),
and `system_prompt`. This module handles the LiteLLM call, JSON extraction,
schema validation, bounded retries with error feedback, fast-fail on auth/config
errors, and token logging.

Model config: ONE JSON secret, named by LLM_PROFILE (default "GEMINI_LITE"):
    {"MODEL": "gemini/gemini-3.1-flash-lite", "TEMPERATURE": 0,
     "MAX_TOKENS": 8192, "GEMINI_API_KEY": "..."}   # or "API_KEY" / "API_BASE"
There is no GLOBAL_/DEFAULT_ chain and no built-in default model.

Per-agent overrides still win (env var / secret, prefix = NAME upper-cased):
    <NAME>_MODEL  <NAME>_PROVIDER  <NAME>_TEMPERATURE  <NAME>_MAX_TOKENS
    <NAME>_API_KEY  <NAME>_API_BASE
A bare <NAME>_MODEL (no "/") gets its provider from <NAME>_PROVIDER.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict, Type

# llm-agents/ is run with its own dir on sys.path; add the repo root so the
# shared `config` module (env var -> GCP Secret Manager) is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import litellm
from litellm import completion
from pydantic import BaseModel, ValidationError

from config import config, LLM_PROFILE, LLM_PROFILE_DEFAULT

logging.basicConfig(
    level=config.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agentkit")

litellm.drop_params = True  # ignore params a provider doesn't support

_FATAL_HINTS = (
    "api key", "api_key", "credentials", "authentication",
    "not found. install", "invalid model", "llm provider not provided",
)

# The model profile: one JSON secret shared by every agent.
_PROFILE_NAME = config.get(LLM_PROFILE, LLM_PROFILE_DEFAULT)
_PROFILE = config.get_json(_PROFILE_NAME) or {}


class AgentError(RuntimeError):
    """Raised when an agent cannot produce valid output."""


def _env(key: str) -> str:
    """Resolve a config value: env var -> GCP Secret Manager (see config.py)."""
    return (config.get(key) or "").strip()


def _profile(key: str, default=None):
    """A field from the LLM_PROFILE JSON secret (None/'' -> default)."""
    v = _PROFILE.get(key)
    return default if v is None or v == "" else v


def _only_json(text: str) -> str:
    """Strip code fences / prose around a JSON object."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
    a, b = t.find("{"), t.rfind("}")
    return t[a : b + 1] if 0 <= a < b else t


def _is_fatal(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(h in msg for h in _FATAL_HINTS)


class BaseAgent:
    name: str = ""
    schema: Type[BaseModel]
    system_prompt: str = ""
    max_retries: int = 3

    #: {"model", "input_tokens", "output_tokens"} from the most recent run()
    last_run: Dict[str, Any]

    def __init__(self) -> None:
        assert self.name and getattr(self, "schema", None), "agent needs name + schema"
        self.last_run = {}

    # -- override for a dynamically built prompt --------------------------- #
    def system(self) -> str:
        return self.system_prompt

    # -- the shared machinery ------------------------------------------- #
    def run(self, user_text: str) -> BaseModel:
        p = self.name.upper()

        model = _env(f"{p}_MODEL") or _profile("MODEL")
        if not model:
            raise AgentError(
                f"[{self.name}] no model configured: set \"MODEL\" in the "
                f"'{_PROFILE_NAME}' secret (or {p}_MODEL)"
            )
        if "/" not in model and _env(f"{p}_PROVIDER"):
            model = f"{_env(f'{p}_PROVIDER')}/{model}"

        params: Dict[str, Any] = {
            "model": model,
            "max_tokens": int(_env(f"{p}_MAX_TOKENS") or _profile("MAX_TOKENS", 8192)),
            "response_format": {"type": "json_object"},
            "num_retries": 2,  # transport-level: timeouts / 429 / 5xx
        }

        temperature = _env(f"{p}_TEMPERATURE") or _profile("TEMPERATURE")
        if temperature is not None and temperature != "":
            params["temperature"] = float(temperature)

        api_key = _env(f"{p}_API_KEY") or _profile("API_KEY") or _profile("GEMINI_API_KEY")
        if api_key:
            params["api_key"] = api_key
        api_base = _env(f"{p}_API_BASE") or _profile("API_BASE")
        if api_base:
            params["api_base"] = api_base

        messages = [
            {"role": "system", "content": self.system()},
            {"role": "user", "content": user_text},
        ]

        last = "unknown error"
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = completion(messages=messages, **params)
            except Exception as exc:  # noqa: BLE001 - LiteLLM raises many provider types
                if _is_fatal(exc):
                    raise AgentError(f"[{self.name}] {type(exc).__name__}: {exc}") from exc
                last = f"{type(exc).__name__}: {exc}"
                log.warning("[%s] attempt %d/%d failed: %s", self.name, attempt, self.max_retries, last)
                continue

            text = (resp.choices[0].message.content or "").strip()
            try:
                data = self.schema.model_validate_json(_only_json(text))
            except (ValidationError, ValueError) as exc:
                last = str(exc).splitlines()[0]
                log.warning("[%s] attempt %d/%d bad output: %s", self.name, attempt, self.max_retries, last)
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"That failed validation ({last}). Return ONLY corrected JSON."},
                ]
                continue

            usage = getattr(resp, "usage", None)
            try:
                cost = round(litellm.completion_cost(completion_response=resp), 6)
            except Exception:  # noqa: BLE001 - unknown model / missing price sheet
                cost = None
            self.last_run = {
                "model": getattr(resp, "model", params["model"]),
                "input_tokens": getattr(usage, "prompt_tokens", None),
                "output_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
                "cost_usd": cost,
            }
            log.info("[%s] ok (attempt %d) %s", self.name, attempt, self.last_run)
            return data

        raise AgentError(f"[{self.name}] failed after {self.max_retries} attempts: {last}")

    def node(self, read: str, write: str):
        """Return a LangGraph node: read `state[read]` -> write `state[write]` (+ meta)."""

        def _node(state: dict) -> dict:
            if state.get("error"):
                return {}
            raw = state.get(read)
            text = raw if isinstance(raw, str) else (json.dumps(raw, indent=2) if raw else "")
            text = text.strip()
            if not text:
                return {"error": f"[{self.name}] missing input '{read}'"}
            try:
                data = self.run(text)
            except AgentError as exc:
                return {"error": str(exc)}
            return {
                write: data.model_dump(mode="json"),
                "meta": {**(state.get("meta") or {}), self.name: self.last_run},
            }

        _node.__name__ = f"{self.name}_node"
        return _node
