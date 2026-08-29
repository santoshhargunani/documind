"""
critic.py
---------
The fourth and final node in the DocuMind agent graph. Checks whether
the synthesis agent's draft_answer is actually grounded in the
retrieved chunks, and decides what happens next:

  - If grounded: promote draft_answer to final_answer. Done.
  - If NOT grounded and retries remain: leave final_answer unset,
    increment retry_count. The graph (wired in Step 5.6) will route
    back to the retriever or synthesis node for another attempt.
  - If NOT grounded and retries are exhausted: return the best
    attempt anyway, but with an honest disclaimer — never silently
    return a distrusted answer as if it were verified.

This is the "self-reflection" pattern the FDE job description calls
out explicitly — an agent checking another agent's work before it
reaches the user, rather than trusting a single LLM call end to end.
"""

# --- IMPORTS ---

import json
# Standard library — same structured-output parsing approach used in
# router.py: ask the LLM for JSON, parse it into a Python dict here,
# rather than relying on a fragile text-parsing heuristic.

import structlog
# Structured logging, consistent with every other module in this
# project.

from google import genai
# Current Vertex AI SDK — same client pattern as every other agent
# module.

from documind.agents.state import AgentState
# Shared graph state (Step 5.1). This node reads state["question"],
# state["draft_answer"], and state["retrieved_chunks"]; writes
# is_grounded, grounding_feedback, retry_count, and sometimes
# final_answer.

from documind.config import get_settings


# --- LOGGER SETUP ---

logger = structlog.get_logger()


# --- CONSTANTS ---

_MAX_RETRIES = 2
# The hard cap on how many synthesis/critic loops a single question
# can go through before we give up and return the best available
# answer with a disclaimer, rather than looping indefinitely. This is
# exactly the safeguard flagged as necessary back in Step 5.1's
# retry_count field design — a critic that can reject forever without
# a limit is a real production hazard (runaway API costs, a request
# that never returns).


# --- CLIENT SETUP ---

def _get_genai_client() -> genai.Client:
    """Same client construction pattern used across every agent module."""
    settings = get_settings()
    return genai.Client(
        vertexai=True,
        project=settings.gcp_project_id,
        location=settings.gcp_region,
    )


# --- CRITIC SYSTEM PROMPT ---

_CRITIC_SYSTEM_PROMPT = """You are a fact-checking component in an enterprise document Q&A \
system. You will be given a QUESTION, the CONTEXT passages that were retrieved to answer \
it, and a proposed ANSWER. Your job is to judge whether the ANSWER is fully supported by \
the CONTEXT — nothing more, nothing less.

Respond with ONLY a JSON object, no other text, in this exact shape:

{"grounded": true | false, "feedback": "<one sentence explaining your judgment>"}

Rules:
- "grounded": true only if every factual claim in the ANSWER can be directly traced to
  something stated in the CONTEXT. An answer that adds plausible-sounding details not
  present in the CONTEXT is NOT grounded, even if those details happen to be true in
  general.
- "grounded": true is also correct if the ANSWER appropriately says the CONTEXT doesn't
  contain enough information — that is a valid, honest answer, not a failure.
- Be strict. When in doubt, mark it false and explain what's unsupported — a false
  negative here just triggers a retry, which is cheap. A false positive lets a
  hallucination reach the user, which is the failure this whole check exists to prevent."""
# That last bullet is a deliberate, explicit statement of WHY the
# critic should be strict, not just an instruction to be strict — LLMs
# follow instructions more reliably when they understand the
# reasoning behind them, and it's also documentation for the next
# engineer (or interviewer) reading this prompt about the intended
# error-asymmetry: false negatives (unnecessary retries) are cheap,
# false positives (approving a hallucination) are the actual risk
# this component exists to catch.


# --- MAIN NODE FUNCTION ---

def critique_answer(state: AgentState) -> dict:
    """
    The LangGraph node function. Judges state["draft_answer"] against
    state["retrieved_chunks"], and decides the outcome of this
    question-answering attempt.
    """

    question = state["question"]
    draft_answer = state["draft_answer"]
    chunks = state["retrieved_chunks"]
    retry_count = state.get("retry_count", 0)
    # .get(..., 0) rather than state["retry_count"] — defensive
    # against the very first graph run, where retry_count may not
    # have been explicitly initialized yet depending on how the graph
    # is invoked. Defaults to 0 (no retries yet) if missing.

    context_block = "\n\n".join(f"[{i}] {c.chunk_text}" for i, c in enumerate(chunks, start=1))
    # Same numbered-passage formatting used in synthesis.py's
    # _format_context — the critic needs to see the SAME context the
    # synthesis agent saw, in order to judge whether the answer
    # actually stuck to it.

    prompt = f"QUESTION: {question}\n\nCONTEXT:\n{context_block}\n\nANSWER: {draft_answer}"

    client = _get_genai_client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=_CRITIC_SYSTEM_PROMPT,
            temperature=0,
            response_mime_type="application/json",
        ),
    )
    # temperature=0, same reasoning as the router: this is a
    # judgment/classification task ("is this grounded, yes or no"),
    # not a creative one — we want consistent, repeatable verdicts on
    # the same input, not stylistic variation.

    try:
        parsed = json.loads(response.text)
        is_grounded = bool(parsed["grounded"])
        feedback = parsed.get("feedback", "")
    except (json.JSONDecodeError, KeyError) as e:
        # Same defensive-fallback pattern as router.py. If the critic
        # itself fails to produce parseable output, we fail toward
        # the SAFER outcome for a fact-checking component: treat the
        # answer as NOT grounded rather than silently approving it.
        # An unnecessary retry (caused by a parse failure) is a minor
        # cost; an unverified answer slipping through as "approved by
        # default" would defeat the entire purpose of this node.
        logger.warning("critic_parse_failed", error=str(e), raw_response=response.text)
        is_grounded = False
        feedback = "Fallback: critic response could not be parsed, treating as ungrounded."

    new_retry_count = retry_count + 1

    logger.info(
        "answer_critiqued",
        question=question,
        is_grounded=is_grounded,
        feedback=feedback,
        retry_count=new_retry_count,
    )

    result: dict = {
        "is_grounded": is_grounded,
        "grounding_feedback": feedback,
        "retry_count": new_retry_count,
    }

    if is_grounded:
        # The answer passed the check — promote it to final_answer.
        # This is the field the API layer (built in a later step)
        # will actually read and return to the user.
        result["final_answer"] = draft_answer
    elif new_retry_count >= _MAX_RETRIES:
        # Not grounded, AND we've exhausted our retry budget. Rather
        # than looping forever or silently returning an unverified
        # answer as if it were trustworthy, we return the best
        # attempt WITH AN EXPLICIT DISCLAIMER. This is a deliberate
        # design choice worth defending in an interview: a system
        # that just quietly gives up and returns nothing is unhelpful;
        # a system that returns a possibly-wrong answer without
        # flagging the uncertainty is actively misleading. Honesty
        # about confidence is the right default for an enterprise
        # tool where wrong-but-confident answers cause real harm.
        result["final_answer"] = (
            f"{draft_answer}\n\n"
            f"(Note: I wasn't able to fully verify this answer against the source "
            f"documents after {new_retry_count} attempts. Please confirm independently.)"
        )
    # else: not grounded, retries remain. final_answer is deliberately
    # left OUT of the returned dict — meaning it stays unset in state.
    # This is what signals to the graph's conditional routing logic
    # (Step 5.6) that another attempt is needed, rather than ending
    # the graph run here.

    return result
