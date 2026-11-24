"""OpenAI 兼容 LLM 客户端：同步 httpx，JSON Schema 约束输出，429/5xx 重试 1 次。"""
import json
import os

import httpx

from mem.config import LLMSettings

_TIMEOUT = 30.0


class LLMClient:
    def __init__(self, settings: LLMSettings):
        self._settings = settings
        self._url = settings.endpoint.rstrip("/") + "/chat/completions"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self._settings.api_key_env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _post_once(self, system: str, user: str, schema: dict) -> httpx.Response:
        payload = {
            "model": self._settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_schema", "json_schema": schema},
        }
        with httpx.Client(timeout=_TIMEOUT) as client:
            return client.post(self._url, headers=self._headers(), json=payload)

    def chat_json(self, system: str, user: str, schema: dict) -> dict:
        """调用 /chat/completions 并解析 content 中的 JSON。

        429/5xx 重试 1 次；重试后的响应直接处理（仍失败则抛异常，由调用方处理）。
        content 非 JSON 时抛 json.JSONDecodeError / ValueError；HTTP 非 2xx 抛 httpx.HTTPStatusError。
        """
        response = self._post_once(system, user, schema)
        if response.status_code == 429 or response.status_code >= 500:
            response = self._post_once(system, user, schema)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)
