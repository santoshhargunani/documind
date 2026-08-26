"""
state.py
--------
Defines the shared state object that flows through every node in the
DocuMind agent graph (router -> retriever -> synthesis -> critic).

In LangGraph, "state" is the single object every agent node reads from
and writes to. Instead of each function passing its own custom
arguments to the next (which gets unwieldy fast in a branching graph),
every node receives the full current state and returns a partial
update to it. LangGraph merges that update into the state before
routing to whichever node runs next.

This file defines what that shared state actually contains for this
project, and documents the design decisions behind each field.
"""

# --- IMPORTS ---

from typing import Annotated, Literal, TypedDict
# Standard library typing tools.
#   - TypedDict: lets us define a dict with a fixed, known set of keys
#     and types — LangGraph's StateGraph is built around TypedDict
#     (or Pydantic models in newer versions) as the state container.
#     We're using TypedDict here because it's the original, most
#     widely-documented LangGraph pattern, and it keeps state
#     manipulation lightweight — each node just returns a plain dict.
#   - Literal: restricts a field to a fixed set of exact string
#     values (e.g. only "retrieve", "clarify", or "reject" are valid
#     route values) — mypy will flag any other value as a type error.
#   - Annotated: lets us attach extra metadata to a type hint. We use
#     it below to tell LangGraph HOW to merge updates to a specific
#     field (see the reducer explanation on retrieved_chunks).

from operator import add
# Standard library. We use the plain `add` function as a "reducer" —
# see the detailed explanation below at retrieved_chunks. In short:
# it tells LangGraph "when multiple nodes contribute to this field,
# combine them with +  (list concatenation) instead of the default
# behavior of the last write winning."

from pydantic import BaseModel
# Used for RetrievedChunk below — a structured, typed record of a
# single retrieved chunk, kept separate from the TypedDict-based
# top-level state. We nest a Pydantic model inside a TypedDict
# deliberately: TypedDict for the overall graph-state "shape"
# (matches LangGraph's expectations), Pydantic for the actual data
# validation of a meaningful sub-object.


# --- SUPPORTING DATA MODEL ---

class RetrievedChunk(BaseModel):
    """
    A single chunk returned by the retriever agent, along with the
    similarity score it was ranked by. This is intentionally a
    lightweight, retrieval-specific model — NOT the same as the
    Chunk model in chunker.py, which represents a chunk during
    ingestion (with char_start/char_end, no similarity score, no
    document context needed yet).
    """

    document_id: str
    chunk_text: str
    similarity: float
    # Kept alongside the text so the critic agent (Step 5.4) and any
    # future UI can show *why* a chunk was retrieved, and so we can
    # apply a similarity threshold later if we want to filter out
    # weak matches instead of always returning top-k regardless of
    # how relevant they actually are.


# --- MAIN GRAPH STATE ---

class AgentState(TypedDict):
    """
    The complete shared state for one question-answering run through
    the DocuMind agent graph. Every node function will be typed as
    taking an AgentState (or a partial one) and returning a partial
    AgentState update.
    """

    question: str
    # The user's original question, set once at the start of a run
    # and never modified afterward. Every downstream node can refer
    # back to this original wording, even after several graph steps.

    route: Literal["retrieve", "clarify", "reject"] | None
    # Set by the router agent (Step 5.2) — its decision about how to
    # handle this question. `| None` because it doesn't have a value
    # yet before the router runs; Literal restricts it to exactly
    # these three strings once it does. This is what lets later graph
    # logic branch: "if route == 'retrieve', go to the retriever node;
    # if 'reject', skip straight to a canned response," etc.

    retrieved_chunks: Annotated[list[RetrievedChunk], add]
    # The chunks the retriever agent found. Annotated[..., add] is a
    # LangGraph REDUCER — it changes how updates to this field get
    # merged into state.
    #
    # Without a reducer, LangGraph's default behavior is "last write
    # wins" — if a node returns {"retrieved_chunks": [...]}, that
    # value completely REPLACES whatever was there before. That's
    # fine for a single retrieval pass, but if our critic agent later
    # sends the graph back to the retriever for a second attempt
    # (e.g. broader search, different query phrasing), we'd actually
    # want the SECOND batch of chunks appended to the first, not
    # overwriting it — so the synthesis agent has everything gathered
    # across both attempts.
    #
    # `add` as the reducer means: every time a node returns a value
    # for retrieved_chunks, LangGraph combines it with the existing
    # list using Python's `+` operator (list concatenation) instead
    # of replacing it. This is exactly the kind of behavior an
    # interviewer would want to see understood, not just used.

    draft_answer: str | None
    # The synthesis agent's answer, before the critic has verified it.
    # Kept separate from a "final_answer" field (below) deliberately —
    # this state distinction is what makes the critic's job possible:
    # it can compare draft_answer against retrieved_chunks and decide
    # whether to approve it or send the graph back for another pass.

    is_grounded: bool | None
    # The critic agent's verdict: does draft_answer actually appear to
    # be supported by retrieved_chunks, or does it look like the LLM
    # added something not present in the source material (a
    # hallucination)? None before the critic has run.

    grounding_feedback: str | None
    # If is_grounded is False, the critic's explanation of WHY —
    # e.g. "the answer claims a certification date not present in any
    # retrieved chunk." This gets fed back to the synthesis agent (or
    # used to trigger a new retrieval pass) so the retry isn't blind —
    # it has a specific, actionable reason to fix.

    retry_count: int
    # Tracks how many synthesis/critic loops have happened for this
    # question. Critical for preventing an infinite loop: if the
    # critic keeps rejecting the answer, we need a hard stop (e.g.
    # "after 2 retries, return the best attempt with a disclaimer"
    # rather than looping forever burning API calls). This is exactly
    # the kind of production safeguard a naive tutorial
    # implementation would skip and a real system can't.

    final_answer: str | None
    # The answer actually returned to the user — either draft_answer
    # once the critic approves it, or a fallback/disclaimer message
    # if retries are exhausted or the router rejected the question
    # outright. This is the one field the API layer (built in a later
    # step) will actually read to respond to the user.
