import json

import httpx
import pytest
import respx

from pismor.llm.embedder import EmbedUnavailable, Embedder
from pismor.core.config import EmbeddingSettings


@respx.mock
def test_embed_ok():
    respx.post("https://emb.test/v1/embeddings").respond(json={"data": [{"embedding": [0.1, 0.2]}]})
    emb = Embedder(EmbeddingSettings(True, "https://emb.test/v1", "m", "K", 2, 8))
    assert emb.embed(["你好"]) == [[0.1, 0.2]]


def test_disabled_raises():
    emb = Embedder(EmbeddingSettings(False, "", "", "", 2, 8))
    with pytest.raises(EmbedUnavailable):
        emb.embed(["x"])


@respx.mock
def test_service_down_raises():
    respx.post("https://emb.test/v1/embeddings").respond(500)
    emb = Embedder(EmbeddingSettings(True, "https://emb.test/v1", "m", "K", 2, 8))
    with pytest.raises(EmbedUnavailable):
        emb.embed(["x"])   # Review Focus #2


@respx.mock
def test_connection_error_raises():
    respx.post("https://emb.test/v1/embeddings").mock(side_effect=httpx.ConnectError("down"))
    emb = Embedder(EmbeddingSettings(True, "https://emb.test/v1", "m", "K", 2, 8))
    with pytest.raises(EmbedUnavailable):
        emb.embed(["x"])


@respx.mock
def test_batching_by_batch_size():
    route = respx.post("https://emb.test/v1/embeddings").mock(
        side_effect=[
            httpx.Response(200, json={"data": [{"embedding": [0.1]}, {"embedding": [0.2]}]}),
            httpx.Response(200, json={"data": [{"embedding": [0.3]}]}),
        ])
    emb = Embedder(EmbeddingSettings(True, "https://emb.test/v1", "m", "K", 1, 2))
    out = emb.embed(["a", "b", "c"])
    assert out == [[0.1], [0.2], [0.3]]
    assert route.call_count == 2


@respx.mock
def test_endpoint_with_trailing_embeddings_not_doubled():
    # 配置允许直接写完整 URL（example 即如此）；不得再追加一段 /embeddings
    route = respx.post("https://emb.test/v1/embeddings").respond(
        json={"data": [{"embedding": [0.5]}]})
    emb = Embedder(EmbeddingSettings(True, "https://emb.test/v1/embeddings", "m", "K", 1, 8))
    assert emb.embed(["x"]) == [[0.5]]
    assert route.call_count == 1


@respx.mock
def test_request_payload_and_missing_key():
    route = respx.post("https://emb.test/v1/embeddings").respond(
        json={"data": [{"embedding": [1.0]}]})
    emb = Embedder(EmbeddingSettings(True, "https://emb.test/v1", "m", "DEFINITELY_UNSET_KEY", 1, 8))
    assert emb.embed(["x"]) == [[1.0]]
    payload = json.loads(route.calls.last.request.content)
    assert payload == {"model": "m", "input": ["x"]}
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
def test_out_of_order_index_resorted():
    # F2: 规范要求 data[i].index 对应输入下标；网关乱序返回时按 index 重排对齐
    respx.post("https://emb.test/v1/embeddings").respond(
        json={"data": [
            {"index": 1, "embedding": [0.2]},
            {"index": 0, "embedding": [0.1]},
        ]})
    emb = Embedder(EmbeddingSettings(True, "https://emb.test/v1", "m", "K", 1, 8))
    assert emb.embed(["a", "b"]) == [[0.1], [0.2]]


@respx.mock
def test_non_finite_embedding_rejected():
    # F3: NaN 向量入库后该条目在向量检索中静默消失，按服务不可用降级
    # （部分服务输出宽松 JSON 字面量 NaN，标准编码器无法构造，故用原始字节）
    respx.post("https://emb.test/v1/embeddings").respond(
        200, content=b'{"data": [{"index": 0, "embedding": [NaN]}]}')
    emb = Embedder(EmbeddingSettings(True, "https://emb.test/v1", "m", "K", 1, 8))
    with pytest.raises(EmbedUnavailable):
        emb.embed(["x"])
