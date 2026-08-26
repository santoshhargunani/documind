"""
chunker.py
----------
Splits parsed document text (from Document AI) into overlapping,
retrieval-sized chunks. This is the step between "raw extracted text"
and "text ready to be embedded and stored in pgvector."

Why this file exists (see full explanation given in chat):
- Embedding models have input size limits.
- Smaller, focused chunks produce sharper embeddings, which means more
  precise retrieval when a user asks a question.
- Overlap between chunks prevents a single fact from being split across
  a chunk boundary and becoming unretrievable.
"""

# --- IMPORTS ---

import re
# Python's built-in regular expressions module.
# Used here for _normalize_whitespace(), to find and collapse messy
# whitespace patterns that Document AI's OCR output often contains
# (extra spaces from table/column layouts, excess blank lines from
# page breaks). Part of the standard library — no install needed.

import structlog
# Structured (JSON) logging library, declared as a dependency in
# pyproject.toml back in Step 4.1. Every module that does meaningful
# work gets its own logger instance so log lines in Cloud Logging can
# be traced back to the module/function that emitted them, and
# filtered by field (e.g. total_chunks=12) instead of regex-parsed
# from a plain text message.

from pydantic import BaseModel
# We only import BaseModel (not Field, BaseSettings, etc.) because
# that's the only Pydantic feature this file needs — Chunk is a
# simple data model with no custom validation logic. Importing only
# what's used is both a style convention (ruff's "I" import-sorting
# rule checks for this) and makes the file's purpose obvious at a
# glance: "this file defines a data model."


# --- LOGGER SETUP ---

logger = structlog.get_logger()
# Creates a logger instance bound to this module. Every call like
# logger.info("event_name", key=value, ...) emits a structured JSON
# log line — not a formatted string — so each field (total_chunks,
# chunk_size, etc.) is independently queryable in Cloud Logging.


# --- DATA MODEL ---

class Chunk(BaseModel):
    """
    Represents a single chunk of text extracted from a larger document.

    Using a Pydantic model instead of a plain dict or tuple gives us:
    - Type safety (chunk_index is guaranteed to be an int, etc.)
    - Self-documentation (anyone reading this knows exactly what
      fields a chunk has, without hunting through the code)
    - Free validation if these values ever come from an untrusted
      source (e.g. deserialized from an API request)
    """

    text: str
    # The actual chunk content — the substring of text that will be
    # sent to the embedding model and eventually stored in pgvector.

    chunk_index: int
    # This chunk's position in the sequence of chunks produced from
    # the source document (0, 1, 2, ...). Useful for reconstructing
    # document order later, or debugging "why did chunk 7 retrieve
    # for this query."

    char_start: int
    char_end: int
    # The chunk's exact character offsets within the *original*
    # (whitespace-normalized) source text. This is what enables
    # traceability later — e.g. showing a user "this answer is based
    # on characters 1200-2200 of document X" — and it's what a
    # critic/verification agent (built in a later step) can use to
    # confirm an LLM's answer is actually grounded in real source
    # text, not hallucinated.


# --- HELPER FUNCTION ---

def _normalize_whitespace(text: str) -> str:
    """
    Cleans up irregular whitespace in raw OCR output before chunking.

    Document AI extracts text from a *visual* page layout (columns,
    tables, headers, footers). When that gets flattened into a single
    text string, you get artifacts like multiple spaces between words
    (from column/table layout) and multiple blank lines (from page
    breaks). This function removes that noise so:
      1. Character-counting in chunk_text() reflects real content,
         not incidental formatting.
      2. Chunk boundary detection (word-boundary snapping) works
         predictably instead of tripping over irregular gaps.

    The leading underscore in the function name is a Python convention
    signaling "internal helper, not part of this module's public API"
    — callers outside this file should use chunk_text(), not this.
    """

    text = re.sub(r"[ \t]+", " ", text)
    # r"[ \t]+"  -> matches one-or-more consecutive spaces or tabs.
    # The "r" prefix makes this a *raw string*, so \t is treated as
    # the two characters backslash-t (which regex interprets as "tab
    # character") rather than Python trying to interpret \t itself.
    # Replaces any run of spaces/tabs with a single space.
    # Example: "Enterprise    customers" -> "Enterprise customers"

    text = re.sub(r"\n{3,}", "\n\n", text)
    # r"\n{3,}" -> matches 3 or more consecutive newline characters,
    # i.e. two or more blank lines stacked in a row.
    # Replaces them with exactly "\n\n" (one blank line), which
    # preserves intentional paragraph breaks (a single \n\n doesn't
    # match {3,} so it's left alone) while removing excessive
    # vertical whitespace from page breaks or section dividers.

    return text.strip()
    # Removes leading/trailing whitespace from the whole document —
    # common if the first or last page had header/footer padding
    # that OCR picked up as part of the text.


# --- MAIN CHUNKING FUNCTION ---

def chunk_text(text: str, chunk_size: int = 1000, chunk_overlap: int = 150) -> list[Chunk]:
    """
    Splits `text` into a list of overlapping Chunk objects.

    Parameters:
        text: the full document text (typically Document AI's output).
        chunk_size: target maximum length of each chunk, in characters.
                    Defaults to 1000 — a reasonable starting point,
                    meant to be tuned later against a real eval set
                    (see the interview-question discussion in chat
                    about how chunk size affects retrieval quality).
        chunk_overlap: how many characters the end of one chunk
                       overlaps with the start of the next. Defaults
                       to 150. This overlap exists so that a fact
                       sitting right at a chunk boundary still appears
                       fully within at least one chunk, instead of
                       being split in half and lost.

    Returns:
        A list of Chunk objects, in document order.
    """

    if chunk_overlap >= chunk_size:
        raise ValueError(
            f"chunk_overlap ({chunk_overlap}) must be smaller than chunk_size ({chunk_size})"
        )
    # Guard clause: fail loudly and immediately if the caller passes
    # nonsensical values. If overlap >= size, the sliding window below
    # would never advance forward (or would move backwards), which
    # would either infinite-loop or produce garbage chunks. Better to
    # raise a clear error at the moment of misuse than to debug a
    # hang or corrupted output later.

    text = _normalize_whitespace(text)
    # Clean the input before doing any character-position math, for
    # the reasons explained in _normalize_whitespace's docstring above.

    chunks: list[Chunk] = []
    # The list we'll build up and return. Type-annotated as
    # list[Chunk] so mypy (running in --strict mode, per pyproject.toml)
    # can verify every append() below actually adds a Chunk, not some
    # other type by mistake.

    start = 0
    # The character index where the *next* chunk will begin. This is
    # the "sliding window" position — it moves forward through the
    # text as we go.

    chunk_index = 0
    # A simple counter tracking how many chunks we've created so far,
    # stored on each Chunk so downstream code (and humans debugging)
    # can see chunk order.

    while start < len(text):
        # Keep looping and creating chunks until the sliding window
        # has moved past the end of the text.

        end = min(start + chunk_size, len(text))
        # Take a bite of up to chunk_size characters starting at
        # `start`. min(..., len(text)) caps this at the actual end of
        # the text so we never try to slice past the string's length
        # on the final chunk.

        if end < len(text):
            # Only do boundary-snapping if this isn't already the last
            # chunk (if end == len(text), there's nothing after it to
            # worry about cutting mid-word).

            boundary = text.rfind(" ", start, end)
            # rfind searches *backwards* from `end`, within the
            # range [start, end), for the last space character.
            # Returns -1 if no space is found in that range.

            if boundary > start:
                # Only snap to this boundary if it's a meaningful
                # position (not right at `start`, which would produce
                # a near-empty chunk). If a valid space was found,
                # move the cut point there instead of cutting exactly
                # at chunk_size — this avoids slicing a word in half,
                # e.g. turning "...quarterly rev" into a cleaner
                # "...quarterly" cut.
                end = boundary

        chunk_str = text[start:end].strip()
        # Extract the actual substring for this chunk and strip any
        # leading/trailing whitespace left over from the slice.

        if chunk_str:
            # Only keep non-empty chunks — guards against edge cases
            # (e.g. a chunk that ended up being pure whitespace)
            # producing a useless empty entry in the results.

            chunks.append(
                Chunk(
                    text=chunk_str,
                    chunk_index=chunk_index,
                    char_start=start,
                    char_end=end,
                )
            )
            # Build a Chunk object with all four fields and add it to
            # our results list.

            chunk_index += 1
            # Increment the counter for the next chunk.

        if end >= len(text):
            # We've reached (or passed) the end of the text — no more
            # chunks to create. Exit the loop.
            break

        start = end - chunk_overlap
        # This is the line that actually creates the overlap: instead
        # of starting the next chunk exactly where this one ended
        # (start = end), we step backwards by chunk_overlap characters.
        # That means the last `chunk_overlap` characters of this chunk
        # will also appear at the start of the next chunk — ensuring
        # any fact sitting near the boundary is fully captured in at
        # least one chunk.

    logger.info(
        "chunking_complete",
        total_chunks=len(chunks),
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        source_length=len(text),
    )
    # Structured log line summarizing the result of this call. In
    # Cloud Logging, this lets you spot anomalies at a glance — e.g.
    # a document that produced suspiciously few chunks (possible
    # parsing failure upstream) or an unexpectedly huge number
    # (chunk_size misconfigured for this document type).

    return chunks
    # Hand back the full list of Chunk objects to the caller (e.g.
    # the ingestion pipeline step that will embed and store each one).
