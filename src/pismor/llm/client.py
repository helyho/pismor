"""OpenAI 兼容 LLM 客户端：同步 httpx，JSON Schema 约束输出，transport 异常/429/5xx 指数退避重试。"""
import json
import logging
import os
import time

import httpx

from pismor.core.config import LLMSettings

logger = logging.getLogger("mem.llm")


class LLMClient:
    def __init__(self, settings: LLMSettings):
        self._settings = settings
        self._url = settings.endpoint.rstrip("/") + "/chat/completions"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self._settings.api_key_env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if self._settings.user_agent:
            headers["User-Agent"] = self._settings.user_agent
        return headers

    def _post_once(self, system: str, user: str, schema: dict) -> httpx.Response:
        # json_object 模式（DeepSeek/GLM）仅保证合法 JSON，schema 由提示词引导：
        # 把 JSON Schema 文本显式注入 system，否则模型自造顶层键（实测 items/entries），
        # 解析端 result.get("candidates") 拿空列表，候选全部丢失
        if (self._settings.structured_output_param
                and self._settings.structured_output_type == "json_object"):
            schema_text = json.dumps(schema.get("schema", {}), ensure_ascii=False)
            system = (f"{system}\n\n[输出格式硬约束] 必须输出一个严格 JSON 对象，"
                      f"顶层键与结构严格符合以下 JSON Schema；"
                      f"禁止改用其他顶层键（items、entries 等一律无效）：\n{schema_text}")
        payload = {
            "model": self._settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # 结构化输出：OpenAI json_schema（Schema 级强制）默认；DeepSeek/GLM 走
        # json_object（仅保证合法 JSON，schema 由提示词引导）。param 置空则整段省略。
        if self._settings.structured_output_param:
            fmt = {"type": self._settings.structured_output_type}
            if self._settings.structured_output_type == "json_schema":
                fmt["json_schema"] = schema
            payload[self._settings.structured_output_param] = fmt
        with httpx.Client(timeout=self._settings.timeout_seconds) as client:
            return client.post(self._url, headers=self._headers(), json=payload)

    def _backoff(self, attempt: int, response: httpx.Response | None = None) -> float:
        """重试间隔：指数退避；429 若带数值 Retry-After 则取其较大者。"""
        delay = self._settings.retry_backoff_seconds * (2 ** attempt)
        if response is not None and response.status_code == 429:
            raw = response.headers.get("Retry-After")
            try:
                delay = max(delay, float(raw))
            except (TypeError, ValueError):
                pass  # HTTP-date 形式不解析，退回指数退避
        return delay

    def chat_json(self, system: str, user: str, schema: dict) -> dict:
        """调用 /chat/completions 并解析 content 中的 JSON。

        transport 异常（超时/断连）与 429/5xx 共用 retries 次额外尝试；耗尽后最后一次
        响应直接处理（transport 耗尽抛最后异常）。content 非 JSON 时抛
        json.JSONDecodeError / ValueError；HTTP 非 2xx 抛 httpx.HTTPStatusError。
        """
        retries = max(0, self._settings.retries)
        logger.info("llm call: model=%s system=%dc user=%dc schema=%s",
                    self._settings.model, len(system), len(user), schema.get("name"))
        t0 = time.monotonic()
        response = None
        for attempt in range(retries + 1):
            try:
                response = self._post_once(system, user, schema)
            except httpx.TransportError as exc:
                logger.warning("llm transport error (attempt %d/%d): %r",
                               attempt + 1, retries + 1, exc)
                if attempt == retries:
                    raise  # 重试耗尽：异常冒泡由调用方处理
                time.sleep(self._backoff(attempt))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                logger.warning("llm retry on status %s (attempt %d/%d)",
                               response.status_code, attempt + 1, retries + 1)
                if attempt < retries:
                    time.sleep(self._backoff(attempt, response))
                continue  # 耗尽后走 raise_for_status，与旧语义一致
            break
        preview = response.text.replace("\n", " ")[:self._settings.log_preview_chars]
        logger.info("llm resp: status=%s %.0fms body=%dc preview=%s",
                    response.status_code, (time.monotonic() - t0) * 1000,
                    len(response.content), preview)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        if not content:
            # 拒答/内容过滤会让 content 为 null；json.loads(None) 只会抛难懂的 TypeError
            raise ValueError("llm response content is empty (message.content is null)")
        return json.loads(content)
