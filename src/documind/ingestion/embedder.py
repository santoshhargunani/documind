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

import structlog
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
# Vertex AI SDK classes specific to text embeddings.
#   - TextEmbeddingModel: the model handle itself (loaded via
#     from_pretrained), used to actually request embeddings.
#   - TextEmbeddingInput: wraps a piece of text together with metadata
#     about *how* it will be used (see task_type explanation below,
#     inside _embed_batch) — this affects the resulting vector.

import vertexai
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
# How many chunks we send to Vertex AI in a single API call. Leading
# underscore = internal constant, not meant to be imported by other
# modules. Batching multiple texts per call reduces network round-trips
# compared to embedding one chunk at a time, improving both latency and
# API quota usage. 5 is a conservative starting value — real tuning
# would push this higher based on the model's actual batch limits and
# typical chunk sizes.


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


# --- INTERNAL BATCH EMBEDDING HELPER ---

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
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

    inputs = [
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


# --- PUBLIC FUNCTION ---

def embed_chunks(chunks: list[Chunk]) -> list[EmbeddedChunk]:
    """
    Public entry point: takes a list of Chunk objects (from chunker.py)
    and returns a list of EmbeddedChunk objects, each carrying its
    original chunk plus a 768-dimension embedding vector.

    Processes chunks in batches of _EMBED_BATCH_SIZE to balance API
    efficiency (fewer round-trips than one-at-a-time) against not
    overwhelming a single request with too much text at once.
    """

    settings = get_settings()
    # Needed for settings.embedding_model below.

    _init_vertexai()
    # Must run before loading any model — see _init_vertexai() above.

    model = TextEmbeddingModel.from_pretrained(settings.embedding_model)
    # Loads the embedding model handle by name. settings.embedding_model
    # defaults to "text-embedding-005" (set in config.py, Step 4.3).
    # This happens once per call to embed_chunks — not once per batch —
    # avoiding redundant model-loading overhead.

    results: list[EmbeddedChunk] = []
    # The list we'll build up across all batches and return at the end.
    # Type-annotated as list[EmbeddedChunk] so mypy --strict can verify
    # nothing else accidentally gets added to it.

    for i in range(0, len(chunks), _EMBED_BATCH_SIZE):
        # range(0, len(chunks), _EMBED_BATCH_SIZE) steps through
        # indices 0, 5, 10, 15, ... — the standard Python pattern for
        # walking through a list in fixed-size batches.

        batch = chunks[i : i + _EMBED_BATCH_SIZE]
        # Slice out the current batch: chunks i, i+1, ..., i+4 (up to
        # 5 items, or fewer for the final partial batch).

        embedded_batch = _embed_batch(model, batch)
        # Embed this one batch (with retry/backoff already applied
        # inside _embed_batch).

        results.extend(embedded_batch)
        # .extend() appends each item from embedded_batch individually
        # onto results — as opposed to .append(), which would nest the
        # whole batch as one single list-inside-a-list. extend() keeps
        # `results` as one flat list of EmbeddedChunk objects.

        logger.info(
            "embedded_batch",
            batch_start=i,
            batch_size=len(batch),
            total_processed=len(results),
        )
        # Structured progress log after each batch — useful for
        # watching a large document's embedding progress in real time
        # via Cloud Logging, and for spotting exactly where a slowdown
        # or failure happened in a long-running ingestion job.

    logger.info("embedding_complete", total_chunks=len(results), model=settings.embedding_model)
    # Final summary log once all batches are done.

    return results
    # Hand back the full list of EmbeddedChunk objects — ready to be
    # written into Cloud SQL/pgvector in the next step.
