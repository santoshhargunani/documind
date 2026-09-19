"""
graph.py
--------
Wires the four agent nodes (router, retriever, synthesis, critic)
into an actual LangGraph StateGraph — a directed graph with branching
logic, as opposed to a fixed linear sequence of function calls.

This is the file that turns everything built in Steps 5.1-5.5 into a
single, invokable unit: hand it a question, get back a final_answer,
with all the routing, retrieval, generation, and verification logic
happening internally.
"""

# --- IMPORTS ---

from langgraph.graph import END, START, StateGraph
# The core LangGraph building blocks:
#   - StateGraph: the graph builder itself — you add nodes and edges
#     to an instance of this, then compile() it into a runnable graph.
#   - START / END: special sentinel node names marking the graph's
#     single entry point and its possible exit point(s). Every graph
#     needs at least one edge from START and at least one path that
#     reaches END.

from documind.agents.critic import critique_answer
from documind.agents.retriever import retrieve_chunks
from documind.agents.router import route_question
from documind.agents.state import AgentState
from documind.agents.synthesis import synthesize_answer
from documind.tracing import tracer
# The four agent node functions built in Steps 5.2-5.5, plus the
# shared AgentState schema from Step 5.1 that the graph is built
# around.

def _traced(name: str, fn):
    """
    Wraps a node function so its execution is captured as a named
    OpenTelemetry span. This is applied uniformly to every node when
    the graph is built, rather than modifying each agent file
    individually — one small addition here instruments the entire
    graph's timing breakdown at once.
    """
    def wrapped(state):
        with tracer.start_as_current_span(name):
            return fn(state)
    return wrapped

# --- SMALL TERMINAL NODES ---
# The router can decide a question needs "clarify" or "reject"
# handling instead of full retrieval. Rather than building special-
# case logic into the router itself, we give each outcome its own
# tiny graph node — consistent with every other node in this graph
# being a single-responsibility function that reads/writes state.

def handle_clarify(state: AgentState) -> dict:
    """
    Reached when the router decides the question is too vague to
    search effectively. Sets a final_answer immediately — this path
    skips retrieval, synthesis, and the critic entirely, since there's
    nothing to search for or verify yet.
    """

    return {
        "final_answer": (
            "Could you clarify your question a bit? I want to make sure I search "
            "for the right information."
        )
    }


def handle_reject(state: AgentState) -> dict:
    """
    Reached when the router decides the question is out of scope for
    an enterprise document assistant. Same pattern as handle_clarify —
    short-circuits straight to a final_answer, no wasted retrieval or
    generation work on a question this system isn't meant to answer.
    """

    return {
        "final_answer": (
            "That's outside what I can help with — I'm built to answer questions "
            "using our internal knowledge base."
        )
    }


# --- CONDITIONAL EDGE FUNCTIONS ---
# These are NOT graph nodes themselves — they don't read/write state
# via a return dict. Instead, LangGraph calls them after a given node
# runs, and their return value (a plain string) tells LangGraph which
# node to go to next. This is how branching logic gets expressed in a
# StateGraph.

def route_after_router(state: AgentState) -> str:
    """
    Called immediately after the router node runs. Reads
    state["route"] (set by route_question in router.py) and returns
    the name of whichever node should run next — this is the router's
    three-way branch (retrieve / clarify / reject) actually taking
    effect in the graph's execution path.
    """

    return state["route"]
    # The string this returns must exactly match a key in the mapping
    # passed to add_conditional_edges below ("retrieve", "clarify",
    # "reject") — LangGraph uses it as a lookup key, not as a node
    # name directly, which is why the mapping dict exists at all (see
    # build_graph() below).


def route_after_critic(state: AgentState) -> str:
    """
    Called immediately after the critic node runs. This is what
    implements the retry loop described in Step 5.5: if the critic
    approved the answer (or exhausted retries and returned a
    disclaimer-appended best attempt), state["final_answer"] will be
    set, and we're done. Otherwise, final_answer is still None,
    meaning the critic rejected the answer and retries remain — send
    the graph back to the retriever for another attempt.
    """

    if state.get("final_answer") is not None:
        return "done"
    return "retry"


# --- GRAPH CONSTRUCTION ---

def build_graph():
    """
    Constructs and compiles the full DocuMind agent graph. Called
    once (e.g. at application startup, in a later FastAPI step) to
    produce a reusable, invokable graph object — not rebuilt on every
    request.
    """

    graph = StateGraph(AgentState)
    # Creates a new graph builder, typed to our AgentState schema.
    # LangGraph uses this type information to know what shape of
    # state every node in this graph will receive and return partial
    # updates to.

    # --- Register every node ---
    graph.add_node("router", _traced("router", route_question))
    graph.add_node("retriever", _traced("retriever", retrieve_chunks))
    graph.add_node("synthesis", _traced("synthesis", synthesize_answer))
    graph.add_node("critic", _traced("critic", critique_answer))
    graph.add_node("clarify", _traced("clarify", handle_clarify))
    graph.add_node("reject", _traced("reject", handle_reject))
    # add_node(name, function) registers each node under a string
    # name — this name is what edges (below) refer to. Note the name
    # ("router") doesn't have to match the function name
    # (route_question) — they're independent; using clear, consistent
    # names for both is a readability choice, not a requirement.

    # --- Entry point ---
    graph.add_edge(START, "router")
    # Every run of this graph begins by calling the router node first
    # — this is non-negotiable in our design: nothing gets retrieved
    # or answered before the router has classified the question.

    # --- Router's three-way branch ---
    graph.add_conditional_edges(
        "router",
        route_after_router,
        {
            "retrieve": "retriever",
            "clarify": "clarify",
            "reject": "reject",
        },
    )
    # add_conditional_edges(from_node, decision_function, mapping):
    # after "router" runs, call route_after_router(state) to get a
    # string back, then look that string up in the mapping dict to
    # find which node to actually go to. This indirection (return a
    # string, then map the string to a node name) is what lets the
    # SAME decision function's return value ("retrieve") differ from
    # the node name it routes to ("retriever") if we ever wanted them
    # to differ — here they're similar by choice, not by necessity.

    # --- Linear path from retrieval through generation ---
    graph.add_edge("retriever", "synthesis")
    graph.add_edge("synthesis", "critic")
    # These are ordinary, unconditional edges (add_edge, not
    # add_conditional_edges) — retrieval ALWAYS leads to synthesis,
    # and synthesis ALWAYS leads to the critic. No branching needed
    # here; the branching happens only at the router and the critic.

    # --- The critic's retry loop ---
    graph.add_conditional_edges(
        "critic",
        route_after_critic,
        {
            "done": END,
            "retry": "retriever",
        },
    )
    # This is the actual retry loop: if route_after_critic returns
    # "retry", the graph goes back to the "retriever" node — NOT back
    # to the router, since we already know this question should be
    # answered via retrieval; we just need another attempt at getting
    # it right. Because retrieved_chunks uses the Annotated[..., add]
    # reducer from Step 5.1, this second retrieval pass's results get
    # APPENDED to the first pass's chunks, so synthesis on the retry
    # has strictly more evidence to work with, not a completely fresh
    # start.

    # --- Terminal nodes lead straight to END ---
    graph.add_edge("clarify", END)
    graph.add_edge("reject", END)
    # The router's "clarify" and "reject" paths never touch retrieval,
    # synthesis, or the critic at all — they set final_answer
    # themselves and go straight to the graph's exit.

    return graph.compile()
    # compile() validates the graph structure (e.g. checks every node
    # is reachable, every conditional edge's possible return values
    # are covered in its mapping) and returns a runnable object. This
    # compiled graph is what actually gets invoked with .invoke() —
    # see the test script below.
