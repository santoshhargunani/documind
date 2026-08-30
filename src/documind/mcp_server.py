"""
mcp_server.py
-------------
An MCP (Model Context Protocol) server exposing DocuMind's document
Q&A capability as a standardized tool that any MCP-compatible client
(Claude Desktop, another agent framework, an internal chatbot) can
call — without needing to know anything about LangGraph, pgvector, or
how this system works internally.

This is the pattern the FDE role's job description calls out
explicitly: wrapping an internal capability (here, our own RAG
pipeline) as an MCP tool so it can be connected to a customer's other
AI products.

Run with:  python -m documind.mcp_server
"""

# --- IMPORTS ---

import sys

import structlog
# Structured logging, consistent with every other module in this
# project.

from mcp.server.fastmcp import FastMCP
# The classic FastMCP decorator API, bundled inside the official
# mcp SDK's stable v1.x line (pinned in pyproject.toml as
# mcp>=1.28,<2 — see the discussion in chat about why v1.x was
# deliberately chosen over the newer v2 rewrite). FastMCP turns a
# plain, type-hinted Python function into a fully spec-compliant MCP
# tool automatically — you don't hand-write any JSON-RPC or protocol
# handling yourself.

from documind.agents.graph import build_graph
# The compiled LangGraph StateGraph from Step 5.6 — router, retriever,
# synthesis, and critic all wired together as one invokable unit.
# This MCP server doesn't reimplement any of that logic; it's a thin
# protocol wrapper around the exact same graph tested in
# scripts/test_graph.py.
structlog.configure(
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)

# --- LOGGER SETUP ---

logger = structlog.get_logger()


# --- MCP SERVER INSTANCE ---

mcp = FastMCP("documind")
# Creates the MCP server itself, named "documind" — this is the name
# an MCP client would see when it connects and lists available
# servers.


# --- GRAPH INITIALIZATION ---

_graph = build_graph()
# The compiled graph is built ONCE, at module load time — not inside
# the tool function below, which would rebuild it on every single
# call. This mirrors the same "load once, reuse many times" principle
# applied to the embedding model in embedder.py (Step 4.7) and the
# Settings singleton in config.py (Step 4.3) — expensive setup work
# happens once per process, not once per request.


# --- THE MCP TOOL ---

@mcp.tool()
def search_knowledge_base(question: str) -> str:
    """
    Answers a question using DocuMind's enterprise knowledge base.

    Searches ingested documents for relevant information and returns
    a grounded answer with citations to the source material. If the
    knowledge base doesn't contain enough information to answer
    confidently, this will say so explicitly rather than guessing.

    Args:
        question: The question to answer, in natural language.

    Returns:
        A grounded answer, or an explanation if the question is too
        vague or out of scope for this knowledge base.
    """
    # This docstring is NOT just documentation for humans reading the
    # source code — FastMCP reads it directly and uses it (along with
    # the type hints on `question: str` and the return type `-> str`)
    # to automatically generate this tool's schema, which is what an
    # MCP client actually sees when deciding whether and how to call
    # this tool. A vague or missing docstring here would mean an LLM
    # client has a worse understanding of what this tool does and
    # when to use it — this is effectively the "prompt" for a piece
    # of tool-use reasoning happening in someone else's system
    # entirely, so it's worth writing as carefully as the system
    # prompts in router.py, synthesis.py, and critic.py.

    logger.info("mcp_tool_invoked", tool="search_knowledge_base", question=question)

    result = _graph.invoke(
        {"question": question, "retry_count": 0, "retrieved_chunks": []}
    )
    # Invoking the SAME compiled graph tested end-to-end in
    # scripts/test_graph.py. Every piece of logic — routing,
    # retrieval, grounded synthesis, critic verification, the retry
    # loop — runs exactly as it did there. This function's only job
    # is translating between "MCP tool call in, string out" and
    # "AgentState in, AgentState out," which is the whole value of
    # keeping the graph itself decoupled from any particular way of
    # invoking it (a script, an MCP tool, a future FastAPI endpoint
    # in Step 7 — all of them will call the same build_graph() output).

    logger.info(
        "mcp_tool_completed",
        tool="search_knowledge_base",
        question=question,
        answer_length=len(result["final_answer"]),
    )

    return result["final_answer"]
    # MCP tools return plain values (here, a string) — FastMCP handles
    # wrapping this into the correct protocol-level response format.
    # We only ever return the graph's final_answer field, never
    # intermediate state like retrieved_chunks or is_grounded — those
    # are internal implementation details the calling client doesn't
    # need and shouldn't depend on.


# --- ENTRY POINT ---

if __name__ == "__main__":
    mcp.run()
    # Starts the server using stdio transport by default — the
    # standard way an MCP client (like Claude Desktop) launches and
    # communicates with a local MCP server: as a subprocess,
    # communicating over stdin/stdout rather than a network port.
    # This is the right transport for local development and testing.
    # A production deployment (e.g. once this runs inside GKE in a
    # later step) would instead use Streamable HTTP transport, so
    # remote clients can connect over the network rather than
    # spawning a local subprocess — that's a configuration change to
    # this same server, not a rewrite.
