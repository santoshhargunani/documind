"""
test_mcp_client.py
-------------------
A minimal MCP client that connects to our own mcp_server.py as a
subprocess (over stdio, same transport MCP Inspector was using) and
calls the search_knowledge_base tool directly — using the official
mcp SDK's client library instead of the Node.js-based Inspector CLI.

This exists because MCP Inspector's --cli mode, when launching a
multi-word Python command ("python -m documind.mcp_server") via npx
on Windows, appears to mis-handle the subprocess arguments — the
server process ends up receiving our JSON-RPC handshake on a bare
Python REPL's stdin instead of our actual script, hence the strange
"NameError: name 'true' is not defined" (JSON's lowercase `true`
isn't valid Python). Writing our own tiny client in pure Python
avoids Node/npx and any of its Windows subprocess quirks entirely,
while still exercising the exact same MCP protocol our server speaks.
"""

# --- IMPORTS ---

import asyncio
# Standard library. The mcp SDK's client APIs are async — connecting
# to a server, sending requests, and awaiting responses all happen
# over asyncio, since MCP communication (even over local stdio) is
# fundamentally a message-passing protocol, not a simple blocking
# function call.

from mcp import ClientSession, StdioServerParameters
# ClientSession: manages the actual MCP protocol conversation once
# connected — listing tools, calling tools, handling responses.
# StdioServerParameters: describes HOW to launch and connect to a
# server over stdio — the command to run and its arguments.

from mcp.client.stdio import stdio_client
# The stdio transport implementation — spawns the server as a
# subprocess and wires up stdin/stdout as the communication channel,
# exactly like MCP Inspector's STDIO transport does, just driven
# directly by our own Python code instead of Inspector's Node.js
# process-spawning logic.

import os

_SERVER_PARAMS = StdioServerParameters(
    command="python",
    args=["-m", "documind.mcp_server"],
    cwd="C:\\Source\\documind",
)
# --- SERVER LAUNCH CONFIGURATION ---

_SERVER_PARAMS = StdioServerParameters(
    command="python",
    args=["-m", "documind.mcp_server"],
)
# Note args is a LIST of separate strings ("-m", "documind.mcp_server")
# rather than one combined string ("-m documind.mcp_server"). This is
# likely the exact distinction that broke under Inspector's npx-based
# spawning — passing args as a proper list here means there's no
# string-splitting ambiguity for the subprocess call to get wrong.


# --- MAIN TEST LOGIC ---

async def main() -> None:
    """
    Connects to our MCP server, lists its tools (to confirm
    search_knowledge_base is registered correctly), then calls it
    with a real test question and prints the result.
    """

    async with stdio_client(_SERVER_PARAMS) as (read_stream, write_stream):
        # stdio_client is an async context manager: entering it
        # spawns our server as a subprocess and returns two streams
        # (read_stream, write_stream) representing the communication
        # channel to it. Exiting the `with` block cleanly terminates
        # the subprocess — same guaranteed-cleanup pattern as
        # db.py's get_connection() context manager from Step 4.8,
        # just for a subprocess instead of a database connection.

        async with ClientSession(read_stream, write_stream) as session:
            # ClientSession wraps the raw streams with the actual MCP
            # protocol logic — request/response matching, message
            # framing, etc. This is also an async context manager;
            # entering it performs the initial MCP handshake with the
            # server automatically.

            await session.initialize()
            # Explicitly completes the MCP protocol's initialization
            # handshake (capability negotiation between client and
            # server) before we can call anything else.

            tools_result = await session.list_tools()
            print("Available tools:")
            for tool in tools_result.tools:
                print(f"  - {tool.name}: {tool.description}")
            print()
            # Confirms search_knowledge_base is registered and shows
            # the exact description FastMCP generated from our
            # function's docstring — a good sanity check that the
            # tool's schema looks the way we expect an MCP client to
            # see it.

            question = "What cloud technologies does this person have experience with?"
            print(f"Calling search_knowledge_base with question: {question}\n")

            result = await session.call_tool(
                "search_knowledge_base",
                arguments={"question": question},
            )
            # The actual tool invocation — name of the tool plus a
            # dict of arguments matching its schema (just "question"
            # here, per our function's signature).

            for content_block in result.content:
                # An MCP tool result can contain multiple content
                # blocks (text, images, etc.) — we only expect one
                # plain text block back, matching our function's
                # `-> str` return type, but we iterate defensively
                # rather than assuming there's exactly one.
                if content_block.type == "text":
                    print(f"Answer:\n{content_block.text}")


if __name__ == "__main__":
    asyncio.run(main())
    # asyncio.run() is the standard entry point for running an async
    # function from a plain synchronous script — it creates an event
    # loop, runs main() to completion, and cleans up afterward.
