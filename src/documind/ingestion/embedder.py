"""
embedder.py
-----------
Turns text chunks (produced by chunker.py) into vector embeddings using
Vertex AI, so they can be stored in Cloud SQL/pgvector and later searched
by semantic similarity.

Why this file exists:
An embedding is a list of numbers (a vector) that represents the *meaning*
of a piece of text. Texts with similar meaning end up as vectors that are
mathematically close together. RAG retrieval works by embedding a user's
question the same way, then finding which stored chunk-vectors are
closest to it — those chunks are handed to the LLM as context.
"""

# --- IMPORTS ---

import time
from collections.abc import Iterator
from threading import Lock

import structlog

# Vertex AI SDK classes specific to text embeddings.
#   - TextEmbeddingModel: the model handle itself (loaded via
#     from_pretrained), used to actually request embeddings.
#   - TextEmbeddingInput: wraps a piece of text together with metadata
#     about *how* it will be used (see task_type explanation below,
#     inside _embed_batch) — this affects the resulting vector.
import vertexai

# Structured (JSON) logging library, same as every other ingestion module
# (storage.py, parser.py, chunker.py). Gives us queryable log fields
# (e.g. total_chunks=42) in Cloud Logging instead of a plain text message.
from pydantic import BaseModel

# Only BaseModel is needed here — we're defining one simple data model
# (EmbeddedChunk) with no custom validators, so nothing else from
# Pydantic is required. Importing only what's used keeps the file's
# purpose clear at a glance.
from tenacity import retry, stop_after_attempt, wait_exponential

# The retry library used throughout this project (Secret Manager client,
# GCS upload, Document AI parsing). Embedding calls go over the network
# to Vertex AI and can fail transiently — retrying with exponential
# backoff is the standard production-resilience pattern we're applying
# consistently across every external call in this codebase.
from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel

# The top-level Vertex AI SDK package. Needed for vertexai.init(), which
# authenticates the SDK and points it at a specific GCP project/region
# before any model can be loaded. This is a separate, required step —
# unlike some other GCP clients (e.g. Secret Manager, Storage) that take
# project= directly in their constructor, Vertex AI's SDK configures
# itself globally per-process via this init() call.
from documind.config import get_settings

# Our Pydantic Settings loader (Step 4.3) — gives us gcp_project_id,
# gcp_region, and embedding_model without hardcoding any of them here.
from documind.ingestion.chunker import Chunk

# Reusing the Chunk model defined in chunker.py rather than redefining
# chunk fields in this file too. One source of truth for "what a chunk
# looks like" — this module just adds an embedding on top of it.


# --- LOGGER SETUP ---

logger = structlog.get_logger()
# Module-level logger instance, bound to this file, used for structured
# progress/completion logging throughout the embedding process.


# --- CONSTANTS ---

_EMBED_BATCH_SIZE = 5
_CHUNKS_PER_DB_BATCH = 10
_CHUNKS_PER_MINUTE = 10

_rate_limit_lock = Lock()
_next_embedding_request_at = 0.0


# --- DATA MODEL ---

class EmbeddedChunk(BaseModel):
    """
    Pairs a Chunk with the vector embedding Vertex AI generated for it.
    This is the object we'll eventually write into Cloud SQL/pgvector —
    it carries both the original text (and its metadata, via `chunk`)
    and the numeric vector representation needed for similarity search.
    """

    chunk: Chunk
    # A Pydantic model nested inside another Pydantic model. Holds the
    # original chunk's text, index, and character offsets (from
    # chunker.py) so we never lose traceability back to the source text.

    embedding: list[float]
    # The actual vector — a list of floating-point numbers. For the
    # text-embedding-005 model, this will be a list of exactly 768
    # numbers (the model's fixed output dimensionality). This is what
    # gets stored in a pgvector `vector(768)` column in the next step.


# --- INTERNAL SETUP HELPER ---

def _init_vertexai() -> None:
    """
    Authenticates and configures the Vertex AI SDK for this process.
    Must be called before loading any Vertex AI model.
    """

    settings = get_settings()
    # Load our cached Settings instance (project ID, region, etc.).

    vertexai.init(project=settings.gcp_project_id, location=settings.gcp_region)
    # Configures the SDK's global state: which GCP project to bill/use,
    # and which region to run in. "-> None" in the function signature
    # above is a type hint saying this function has no return value —
    # it only has a side effect (configuring the SDK), which is exactly
    # what we're doing here.


def _pace_embedding_request(chunk_count: int) -> None:
    """Reserve time for a request so input chunks average at most 10 per minute."""
    global _next_embedding_request_at

    with _rate_limit_lock:
        now = time.monotonic()
        scheduled_at = max(now, _next_embedding_request_at)
        wait_seconds = scheduled_at - now
        _next_embedding_request_at = scheduled_at + (60 * chunk_count / _CHUNKS_PER_MINUTE)

    if wait_seconds > 0:
        logger.info("embedding_rate_limit_wait", wait_seconds=round(wait_seconds, 2))
        time.sleep(wait_seconds)


# --- INTERNAL BATCH EMBEDDING HELPER ---

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
# Retries this function up to 3 times if it raises an exception, with
# exponential backoff between attempts (starting around 2 seconds,
# doubling up to a 30-second cap). Same resilience pattern used for
# Document AI parsing calls in parser.py — network calls to external
# APIs can fail transiently and deserve a retry before giving up.
def _embed_batch(model: TextEmbeddingModel, chunks: list[Chunk]) -> list[EmbeddedChunk]:
    """
    Embeds a single batch of chunks (up to _EMBED_BATCH_SIZE) in one
    Vertex AI API call. Kept separate from embed_chunks() below so that
    the @retry decorator applies per-batch, not to the whole document —
    if chunk 47 of 50 transiently fails, we don't want to re-embed
    chunks 1-46 unnecessarily.
    """

    inputs: list[str | TextEmbeddingInput] = [
        TextEmbeddingInput(text=chunk.text, task_type="RETRIEVAL_DOCUMENT")
        for chunk in chunks
    ]
    # A list comprehension building one TextEmbeddingInput per chunk.
    #
    # task_type="RETRIEVAL_DOCUMENT" is the most important detail in
    # this whole file. Vertex AI's embedding model produces DIFFERENT
    # vectors for the same text depending on the declared task type,
    # because a document being stored for later retrieval and a
    # question being asked right now play different roles in the
    # embedding space, even if they happened to be identical strings.
    #
    # We use RETRIEVAL_DOCUMENT here because we're embedding chunks
    # *for storage*. Later, when we embed a user's live question to
    # search against these stored vectors, we'll use RETRIEVAL_QUERY
    # instead. Mismatching these two task types is a real, subtle bug
    # that silently degrades retrieval quality without ever throwing
    # an error — worth remembering.

    _pace_embedding_request(len(chunks))
    embeddings = model.get_embeddings(inputs)
    # The actual API call to Vertex AI. Sends the entire batch in one
    # request; gets back one embedding result per input, in the same
    # order they were sent.

    return [
        EmbeddedChunk(chunk=chunk, embedding=embedding.values)
        for chunk, embedding in zip(chunks, embeddings, strict=True)
    ]
    # zip(chunks, embeddings, strict=True) pairs each original chunk
    # with its corresponding embedding result, position by position.
    # strict=True (Python 3.10+) makes zip raise an error if the two
    # lists ever have different lengths, instead of silently
    # truncating to the shorter one — a fail-loudly-not-silently
    # choice, consistent with the guard clause pattern used elsewhere
    # in this project (e.g. chunker.py's chunk_overlap check).
    #
    # embedding.values pulls the actual list[float] vector out of the
    # SDK's response object for each embedding.


# --- PUBLIC FUNCTIONS ---

def embed_chunk_batches(chunks: list[Chunk]) -> Iterator[list[EmbeddedChunk]]:
    """Embed and yield groups of ten chunks so each group can be persisted."""
    if not chunks:
        return

    settings = get_settings()
    _init_vertexai()
    model = TextEmbeddingModel.from_pretrained(settings.embedding_model)

    total_processed = 0
    for group_start in range(0, len(chunks), _CHUNKS_PER_DB_BATCH):
        group = chunks[group_start : group_start + _CHUNKS_PER_DB_BATCH]
        embedded_group: list[EmbeddedChunk] = []

        for batch_start in range(0, len(group), _EMBED_BATCH_SIZE):
            batch = group[batch_start : batch_start + _EMBED_BATCH_SIZE]
            embedded_group.extend(_embed_batch(model, batch))

        total_processed += len(embedded_group)
        logger.info(
            "embedding_batch_ready",
            batch_start=group_start,
            batch_size=len(embedded_group),
            total_processed=total_processed,
        )
        yield embedded_group

    logger.info("embedding_complete", total_chunks=total_processed, model=settings.embedding_model)


def embed_chunks(chunks: list[Chunk]) -> list[EmbeddedChunk]:
    """Embed all chunks and return them as one list for existing callers."""
    return [item for batch in embed_chunk_batches(chunks) for item in batch]
