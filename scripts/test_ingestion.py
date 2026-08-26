"""
test_ingestion.py
------------------
Manual integration test script for the DocuMind ingestion pipeline.

This is NOT a pytest unit test (no mocking, no assertions) — it's a
script that exercises all four ingestion steps in sequence against
REAL GCP services, using real output from each step as input to the
next. This is deliberate: it validates that the pieces actually work
together end-to-end, which mocked unit tests (added later in
tests/unit/) wouldn't catch on their own.

Run with:  python scripts\\test_ingestion.py
Requires:  a real PDF at scripts\\sample.pdf, and GCP resources applied
           via terraform apply (see project notes for the reminder to
           refresh DOCAI_PROCESSOR_ID in .env after each terraform apply).
"""

# --- IMPORTS ---

from pathlib import Path
# Standard library. Used for test_file below — pathlib.Path is the
# cross-platform, type-safe way to represent a filesystem path (handles
# Windows "\" vs Unix "/" automatically), and it's what upload_document()
# in storage.py expects as its first argument.

from documind.ingestion.chunker import chunk_text
# The chunking function from Step 4.6 — splits raw extracted text into
# overlapping, retrieval-sized Chunk objects.

from documind.ingestion.embedder import embed_chunks
# The embedding function from Step 4.7 — turns a list of Chunk objects
# into a list of EmbeddedChunk objects (chunk + 768-dimension vector).

from documind.ingestion.parser import parse_document
# The Document AI parsing function from Step 4.5 — takes a gs:// URI
# and returns the extracted plain text.

from documind.ingestion.storage import upload_document
# The GCS upload function from Step 4.5 — takes a local file path and
# uploads it to the raw-docs bucket, returning its gs:// URI.


# --- STEP 1: UPLOAD ---

test_file = Path("scripts/sample.pdf")
# The local PDF we're using as test input. Must exist on disk before
# running this script — any small PDF with real text works.

gcs_uri = upload_document(test_file, "test-uploads/sample.pdf")
# Uploads the local file to Cloud Storage at the path
# test-uploads/sample.pdf within our raw-docs bucket. Returns a string
# like "gs://project-...-documind-raw-docs/test-uploads/sample.pdf" —
# this exact format is what Document AI's GcsDocument input expects.

print(f"Uploaded to: {gcs_uri}")


# --- STEP 2: PARSE ---

text, page_count = parse_document(gcs_uri)
# Sends the uploaded file's GCS URI to Document AI, which runs OCR and
# returns the extracted plain text as a single string. `text` now
# holds the full extracted content of the PDF.

print(f"Extracted {len(text)} characters")
print(text[:500])
# Print only the first 500 characters as a sanity-check preview —
# printing the entire document would flood the terminal for anything
# beyond a short test file.


# --- STEP 3: CHUNK ---

chunks = chunk_text(text)
# Splits `text` (the real, freshly-parsed document text from Step 2)
# into a list of Chunk objects, using the default chunk_size=1000 and
# chunk_overlap=150 from chunker.py's function signature. This is the
# variable that was missing before — it MUST be defined here, above
# any code that uses it, since Python executes top to bottom.

print(f"\nCreated {len(chunks)} chunks")
for chunk in chunks[:3]:
    # chunks[:3] is a Python slice — "the first 3 elements of the
    # list." We only preview a few chunks here rather than printing
    # every one, since a real document could produce dozens.
    print(f"\n--- Chunk {chunk.chunk_index} (chars {chunk.char_start}-{chunk.char_end}) ---")
    print(chunk.text[:200])


# --- STEP 4: EMBED ---

embedded = embed_chunks(chunks)
# Takes the `chunks` list built in Step 3 and sends each one to Vertex
# AI to get back a 768-dimension embedding vector. This is why chunks
# must exist before this line runs — embed_chunks() takes it as its
# only argument.

print(f"\nEmbedded {len(embedded)} chunks")
print(f"First embedding dimension: {len(embedded[0].embedding)}")
# embedded[0] is the first EmbeddedChunk in the list; .embedding is
# its list[float] vector; len(...) tells us how many numbers are in
# it — should print 768 for the text-embedding-005 model.

print(f"First 5 values: {embedded[0].embedding[:5]}")
# Preview just the first 5 numbers of the first chunk's vector, as a
# sanity check that we got real floating-point values back, not an
# error object or empty list.
from documind.db import persist_document

document_id = persist_document(
    gcs_uri=gcs_uri,
    original_filename="sample.pdf",
    page_count= page_count,
    embedded_chunks=embedded,
)
print(f"\nPersisted document: {document_id}")