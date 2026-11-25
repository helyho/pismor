"""嵌入服务客户端：OpenAI 兼容 /embeddings，可降级——不可用抛 EmbedUnavailable，不阻断主流程。"""
import os

import httpx

from pismor.core.config import EmbeddingSettings


class EmbedUnavailable(Exception):
    """嵌入服务未启用、连接失败或返回非 2xx 时抛出。Task 7/13 捕获后降级继续。"""


class Embedder:
    def __init__(self, settings: EmbeddingSettings):
        self._settings = settings
        base = settings.endpoint.rstrip("/")
        # endpoint 允许写 base（…/v1）或完整路径（…/v1/embeddings），两种都接受
        self._url = base if base.endswith("/embeddings") else base + "/embeddings"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self._settings.api_key_env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _post_batch(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self._settings.model, "input": texts}
        try:
            with httpx.Client(timeout=self._settings.timeout_seconds) as client:
                response = client.post(self._url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:  # 连接失败、超时等
            raise EmbedUnavailable(f"embed request failed: {exc}") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise EmbedUnavailable(f"embed service returned {response.status_code}")
        data = response.json()["data"]
        return [item["embedding"] for item in data]

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入；按 batch_size 分批请求，返回顺序与输入一致。"""
        if not self._settings.enabled:
            raise EmbedUnavailable("embedding disabled by config")
        out: list[list[float]] = []
        size = max(1, self._settings.batch_size)
        for i in range(0, len(texts), size):
            out.extend(self._post_batch(texts[i:i + size]))
        return out
