"""
api.py
------
The FastAPI service exposing DocuMind's agent graph as a regular HTTP
API — the surface a customer's frontend, mobile app, or another
internal service would integrate against (as opposed to mcp_server.py,
which exposes the same graph to OTHER AI/agent systems via MCP).

Both this file and mcp_server.py are deliberately thin wrappers around
the exact same compiled graph from Step 5.6 — neither duplicates the
actual routing/retrieval/synthesis/critic logic.

Run with:  uvicorn documind.api:app --reload
"""

# --- IMPORTS ---

import structlog
# Structured logging, consistent with every other module in this
# project. Unlike mcp_server.py, we do NOT need to redirect this to
# stderr here — that fix was specifically required because MCP's
# stdio transport uses stdout for its JSON-RPC protocol messages.
# FastAPI/uvicorn communicates over HTTP, not stdio, so stdout is
# safe for logging in this file.

from fastapi import FastAPI, HTTPException
# FastAPI: the web framework itself — the `app` object below is an
# instance of this.
# HTTPException: FastAPI's mechanism for returning a proper HTTP error
# response (status code + JSON body) instead of letting an unhandled
# Python exception turn into an opaque 500 with a raw stack trace
# leaking to the client — see the try/except in ask_question() below.

from pydantic import BaseModel
# Used to define this API's request and response "contracts" —
# AskRequest and AskResponse below. FastAPI uses these directly to
# validate incoming requests, generate OpenAPI/Swagger documentation
# automatically, and serialize responses — all without us writing any
# manual validation code.

from starlette.concurrency import run_in_threadpool
# The mechanism that solves the async/sync problem discussed in chat:
# run_in_threadpool takes a SYNCHRONOUS function and its arguments,
# runs it in a background thread (from a thread pool FastAPI/Starlette
# manages), and returns an awaitable — so our synchronous graph.invoke()
# call doesn't block the async event loop while it runs. FastAPI is
# built on Starlette, which is why this utility lives in the starlette
# package even though we're using it from a FastAPI app.

from documind.agents.graph import build_graph
# The compiled LangGraph StateGraph from Step 5.6 — the exact same
# object mcp_server.py uses. One graph, two thin interfaces.

from documind.tracing import configure_tracing

# --- LOGGER SETUP ---
configure_tracing(service_name="documind-api")

logger = structlog.get_logger()


# --- API REQUEST/RESPONSE MODELS ---

class AskRequest(BaseModel):
    """The expected shape of a POST /ask request body."""

    question: str
    # FastAPI will automatically reject any request where "question"
    # is missing or isn't a string, returning a clear 422 validation
    # error — we never have to manually check `if "question" not in
    # body` ourselves.


class AskResponse(BaseModel):
    """The shape of a successful POST /ask response body."""

    answer: str
    is_grounded: bool | None
    # Exposing is_grounded (the critic's verdict) to API consumers is
    # a deliberate choice: a customer's frontend could use this to,
    # for example, show a subtle "unverified" badge on answers the
    # critic wasn't fully confident about, rather than presenting
    # every answer with identical, undifferentiated confidence.


# --- FASTAPI APP INSTANCE ---

app = FastAPI(
    title="DocuMind API",
    description="Enterprise document Q&A knowledge assistant",
    version="0.1.0",
)
# Note: title/description/version aren't just cosmetic — FastAPI uses
# them to auto-generate interactive API documentation, viewable at
# /docs once the server is running. This is a genuinely useful,
# free-with-FastAPI feature worth knowing to demo.


# --- GRAPH INITIALIZATION ---

_graph = build_graph()
# Built once at MODULE load time — meaning once when uvicorn starts
# this app, not once per request. Same "expensive setup happens once"
# principle applied throughout this project (the embedding model, the
# Settings singleton, and identically in mcp_server.py).


# --- HEALTH CHECK ENDPOINT ---

@app.get("/health")
def health_check() -> dict:
    """
    A minimal liveness/readiness endpoint. GKE (and any container
    orchestrator) needs a cheap, fast way to ask "is this pod ready to
    receive traffic?" without exercising the full expensive graph —
    this is exactly that. Kubernetes will call this repeatedly and
    route traffic away from (or restart) a pod that stops responding
    correctly here.
    """

    return {"status": "ok"}
    # Deliberately synchronous (`def`, not `async def`) and trivial —
    # no reason to involve the threadpool machinery for something
    # this cheap; FastAPI runs plain sync endpoints like this directly
    # without blocking the event loop in any meaningful way, since it
    # returns near-instantly.


# --- MAIN ENDPOINT ---

@app.post("/ask", response_model=AskResponse)
async def ask_question(request: AskRequest) -> AskResponse:
    """
    Accepts a question, runs it through the full DocuMind agent graph
    (router -> retriever -> synthesis -> critic, including any retry
    loop), and returns the final grounded answer.
    """

    logger.info("api_request_received", question=request.question)

    try:
        result = await run_in_threadpool(
            _graph.invoke,
            {"question": request.question, "retry_count": 0, "retrieved_chunks": []},
        )
        # This is the actual fix for the async/sync mismatch discussed
        # in chat: run_in_threadpool(_graph.invoke, {...}) schedules
        # our SYNCHRONOUS _graph.invoke(...) call to run in a
        # background thread, and `await`ing it here means THIS
        # endpoint function yields control back to the event loop
        # while that thread does its work — so other incoming
        # requests can still be accepted and start processing during
        # those ~14 seconds, rather than the whole server freezing on
        # one request at a time.
    except Exception as e:
        # A broad except here is a DELIBERATE choice, not sloppiness —
        # this is the outermost boundary of our API, and ANY
        # unexpected failure anywhere in the graph (a GCP API outage,
        # a database connection drop, a malformed LLM response our
        # parsing didn't anticipate) needs to become a clean, safe
        # HTTP error response, not a raw Python stack trace leaked to
        # an external caller — that would both look unprofessional and
        # potentially expose internal implementation details (file
        # paths, library versions) that shouldn't be visible outside
        # our own systems.
        logger.error("graph_execution_failed", question=request.question, error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Something went wrong while processing your question. Please try again.",
        ) from e
        # `from e` preserves the original exception as the "cause" in
        # Python's exception chain — visible in our own server-side
        # logs/stack traces for debugging, while the CLIENT only ever
        # sees the generic, safe "detail" message. This is the right
        # split: full detail for us, minimal detail for the outside
        # world.

    logger.info(
        "api_request_completed",
        question=request.question,
        is_grounded=result.get("is_grounded"),
    )

    return AskResponse(
        answer=result["final_answer"],
        is_grounded=result.get("is_grounded"),
    )
    # FastAPI automatically validates that this returned AskResponse
    # matches the response_model declared on the route decorator above,
    # and serializes it to JSON for the actual HTTP response.
