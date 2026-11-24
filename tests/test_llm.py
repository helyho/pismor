import json

import httpx
import pytest
import respx

from mem.llm import LLMClient
from mem.config import LLMSettings


@respx.mock
def test_chat_json_parses_content():
    route = respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": '{"ok": true}'}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m", api_key_env="TEST_KEY"))
    out = client.chat_json("sys", "user", {"name": "s", "schema": {}})
    assert out == {"ok": True}


@respx.mock
def test_retry_on_500():
    route = respx.post("https://llm.test/v1/chat/completions")
    route.side_effect = [httpx.Response(500), httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})]
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m", api_key_env="TEST_KEY"))
    assert client.chat_json("s", "u", {"name": "n", "schema": {}}) == {}


@respx.mock
def test_bad_json_raises():
    respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": "not-json"}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m", api_key_env="TEST_KEY"))
    with pytest.raises(Exception):
        client.chat_json("s", "u", {"name": "n", "schema": {}})


@respx.mock
def test_missing_api_key_still_sends_request():
    route = respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": '{"ok": 1}'}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m", api_key_env="DEFINITELY_UNSET_KEY"))
    assert client.chat_json("s", "u", {"name": "n", "schema": {}}) == {"ok": 1}
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
def test_request_payload_shape():
    route = respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": "{}"}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m", api_key_env="TEST_KEY"))
    client.chat_json("sys prompt", "usr prompt", {"name": "s", "schema": {"type": "object"}})
    payload = json.loads(route.calls.last.request.content)
    assert payload["model"] == "m"
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "s", "schema": {"type": "object"}},
    }
    assert payload["messages"][0] == {"role": "system", "content": "sys prompt"}
    assert payload["messages"][1] == {"role": "user", "content": "usr prompt"}
