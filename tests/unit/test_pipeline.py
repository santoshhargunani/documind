from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from documind.ingestion import pipeline
from documind.ingestion.chunker import Chunk
from documind.ingestion.embedder import EmbeddedChunk


def test_ingestion_persists_batches_and_resumes_from_saved_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [
        Chunk(text=f"chunk-{index}", chunk_index=index, char_start=index, char_end=index + 1)
        for index in range(12)
    ]
    stored_texts: dict[int, str] = {}
    attempts = iter((1, 2))
    embedding_inputs: list[list[Chunk]] = []
    embedding_calls = 0

    blob = SimpleNamespace(
        generation=7,
        md5_hash="hash",
        crc32c="crc",
        size=100,
        open=lambda _mode: nullcontext(object()),
    )
    storage_client = Mock()
    storage_client.bucket.return_value.get_blob.return_value = blob

    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(
            ingest_max_pdf_mb=50,
            ingest_lease_seconds=900,
            ingest_max_attempts=5,
        ),
    )
    monkeypatch.setattr(pipeline, "_validate_pdf", lambda _blob: None)
    monkeypatch.setattr(pipeline, "_storage_client", lambda: storage_client)
    monkeypatch.setattr(
        pipeline, "get_tenant", lambda _tenant_id: SimpleNamespace(status="active")
    )
    monkeypatch.setattr(
        pipeline,
        "claim_document",
        lambda **_: SimpleNamespace(
            outcome="claimed",
            document_id="document-id",
            attempts=next(attempts),
            last_error=None,
        ),
    )
    monkeypatch.setattr(pipeline, "count_pdf_pages", lambda _file: 1)
    monkeypatch.setattr(pipeline, "parse_document", lambda *_args, **_kwargs: ("text", 1))
    monkeypatch.setattr(pipeline, "chunk_text", lambda _text: chunks)
    monkeypatch.setattr(pipeline, "reset_document_chunks", lambda *_args: stored_texts.clear())
    monkeypatch.setattr(
        pipeline,
        "get_document_chunk_texts",
        lambda *_args: dict(stored_texts),
    )

    def persist(
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        embedded_chunks: list[EmbeddedChunk],
    ) -> int:
        for item in embedded_chunks:
            stored_texts[item.chunk.chunk_index] = item.chunk.text
        return len(embedded_chunks)

    monkeypatch.setattr(pipeline, "persist_chunk_batch", persist)
    monkeypatch.setattr(
        pipeline,
        "finalize_document",
        lambda **kwargs: kwargs["expected_chunk_count"],
    )
    monkeypatch.setattr(pipeline, "release_claim", Mock())

    def embed_batches(remaining_chunks):
        nonlocal embedding_calls
        embedding_calls += 1
        embedding_inputs.append(list(remaining_chunks))
        if embedding_calls == 1:
            yield [
                EmbeddedChunk(chunk=chunk, embedding=[0.0])
                for chunk in remaining_chunks[:10]
            ]
            raise RuntimeError("temporary embedding failure")
        yield [
            EmbeddedChunk(chunk=chunk, embedding=[0.0])
            for chunk in remaining_chunks
        ]

    monkeypatch.setattr(pipeline, "embed_chunk_batches", embed_batches)

    first = pipeline.ingest_object("bucket", "acme/document.pdf")
    first_persisted_count = len(stored_texts)
    second = pipeline.ingest_object("bucket", "acme/document.pdf")

    assert first.status == "retry_later"
    assert first_persisted_count == 10
    assert second.status == "ingested"
    assert second.chunk_count == 12
    assert len(stored_texts) == 12
    assert [chunk.chunk_index for chunk in embedding_inputs[1]] == [10, 11]
