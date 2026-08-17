"""Build the Lead agent's dynamic tool pool.

The MCP portion is intentionally backed by ``MockMCPClient`` for the s19
lesson.  Replacing that client with a real MCP transport should not require
changing the agent loop or its tool naming convention.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..features.mcp import connected_mcp_clients, normalize_mcp_name
from .handlers import BUILTIN_HANDLERS
from .schemas import BUILTIN_TOOLS


def assemble_tool_pool() -> tuple[list[dict[str, Any]], dict[str, Callable[..., str]]]:
    """Return builtin tools plus tools discovered from connected mock MCP servers."""
    tools = [dict(tool) for tool in BUILTIN_TOOLS]
    handlers = dict(BUILTIN_HANDLERS)

    for server_name, client in connected_mcp_clients().items():
        safe_server_name = normalize_mcp_name(server_name)
        for tool in client.tools:
            source_name = tool["name"]
            safe_tool_name = normalize_mcp_name(source_name)
            prefixed_name = f"mcp__{safe_server_name}__{safe_tool_name}"

            if prefixed_name in handlers:
                # The mock registry is static, but protect the agent from an
                # ambiguous normalized name if a later lesson adds more servers.
                continue

            dynamic_tool = dict(tool)
            dynamic_tool["name"] = prefixed_name
            tools.append(dynamic_tool)
            handlers[prefixed_name] = (
                lambda *, mock_client=client, original_name=source_name, **arguments:
                mock_client.call_tool(original_name, arguments)
            )

    return tools, handlers
