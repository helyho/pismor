import json

import httpx
import pytest
import respx

from mem.embedder import EmbedUnavailable, Embedder
from mem.config import EmbeddingSettings


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
def test_request_payload_and_missing_key():
    route = respx.post("https://emb.test/v1/embeddings").respond(
        json={"data": [{"embedding": [1.0]}]})
    emb = Embedder(EmbeddingSettings(True, "https://emb.test/v1", "m", "DEFINITELY_UNSET_KEY", 1, 8))
    assert emb.embed(["x"]) == [[1.0]]
    payload = json.loads(route.calls.last.request.content)
    assert payload == {"model": "m", "input": ["x"]}
    assert "authorization" not in route.calls.last.request.headers
