import json
import logging

import httpx
import pytest
import respx

from pismor.pipeline.gate import gate
from pismor.llm.client import LLMClient
from pismor.core.config import LLMSettings


@respx.mock
def test_chat_json_parses_content():
    route = respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": '{"ok": true}'}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m", api_key_env="TEST_KEY"))
    out = client.chat_json("sys", "user", {"name": "s", "schema": {}})
    assert out == {"ok": True}


@respx.mock
def test_retry_on_timeout():
    route = respx.post("https://llm.test/v1/chat/completions")
    route.side_effect = [
        httpx.ReadTimeout("timed out"),
        httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": 1}'}}]}),
    ]
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m", api_key_env="TEST_KEY"))
    assert client.chat_json("s", "u", {"name": "n", "schema": {}}) == {"ok": 1}


@respx.mock
def test_timeout_twice_raises():
    route = respx.post("https://llm.test/v1/chat/completions")
    route.side_effect = [httpx.ReadTimeout("timed out"), httpx.ReadTimeout("timed out")]
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m", api_key_env="TEST_KEY"))
    with pytest.raises(httpx.ReadTimeout):
        client.chat_json("s", "u", {"name": "n", "schema": {}})


@respx.mock
def test_timeout_retries_zero_raises_immediately():
    route = respx.post("https://llm.test/v1/chat/completions")
    route.side_effect = [httpx.ReadTimeout("timed out"), httpx.ReadTimeout("timed out")]
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m",
                                   api_key_env="TEST_KEY", retries=0))
    with pytest.raises(httpx.ReadTimeout):
        client.chat_json("s", "u", {"name": "n", "schema": {}})
    assert route.call_count == 1


@respx.mock
def test_retry_backoff_between_attempts(monkeypatch):
    # 429/5xx 重试之间必须退避（指数），否则连续打满上游
    from pismor.llm import client as client_mod

    slept = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: slept.append(s))
    route = respx.post("https://llm.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(500),
        httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": 1}'}}]}),
    ]
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m",
                                   api_key_env="TEST_KEY", retries=2,
                                   retry_backoff_seconds=0.4))
    assert client.chat_json("s", "u", {"name": "n", "schema": {}}) == {"ok": 1}
    assert slept == [0.4, 0.8]


@respx.mock
def test_null_content_raises_value_error():
    # 上游返回 content=null（拒答/被过滤）时给明确错误，而不是 json.loads(None) 的 TypeError
    respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": None}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m", api_key_env="TEST_KEY"))
    with pytest.raises(ValueError, match="content"):
        client.chat_json("s", "u", {"name": "n", "schema": {}})


@respx.mock
def test_chat_json_logs_call_and_response(caplog):
    respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": '{"ok": 1}'}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m", api_key_env="TEST_KEY"))
    with caplog.at_level(logging.INFO, logger="mem.llm"):
        client.chat_json("sys prompt", "usr prompt", {"name": "n", "schema": {}})
    texts = [r.getMessage() for r in caplog.records]
    assert any("llm call" in t and "model=m" in t and "schema=n" in t for t in texts)
    assert any("llm resp" in t and "status=200" in t for t in texts)


@respx.mock
def test_log_preview_chars_configurable(caplog):
    content = json.dumps({"pad": "x" * 600})  # content 须为合法 JSON（chat_json 会解析）
    respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": content}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m",
                                   api_key_env="TEST_KEY", log_preview_chars=40))
    with caplog.at_level(logging.INFO, logger="mem.llm"):
        client.chat_json("s", "u", {"name": "n", "schema": {}})
    resp = next(r.getMessage() for r in caplog.records if "llm resp" in r.getMessage())
    assert resp.count("x") <= 40  # 预览被截到配置长度


@respx.mock
def test_timeout_from_settings(monkeypatch):
    captured = {}
    real_client = httpx.Client

    def spy_client(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", spy_client)
    respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": "{}"}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m",
                                   api_key_env="TEST_KEY", timeout_seconds=7.5))
    client.chat_json("s", "u", {"name": "n", "schema": {}})
    assert captured["timeout"] == 7.5


@respx.mock
def test_retries_configurable():
    # retries=2：连续两次 500 第三次成功（旧固定 1 次会直接抛）
    route = respx.post("https://llm.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(500),
        httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": 1}'}}]}),
    ]
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m",
                                   api_key_env="TEST_KEY", retries=2))
    assert client.chat_json("s", "u", {"name": "n", "schema": {}}) == {"ok": 1}


@respx.mock
def test_retries_zero_no_retry_on_500():
    route = respx.post("https://llm.test/v1/chat/completions")
    route.side_effect = [httpx.Response(500), httpx.Response(200)]
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m",
                                   api_key_env="TEST_KEY", retries=0))
    with pytest.raises(httpx.HTTPStatusError):
        client.chat_json("s", "u", {"name": "n", "schema": {}})
    assert route.call_count == 1


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


def test_gate_thresholds_configurable():
    # 默认阈值：0.86 explicit 直通；提高 auto_min 后同候选落 review
    cand = {"dimension": "preference", "topic": "t",
            "confidence": 0.86, "kind": "explicit"}
    auto, review, _ = gate([dict(cand)])
    assert len(auto) == 1 and not review
    auto, review, _ = gate([dict(cand)], auto_min=0.9)
    assert not auto and len(review) == 1
    # inferred_cap 压顶（0.9 → 0.8）后落 review；review_min 抬高到 0.9 → 低置信丢弃
    inferred = dict(cand, kind="inferred", confidence=0.9)
    _, review, dropped = gate([inferred], inferred_cap=0.8, review_min=0.8)
    assert review[0]["confidence"] == 0.8 and not dropped
    _, review, dropped = gate([dict(inferred)], inferred_cap=0.8, review_min=0.9)
    assert not review and len(dropped) == 1


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


@respx.mock
def test_user_agent_injected():
    route = respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": "{}"}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m",
                                   api_key_env="TEST_KEY", user_agent="custom-agent/1.0"))
    client.chat_json("s", "u", {"name": "n", "schema": {}})
    assert route.calls.last.request.headers.get("user-agent") == "custom-agent/1.0"


@respx.mock
def test_user_agent_not_overridden_by_default():
    route = respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": "{}"}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m", api_key_env="TEST_KEY"))
    client.chat_json("s", "u", {"name": "n", "schema": {}})
    ua = route.calls.last.request.headers.get("user-agent", "")
    assert ua.startswith("python-httpx")  # 未配置时保持 httpx 原生 UA，不注入


@respx.mock
def test_structured_output_param_renamable():
    route = respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": "{}"}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m",
                                   api_key_env="TEST_KEY",
                                   structured_output_param="guided_schema"))
    client.chat_json("s", "u", {"name": "n", "schema": {"type": "object"}})
    payload = json.loads(route.calls.last.request.content)
    assert payload["guided_schema"] == {"type": "json_schema",
                                        "json_schema": {"name": "n", "schema": {"type": "object"}}}
    assert "response_format" not in payload


@respx.mock
def test_structured_output_param_empty_omits_field():
    route = respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": "{}"}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m",
                                   api_key_env="TEST_KEY", structured_output_param=""))
    client.chat_json("s", "u", {"name": "n", "schema": {}})
    payload = json.loads(route.calls.last.request.content)
    assert "response_format" not in payload


@respx.mock
def test_structured_output_json_object_shape():
    # DeepSeek/GLM 模式：只传 type，schema 不进 payload（结构由提示词引导）
    route = respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": "{}"}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m",
                                   api_key_env="TEST_KEY",
                                   structured_output_type="json_object"))
    client.chat_json("s", "u", {"name": "n", "schema": {"type": "object"}})
    payload = json.loads(route.calls.last.request.content)
    assert payload["response_format"] == {"type": "json_object"}
    assert "json_schema" not in payload["response_format"]

@respx.mock
def test_json_object_schema_injected_into_prompt():
    # json_object 模式仅保证合法 JSON，schema 必须由提示词引导（实测 DeepSeek 自造
    # items/entries 顶层键导致候选全丢）——注入 schema 文本到 system prompt
    route = respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": "{}"}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m",
                                   api_key_env="TEST_KEY",
                                   structured_output_type="json_object"))
    schema = {"name": "n", "schema": {"type": "object",
                                      "properties": {"candidates": {"type": "array"}}}}
    client.chat_json("s", "u", schema)
    payload = json.loads(route.calls.last.request.content)
    sys_content = payload["messages"][0]["content"]
    assert '"candidates"' in sys_content  # schema 注入：容器键显式可见
    assert "禁止" in sys_content  # 自造顶层键的禁用约束在
@respx.mock
def test_json_schema_mode_not_double_inject_schema():
    # json_schema 模式由 response_format 强制，system prompt 保持原样不重复注入
    route = respx.post("https://llm.test/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": "{}"}}]})
    client = LLMClient(LLMSettings(endpoint="https://llm.test/v1", model="m", api_key_env="TEST_KEY"))
    client.chat_json("s", "u", {"name": "n", "schema": {"type": "object"}})
    payload = json.loads(route.calls.last.request.content)
    assert payload["messages"][0]["content"] == "s"
