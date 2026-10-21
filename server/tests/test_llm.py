import asyncio
import json

import httpx
import pytest

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


def test_extra_body_and_image_are_sent():
    llm = LLM("http://test/v1", "gpt", extra={"reasoning_effort": "none"})
    seen = fake_server(llm, "max_tokens")
    asyncio.run(llm.chat("sys", "hi", image=b"\xff\xd8jpeg"))
    body = seen[0]
    assert body["reasoning_effort"] == "none"
    assert body["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


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
    assert asyncio.run(llm.chat("sys", "again")) == "DO: wait"
    assert ["temperature" in b for b in seen] == [True, False, False]


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


@pytest.mark.parametrize("body", [
    "Quota exceeded: GenerateRequestsPerDayPerProjectPerModel-FreeTier",
    '{"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}',
])
def test_exhausted_quota_fails_fast(body):
    llm = LLM("http://test/v1", "gemini")
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, text=body)

    llm._http = httpx.AsyncClient(base_url="http://test/v1", transport=httpx.MockTransport(handle))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(llm.chat("sys", "hi"))
    assert len(calls) == 1


def test_rate_limit_gives_up_after_the_last_wait(monkeypatch):
    monkeypatch.setattr("hidagent.llm.RATE_LIMIT_WAITS", (0, 0))
    llm = LLM("http://test/v1", "gemini")
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429)

    llm._http = httpx.AsyncClient(base_url="http://test/v1", transport=httpx.MockTransport(handle))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(llm.chat("sys", "hi"))
    assert len(calls) == 3


def test_temperature_retry_also_waits_out_a_rate_limit(monkeypatch):
    monkeypatch.setattr("hidagent.llm.RATE_LIMIT_WAITS", (0, 0))
    llm = LLM("http://test/v1", "claude")
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append("temperature" in body)
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "`temperature` is deprecated for this model."}})
        if seen.count(False) == 1:  # the first retry without temperature is rate limited
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "DO: wait"}}]})

    llm._http = httpx.AsyncClient(base_url="http://test/v1", transport=httpx.MockTransport(handle))
    assert asyncio.run(llm.chat("sys", "hi")) == "DO: wait"
    assert seen == [True, False, False]


def test_other_400_mentioning_temperature_is_not_swallowed():
    llm = LLM("http://test/v1", "gpt")
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "invalid image; temperature is fine"}})

    llm._http = httpx.AsyncClient(base_url="http://test/v1", transport=httpx.MockTransport(handle))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(llm.chat("sys", "hi"))
    assert len(seen) == 1 and "temperature" in seen[0]
    assert llm._temperature is True
