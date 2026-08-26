"""
retriever.py
------------
The second node in the DocuMind agent graph (runs when the router
decides route == "retrieve"). Embeds the user's question and searches
pgvector for the most semantically similar stored chunks.

This node is deliberately "thin" — it doesn't do any reasoning or
generation itself. Its only job is: question in, ranked evidence out.
The synthesis agent (Step 5.4) is what actually turns that evidence
into an answer. Keeping retrieval and generation as separate graph
nodes (rather than one big "answer the question" function) is what
makes each step independently testable, debuggable, and swappable —
e.g. we could later replace this node's search strategy (add keyword
search, add reranking) without touching synthesis at all.
"""

# --- IMPORTS ---

import structlog
# Structured logging, consistent with every other module in this
# project.

from google import genai
# Current Vertex AI SDK — used here for the QUERY-side embedding call.
# Note: embedder.py (Step 4.7) still uses the older
# vertexai.language_models SDK for DOCUMENT-side embeddings during
# ingestion. Both produce compatible 768-dimension vectors from the
# same underlying text-embedding-005 model, so mixing SDK generations
# across ingestion vs. retrieval doesn't cause a functional problem —
# it's a code-cleanliness item to unify later, not a correctness bug.

from documind.agents.state import AgentState, RetrievedChunk
# The shared graph state (Step 5.1) — this node reads state["question"]
# and returns an update to state["retrieved_chunks"]. RetrievedChunk is
# the Pydantic model we build each search result into.

from documind.config import get_settings
from documind.db import get_connection
# Reusing the exact same connection function from db.py (Step 4.8) —
# one place that knows how to connect to Cloud SQL, used by both the
# ingestion write path and this retrieval read path.


# --- LOGGER SETUP ---

logger = structlog.get_logger()


# --- CONSTANTS ---

_TOP_K = 3
# How many chunks to retrieve per question. Same "top-k retrieval"
# concept discussed when we tested similarity search manually in
# Step 4 — kept as a named constant here (rather than a magic number
# buried in the SQL call below) so it's easy to find and tune later,
# and easy to eventually promote to a Settings field if we want it
# configurable per-environment.


# --- CLIENT SETUP ---

def _get_genai_client() -> genai.Client:
    """
    Same client construction pattern as router.py — builds a
    genai.Client configured to call Vertex AI using our GCP project's
    Application Default Credentials.
    """

    settings = get_settings()
    return genai.Client(
        vertexai=True,
        project=settings.gcp_project_id,
        location=settings.gcp_region,
    )


# --- EMBEDDING HELPER ---

def _embed_query(question: str) -> list[float]:
    """
    Embeds the user's question using the CURRENT google-genai SDK,
    with task_type="RETRIEVAL_QUERY" — not "RETRIEVAL_DOCUMENT".

    This is the query-side counterpart to the task_type distinction
    flagged as critical back in embedder.py (Step 4.7): the same
    embedding model produces a differently-optimized vector depending
    on whether text is being stored for later search, or is itself a
    live search query. Getting this backwards here (using
    RETRIEVAL_DOCUMENT for a query) is exactly the kind of subtle bug
    that degrades search quality without ever raising an error.
    """

    settings = get_settings()
    client = _get_genai_client()

    response = client.models.embed_content(
        model=settings.embedding_model,
        contents=question,
        config=genai.types.EmbedContentConfig(task_type="RETRIEVAL_QUERY"),
    )
    # The google-genai SDK's embedding call — a different method
    # signature than the older vertexai.language_models SDK used in
    # embedder.py (get_embeddings() there vs. embed_content() here),
    # but conceptually doing the same job: text in, vector out.

    return response.embeddings[0].values
    # embed_content can accept and return multiple embeddings in one
    # call (hence a list); we only sent one question, so we take the
    # first (only) result.


# --- SIMILARITY SEARCH HELPER ---

def _search_chunks(query_embedding: list[float], top_k: int) -> list[RetrievedChunk]:
    """
    Runs the actual pgvector cosine-similarity search — the same
    query pattern proved out in scripts/test_retrieval.py (Step 4),
    now wrapped as a reusable function instead of a one-off script.
    """

    query_vector_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
    # Same pgvector text-format conversion used throughout this
    # project (db.py's insert_chunks, and the original
    # test_retrieval.py script) — pg8000 needs the vector as a
    # bracketed, comma-separated string, cast via ::vector in SQL.

    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                document_id,
                text,
                1 - (embedding <=> %s::vector) AS similarity
            FROM chunks
            ORDER BY embedding <=> %s::vector
            LIMIT %s;
            """,
            (query_vector_str, query_vector_str, top_k),
        )
        # Identical logic to test_retrieval.py's query: <=> is
        # pgvector's cosine distance operator (matches the
        # vector_cosine_ops HNSW index built in Step 4.8's schema),
        # ORDER BY it ascending to get the most similar chunks first,
        # LIMIT to top_k results. The only addition here is selecting
        # document_id too, since RetrievedChunk (Step 5.1) tracks
        # which document each chunk came from.

        rows = cursor.fetchall()

    return [
        RetrievedChunk(document_id=str(document_id), chunk_text=text, similarity=similarity)
        for document_id, text, similarity in rows
    ]
    # A list comprehension converting each raw database row (a tuple
    # of three values) into a proper RetrievedChunk Pydantic object —
    # matching the type our graph state expects, rather than passing
    # raw tuples further down the pipeline.


# --- MAIN NODE FUNCTION ---

def retrieve_chunks(state: AgentState) -> dict:
    """
    The LangGraph node function. Takes the current state (needs
    state["question"]), embeds it, searches pgvector, and returns a
    partial state update setting retrieved_chunks.

    Recall from Step 5.1: retrieved_chunks uses an Annotated[...,
    add] reducer in AgentState, meaning if this node runs more than
    once for the same question (e.g. the critic sends the graph back
    for another retrieval attempt), results from each run get
    APPENDED to the existing list rather than replacing it.
    """

    question = state["question"]

    query_embedding = _embed_query(question)
    chunks = _search_chunks(query_embedding, top_k=_TOP_K)

    logger.info(
        "chunks_retrieved",
        question=question,
        chunk_count=len(chunks),
        top_similarity=chunks[0].similarity if chunks else None,
    )

    return {"retrieved_chunks": chunks}
    # Only returns retrieved_chunks — this node isn't responsible for
    # any other AgentState field, consistent with the partial-update
    # pattern every node in this graph follows.
