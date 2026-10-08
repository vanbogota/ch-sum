import json

import anthropic
import httpx2
import pytest

from ghostwriter.llm import LLM, LLMRefusal


def make_llm(response: dict, seen: list):
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.read()))
        return httpx2.Response(200, json=response)

    client = anthropic.AsyncAnthropic(
        api_key="test", http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    )
    return LLM("claude-sonnet-5", client=client)


def message(text: str, stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


async def test_json_request_shape():
    seen: list = []
    llm = make_llm(message('{"escalate": false}'), seen)
    out = await llm.json("sys", "user", {"type": "object"})
    assert out == {"escalate": False}
    body = seen[0]
    assert body["model"] == "claude-sonnet-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"] == [{"role": "user", "content": "user"}]


async def test_refusal_raises():
    llm = make_llm(message("", stop_reason="refusal"), [])
    with pytest.raises(LLMRefusal):
        await llm.text("sys", "user")


def test_empty_base_url_env_is_ignored(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
    llm = LLM("m", api_key="k")
    assert str(llm._client.base_url).startswith("https://api.anthropic.com")


def test_gateway_base_url(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    llm = LLM("m", api_key="sk-virtual", base_url="http://litellm:4000/anthropic")
    assert str(llm._client.base_url).rstrip("/") == "http://litellm:4000/anthropic"
