import asyncio
import json

import httpx

from hidagent.llm import LLM


def fake_server(llm: LLM, wants: str) -> list[dict]:
    """Point the client at an in-memory endpoint that only accepts the `wants` token parameter."""
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if wants not in body:
            return httpx.Response(400, json={"error": {"message": f"Unsupported parameter. Use '{wants}' instead."}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "DO: wait"}}]})

    llm._http = httpx.AsyncClient(base_url="http://test/v1", transport=httpx.MockTransport(handle))
    return seen


def test_classic_servers_get_max_tokens_once():
    llm = LLM("http://test/v1", "qwen")
    seen = fake_server(llm, "max_tokens")
    assert asyncio.run(llm.chat("sys", "hi")) == "DO: wait"
    assert len(seen) == 1 and seen[0]["max_tokens"] == 600


def test_auto_switches_to_max_completion_tokens_and_remembers():
    llm = LLM("http://test/v1", "gpt")
    seen = fake_server(llm, "max_completion_tokens")
    asyncio.run(llm.chat("sys", "hi"))
    asyncio.run(llm.chat("sys", "again"))
    assert [("max_tokens" in b, "max_completion_tokens" in b) for b in seen] == [(True, False), (False, True),
                                                                                 (False, True)]


def test_explicit_token_param_is_used_directly():
    llm = LLM("http://test/v1", "gpt", token_param="max_completion_tokens")
    seen = fake_server(llm, "max_completion_tokens")
    asyncio.run(llm.chat("sys", "hi"))
    assert len(seen) == 1


def test_gemini_openai_endpoint_gets_max_tokens_and_a_jpeg():
    """Gemini's OpenAI-compatible endpoint accepts max_tokens. auto already sends that first."""
    llm = LLM("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.8-flash",
              api_key="test-key", token_param="max_tokens")
    assert llm._http.headers["Authorization"] == "Bearer test-key"
    seen = fake_server(llm, "max_tokens")
    assert asyncio.run(llm.chat("sys", "what is on screen?", image=b"\xff\xd8\xff\xd9")) == "DO: wait"
    body = seen[0]
    assert "max_completion_tokens" not in body and body["max_tokens"] == 600
    image = body["messages"][1]["content"][0]
    assert image["type"] == "image_url" and image["image_url"]["url"].startswith("data:image/jpeg;base64,")

    auto = LLM("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.8-flash")
    seen = fake_server(auto, "max_tokens")
    asyncio.run(auto.chat("sys", "hi"))
    assert len(seen) == 1 and "max_completion_tokens" not in seen[0]


def test_from_env_can_pin_max_tokens_for_gemini(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    monkeypatch.setenv("LLM_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_TOKEN_PARAM", "max_tokens")
    llm = LLM.from_env("ACTOR")
    assert llm.model == "gemini-3.8-flash"
    assert llm.token_param == "max_tokens" and llm._auto is False
    assert str(llm._http.base_url).rstrip("/") == "https://generativelanguage.googleapis.com/v1beta/openai"


def test_extra_body_and_image_are_sent():
    llm = LLM("http://test/v1", "gpt", extra={"reasoning_effort": "none"})
    seen = fake_server(llm, "max_tokens")
    asyncio.run(llm.chat("sys", "hi", image=b"\xff\xd8jpeg"))
    body = seen[0]
    assert body["reasoning_effort"] == "none"
    assert body["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_rate_limit_is_waited_out(monkeypatch):
    monkeypatch.setattr("hidagent.llm.RATE_LIMIT_WAITS", (0, 0))
    llm = LLM("http://test/v1", "gemini")
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "DO: wait"}}]})

    llm._http = httpx.AsyncClient(base_url="http://test/v1", transport=httpx.MockTransport(handle))
    assert asyncio.run(llm.chat("sys", "hi")) == "DO: wait" and len(calls) == 3


def test_daily_quota_fails_fast():
    llm = LLM("http://test/v1", "gemini")
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, text="Quota exceeded: GenerateRequestsPerDayPerProjectPerModel-FreeTier")

    llm._http = httpx.AsyncClient(base_url="http://test/v1", transport=httpx.MockTransport(handle))
    try:
        asyncio.run(llm.chat("sys", "hi"))
    except httpx.HTTPStatusError:
        pass
    assert len(calls) == 1


def test_temperature_is_dropped_when_the_model_refuses_it():
    llm = LLM("http://test/v1", "claude")
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "`temperature` is deprecated for this model."}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "DO: wait"}}]})

    llm._http = httpx.AsyncClient(base_url="http://test/v1", transport=httpx.MockTransport(handle))
    assert asyncio.run(llm.chat("sys", "hi")) == "DO: wait"
    asyncio.run(llm.chat("sys", "again"))
    assert ["temperature" in b for b in seen] == [True, False, False]
