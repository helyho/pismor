"""嵌入服务客户端：OpenAI 兼容 /embeddings，可降级——不可用抛 EmbedUnavailable，不阻断主流程。"""
import math
import os

import httpx

from pismor.core.config import EmbeddingSettings


class EmbedUnavailable(Exception):
    """嵌入服务未启用、连接失败或返回非 2xx 时抛出。Task 7/13 捕获后降级继续。"""


def l2_normalize(vec: list[float]) -> list[float]:
    """L2 归一化；零向量/非法数值原样返回（装配侧余弦→距离换算依赖单位向量前提）。"""
    try:
        norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    except (TypeError, ValueError):
        return vec
    # NaN 与任何比较均为 False：`norm <= 0` 挡不住 NaN，会走进除法产出全 NaN 向量
    if not (norm > 0.0) or not math.isfinite(norm):
        return vec
    return [float(x) / norm for x in vec]


class Embedder:
    def __init__(self, settings: EmbeddingSettings):
        self._settings = settings
        base = settings.endpoint.rstrip("/")
        # endpoint 允许写 base（…/v1）或完整路径（…/v1/embeddings），两种都接受
        self._url = base if base.endswith("/embeddings") else base + "/embeddings"
        # F19: 实例级连接复用——一次任务内多次批量嵌入不再重复建 TLS 连接
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._settings.timeout_seconds)
        return self._client

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self._settings.api_key_env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _post_batch(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self._settings.model, "input": texts}
        try:
            response = self._http().post(self._url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:  # 连接失败、超时等
            raise EmbedUnavailable(f"embed request failed: {exc}") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise EmbedUnavailable(f"embed service returned {response.status_code}")
        data = response.json()["data"]
        # F2: 规范要求 data[i].index 对应输入下标，但部分网关/实现乱序返回；
        # 全部带 index 时按 index 重排，缺 index（本地推理服务）时保序
        if all(isinstance(item.get("index"), int) for item in data):
            data = sorted(data, key=lambda item: item["index"])
        vecs = [item["embedding"] for item in data]
        # F3: NaN/Inf 向量入库后该条目在向量检索中静默消失，按服务不可用降级处理
        if not all(math.isfinite(float(x)) for vec in vecs for x in vec):
            raise EmbedUnavailable("embed service returned non-finite values")
        return vecs

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入；按 batch_size 分批请求，返回顺序与输入一致。"""
        if not self._settings.enabled:
            raise EmbedUnavailable("embedding disabled by config")
        out: list[list[float]] = []
        size = max(1, self._settings.batch_size)
        for i in range(0, len(texts), size):
            out.extend(self._post_batch(texts[i:i + size]))
        return out
