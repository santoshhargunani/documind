"""
synthesis.py
------------
The third node in the DocuMind agent graph. Takes the question and
the chunks the retriever found, and generates an actual answer using
Gemini — grounded strictly in the retrieved evidence, not the model's
own general knowledge.

This is the node most people picture when they think "RAG," but
notice how much has to happen BEFORE it (routing, retrieval) and
AFTER it (the critic, Step 5.5, verifying the answer is actually
trustworthy) for this step to be reliable rather than just plausible-
sounding.
"""

# --- IMPORTS ---

import structlog
# Structured logging, consistent with every other module in this
# project.

from google import genai
# Current Vertex AI SDK — same client pattern as router.py and
# retriever.py.

from documind.agents.state import AgentState, RetrievedChunk
# Shared graph state (Step 5.1). This node reads state["question"]
# and state["retrieved_chunks"], and writes state["draft_answer"].

from documind.config import get_settings


# --- LOGGER SETUP ---

logger = structlog.get_logger()


# --- CLIENT SETUP ---

def _get_genai_client() -> genai.Client:
    """
    Same client construction pattern used in router.py and
    retriever.py — one small function repeated across agent modules
    rather than factored into a shared helper. Worth naming as a
    conscious tradeoff: a shared `_clients.py` utility would reduce
    duplication, but at this project's current size, keeping each
    agent file self-contained and independently readable is a
    reasonable simplicity/DRY tradeoff. Worth revisiting if a fourth
    or fifth agent needing this repeats the pattern again.
    """

    settings = get_settings()
    return genai.Client(
        vertexai=True,
        project=settings.gcp_project_id,
        location=settings.gcp_region,
    )


# --- SYSTEM PROMPT ---

_SYNTHESIS_SYSTEM_PROMPT = """You are an enterprise document assistant. Answer the user's \
question using ONLY the information in the provided context below. Follow these rules \
strictly:

1. If the context contains the answer, respond clearly and concisely, citing which \
   context passage(s) you used (e.g. "According to context [2]...").
2. If the context does NOT contain enough information to answer the question, say so \
   explicitly. Do NOT use your own general knowledge to fill gaps — an unsupported \
   answer is worse than no answer in this system.
3. Do not speculate or make assumptions beyond what the context states."""
# This system prompt is the single most important piece of prompt
# engineering in this whole agent graph. Rule 2 in particular is what
# makes the critic agent's job (Step 5.5) tractable — we're
# instructing the model up front to prefer admitting ignorance over
# hallucinating, rather than relying on the critic to catch every
# fabrication after the fact. Defense in depth: a good prompt reduces
# how often bad answers happen; the critic catches what still slips
# through.


# --- CONTEXT FORMATTING HELPER ---

def _format_context(chunks: list[RetrievedChunk]) -> str:
    """
    Turns the list of RetrievedChunk objects into a single numbered
    text block to embed in the prompt sent to Gemini. Numbering each
    passage is what lets the model (and the system prompt's Rule 1)
    refer back to "context [2]" in its answer — giving us a citation
    trail from answer back to source, which is exactly the kind of
    traceability we built char_start/char_end for during ingestion,
    now paying off at the generation step.
    """

    return "\n\n".join(
        f"[{i}] {chunk.chunk_text}" for i, chunk in enumerate(chunks, start=1)
    )
    # enumerate(chunks, start=1) numbers passages starting at 1 (not
    # 0) since "[0] ..." would read oddly to a human or to the model
    # being asked to cite it back. "\n\n".join(...) puts a blank line
    # between each numbered passage, so Gemini clearly sees them as
    # separate, distinct pieces of evidence rather than one run-on
    # block of text.


# --- MAIN NODE FUNCTION ---

def synthesize_answer(state: AgentState) -> dict:
    """
    The LangGraph node function. Reads state["question"] and
    state["retrieved_chunks"], calls Gemini with strict grounding
    instructions, and returns a partial state update setting
    draft_answer.

    Called "draft" deliberately (not "answer" or "final_answer") —
    this output has not yet been checked by the critic agent
    (Step 5.5). It's a candidate answer, not a guaranteed-correct one.
    """

    question = state["question"]
    chunks = state["retrieved_chunks"]

    if not chunks:
        # Defensive guard: if the retriever found nothing at all (e.g.
        # a very sparse knowledge base, or a genuinely unanswerable
        # question that still got routed to "retrieve"), don't even
        # call the LLM — there's no context to synthesize from, and
        # asking Gemini to answer with an empty context block risks
        # it falling back on general knowledge despite our system
        # prompt's instructions. Short-circuiting here is both cheaper
        # (no wasted API call) and more reliable than hoping the
        # prompt alone prevents an ungrounded answer.
        logger.warning("synthesis_skipped_no_chunks", question=question)
        return {"draft_answer": "I don't have enough information in the knowledge base to answer that question."}

    context_block = _format_context(chunks)

    client = _get_genai_client()

    prompt = f"Context:\n{context_block}\n\nQuestion: {question}"
    # A simple, explicit prompt structure: context first, question
    # second. This ordering matters somewhat for how models attend to
    # long inputs — putting the question last, right before the model
    # starts generating, keeps it fresh in context.

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=_SYNTHESIS_SYSTEM_PROMPT,
            temperature=0.2,
        ),
    )
    # Contrast with the router's temperature=0: synthesis gets a
    # small amount of randomness (0.2) since generating a natural,
    # well-phrased answer benefits from slight variation, whereas
    # classification (the router's job) benefits from being perfectly
    # deterministic. Still low enough to stay close to the grounded,
    # literal answer rather than drifting into creative territory.
    #
    # Using gemini-2.5-flash here too, not a larger/more expensive
    # model — Flash is capable enough for grounded extraction-style
    # answering from provided context, which is a lighter task than
    # open-ended reasoning. We'd reach for a larger model if synthesis
    # quality testing (Step 8's eval pipeline) showed Flash falling
    # short on more complex, multi-chunk reasoning questions.

    draft_answer = response.text

    logger.info(
        "answer_synthesized",
        question=question,
        chunk_count=len(chunks),
        answer_length=len(draft_answer),
    )

    return {"draft_answer": draft_answer}
    # Only returns draft_answer — consistent with every other node's
    # partial-update pattern. The critic agent (next) is responsible
    # for turning this into a final_answer.
