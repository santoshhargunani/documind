"""
test_retrieval.py
------------------
Proves the ingestion pipeline is actually retrieval-ready: embeds a
sample question the same way a real user query will be embedded, then
runs a real pgvector similarity search against the chunks we stored
in Step 4.8, and prints which chunks came back closest.

This is the first code in the project that touches the RETRIEVAL side
of RAG, as opposed to the INGESTION side everything else so far has
built. Step 5 (LangGraph agents) will wrap this same core logic in a
proper "retriever agent."
"""

# --- IMPORTS ---

import vertexai
# Needed to initialize the Vertex AI SDK, same as in embedder.py —
# required before loading any embedding model.

from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel
# Same embedding classes used in embedder.py. Note we're about to use
# a DIFFERENT task_type than embedder.py did — see the comment below,
# this is the most important detail in this whole script.

from documind.config import get_settings
# Settings loader — gives us gcp_project_id, gcp_region, embedding_model.

from documind.db import get_connection
# Reuses the exact same connection function from db.py (Step 4.8) —
# no new connection logic needed, just a different query.


# --- CONFIG / TEST INPUT ---

settings = get_settings()

TEST_QUESTION = "What cloud technologies does this person have experience with?"
# A question we already know the answer to, since we can see the
# resume text — this lets us sanity-check whether the RIGHT chunk
# (the one mentioning "Cloud Technologies : Pivotal Cloud Foundry,
# Azure, Google Cloud (GCP)") actually comes back as the top result.


# --- STEP 1: EMBED THE QUESTION ---

vertexai.init(project=settings.gcp_project_id, location=settings.gcp_region)
model = TextEmbeddingModel.from_pretrained(settings.embedding_model)

query_input = TextEmbeddingInput(text=TEST_QUESTION, task_type="RETRIEVAL_QUERY")
# task_type="RETRIEVAL_QUERY" — NOT "RETRIEVAL_DOCUMENT" like
# embedder.py used for storing chunks. This is the exact distinction
# flagged as an interview-worthy detail back in Step 4.7: the same
# embedding model produces a differently-optimized vector depending on
# whether the text is "a document being stored for later search" or
# "a live query searching against stored documents." Getting this
# backwards here would silently degrade search quality without ever
# throwing an error — nothing would crash, results would just be
# subtly worse.

query_embedding = model.get_embeddings([query_input])[0].values
# get_embeddings() takes a list (even for one item) and returns a
# list of results — [0] takes the first (only) result, .values pulls
# out the actual list[float] vector.

query_vector_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
# Same pgvector text-format conversion used in db.py's insert_chunks —
# pg8000 needs the vector as a bracketed, comma-separated string,
# cast to ::vector in the SQL itself.

print(f"Question: {TEST_QUESTION}")
print(f"Query embedding dimension: {len(query_embedding)}\n")


# --- STEP 2: SIMILARITY SEARCH AGAINST STORED CHUNKS ---

with get_connection() as conn:
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            text,
            1 - (embedding <=> %s::vector) AS similarity
        FROM chunks
        ORDER BY embedding <=> %s::vector
        LIMIT 3;
        """,
        (query_vector_str, query_vector_str),
    )
    # This is the actual vector similarity search — the whole point of
    # everything built so far. Breaking down the pgvector-specific
    # parts:
    #
    #   <=>  is pgvector's COSINE DISTANCE operator. It measures how
    #        different two vectors' directions are — 0 means
    #        identical direction (maximally similar), 2 means
    #        completely opposite. This matches the vector_cosine_ops
    #        index we built the HNSW index with in Step 4.8's schema —
    #        using a DIFFERENT distance operator than what the index
    #        was built for would mean the index couldn't be used
    #        efficiently, so this pairing matters.
    #
    #   1 - (embedding <=> %s::vector) AS similarity
    #        converts distance into a more intuitive "similarity"
    #        score for display purposes: 1.0 means identical
    #        direction (perfect match), 0 means unrelated, negative
    #        means opposite. This is just for human readability in
    #        this test script — the actual search below uses the raw
    #        distance operator directly.
    #
    #   ORDER BY embedding <=> %s::vector
    #        sorts every row by its cosine DISTANCE to our query
    #        vector, smallest distance first — meaning the most
    #        similar chunks come first. This is the line that
    #        actually powers "find the chunks most relevant to this
    #        question."
    #
    #   LIMIT 3
    #        we only want the top 3 most relevant chunks — this is
    #        the "k" in "top-k retrieval," a standard RAG parameter.
    #        In the real pipeline this would be a configurable value,
    #        same as chunk_size and chunk_overlap.

    results = cursor.fetchall()
    # fetchall() returns every row from the query as a list of tuples
    # (as opposed to fetchone(), used earlier in db.py for a
    # single-row result).

    print("Top 3 most relevant chunks:\n")
    for i, (text, similarity) in enumerate(results, start=1):
        # enumerate(results, start=1) gives us both the loop index
        # (starting at 1, for human-friendly "Result #1" labeling)
        # and unpacks each row tuple into its two columns (text,
        # similarity) directly.
        print(f"--- Result {i} (similarity: {similarity:.4f}) ---")
        print(text[:300])
        print()
