from types import SimpleNamespace

import pytest

from documind.ingestion import embedder
from documind.ingestion.chunker import Chunk


def _chunks(count: int) -> list[Chunk]:
    return [
        Chunk(text=f"chunk-{index}", chunk_index=index, char_start=index, char_end=index + 1)
        for index in range(count)
    ]


def test_embedding_request_pacing_limits_chunks_to_ten_per_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(embedder.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(embedder.time, "sleep", sleep)
    monkeypatch.setattr(embedder, "_next_embedding_request_at", 0.0)

    embedder._pace_embedding_request(5)
    embedder._pace_embedding_request(5)

    assert waits == [30.0]
    assert now[0] == 130.0


def test_embed_chunk_batches_yields_groups_of_ten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = _chunks(23)
    monkeypatch.setattr(
        embedder,
        "get_settings",
        lambda: SimpleNamespace(embedding_model="test-model"),
    )
    monkeypatch.setattr(embedder, "_init_vertexai", lambda: None)
    monkeypatch.setattr(
        embedder.TextEmbeddingModel,
        "from_pretrained",
        lambda _: object(),
    )
    monkeypatch.setattr(
        embedder,
        "_embed_batch",
        lambda _model, batch: [
            embedder.EmbeddedChunk(chunk=chunk, embedding=[0.0]) for chunk in batch
        ],
    )

    batches = list(embedder.embed_chunk_batches(chunks))

    assert [len(batch) for batch in batches] == [10, 10, 3]
    assert [item.chunk.chunk_index for batch in batches for item in batch] == list(range(23))
