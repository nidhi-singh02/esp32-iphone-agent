"""Minimal client for any OpenAI-compatible chat endpoint (vision models)."""

import asyncio
import base64
import json
import os

import httpx

TOKEN_PARAMS = ("max_tokens", "max_completion_tokens")
RATE_LIMIT_WAITS = (5, 15, 30)  # seconds to back off after a 429 (Retry-After wins), then give up
NO_RETRY_QUOTAS = ("PerDay", "insufficient_quota")  # a daily or billing quota will not clear in seconds


def _refuses_temperature(error: str) -> bool:
    """True for a 400 that rejects the `temperature` parameter itself, not one that merely mentions it."""
    error = error.lower()
    return "temperature" in error and any(w in error for w in ("deprecated", "unsupported", "not support"))


class LLM:
    def __init__(self, base_url: str, model: str, api_key: str = "", extra: dict | None = None,
                 token_param: str = "auto"):
        self.model, self.extra = model, extra or {}
        self._temperature = True  # some models (Claude 5) reject `temperature`; dropped for good once refused
        # "auto": send max_tokens; switch for good if the server asks for max_completion_tokens (newer OpenAI models)
        self.token_param, self._auto = (token_param, False) if token_param in TOKEN_PARAMS else ("max_tokens", True)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=120)

    @classmethod
    def from_env(cls, role: str) -> "LLM":
        """ROLE_* variables (e.g. ACTOR_MODEL) override the shared LLM_* ones."""
        def env(key, default=""):
            return os.environ.get(f"{role}_{key}") or os.environ.get(f"LLM_{key}", default)
        return cls(env("BASE_URL", "http://localhost:8000/v1"), env("MODEL"),
                   env("API_KEY"), json.loads(env("EXTRA_BODY", "{}")), env("TOKEN_PARAM", "auto"))

    async def chat(self, system: str, text: str, image: bytes | None = None,
                   max_tokens: int = 600, temperature: float = 0) -> str:
        content = [{"type": "text", "text": text}]
        if image:
            url = "data:image/jpeg;base64," + base64.b64encode(image).decode()
            content.insert(0, {"type": "image_url", "image_url": {"url": url}})
        body = {"model": self.model, **({"temperature": temperature} if self._temperature else {}), **self.extra,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
        r = await self._post({self.token_param: max_tokens, **body})
        if r.status_code == 400 and self._auto and "max_completion_tokens" in r.text:
            self.token_param, self._auto = "max_completion_tokens", False
            r = await self._post({self.token_param: max_tokens, **body})
        if r.status_code == 400 and self._temperature and _refuses_temperature(r.text):
            self._temperature = False
            body.pop("temperature", None)
            r = await self._post({self.token_param: max_tokens, **body})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""

    async def _post(self, payload: dict) -> httpx.Response:
        """POST, waiting out rate limits (429) instead of failing the whole task."""
        for wait in (*RATE_LIMIT_WAITS, None):
            r = await self._http.post("/chat/completions", json=payload)
            if r.status_code != 429 or wait is None or any(q in r.text for q in NO_RETRY_QUOTAS):
                return r
            retry_after = r.headers.get("retry-after", "")
            await asyncio.sleep(min(float(retry_after), 60) if retry_after.replace(".", "", 1).isdigit() else wait)
