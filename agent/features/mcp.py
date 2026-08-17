"""Teaching-only mock implementation of the MCP client boundary.

This module deliberately does *not* speak the Model Context Protocol over a
network or start an MCP server process.  It lets the agent exercise the s19
flow--connect, discover tools, assemble a dynamic tool pool, and call a
tool--using ordinary in-process Python functions.  A production MCP client
would replace ``MockMCPClient`` with an implementation backed by a real
transport such as stdio or Streamable HTTP.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any


ToolDefinition = dict[str, Any]
ToolHandler = Callable[..., str]

_DISALLOWED_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


class MockMCPClient:
    """In-memory stand-in for one MCP server used by the s19 lesson.

    ``register`` models MCP's ``tools/list`` response and ``call_tool`` models
    ``tools/call``.  Both are local function calls on purpose: this is a mock,
    not an MCP transport implementation.
    """

    def __init__(self, name: str):
        self.name = name
        self.tools: list[ToolDefinition] = []
        self._handlers: dict[str, ToolHandler] = {}

    def register(
        self,
        tool_definitions: list[ToolDefinition],
        handlers: Mapping[str, ToolHandler],
    ) -> None:
        """Register predeclared tools, simulating discovery through tools/list."""
        self.tools = [dict(tool) for tool in tool_definitions]
        self._handlers = dict(handlers)

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call a local mock handler, simulating MCP tools/call."""
        handler = self._handlers.get(tool_name)
        if handler is None:
            return f"Mock MCP error: unknown tool '{tool_name}' on server '{self.name}'"

        try:
            return str(handler(**arguments))
        except TypeError as error:
            return f"Mock MCP error: invalid input for '{tool_name}': {error}"
        except Exception as error:  # pragma: no cover - defensive adapter boundary
            return f"Mock MCP error: '{tool_name}' failed: {error}"


def normalize_mcp_name(name: str) -> str:
    """Make a server or tool name safe for the mcp__server__tool namespace."""
    return _DISALLOWED_NAME_CHARS.sub("_", name)


def _make_docs_server() -> MockMCPClient:
    """Create a deterministic, read-only mock documentation server."""
    client = MockMCPClient("docs")
    client.register(
        [
            {
                "name": "search",
                "description": "Search the mock documentation (readOnly).",
                "annotations": {"readOnlyHint": True},
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Words to look up in the mock documentation.",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_version",
                "description": "Return the mock documentation version (readOnly).",
                "annotations": {"readOnlyHint": True},
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        ],
        {
            "search": lambda query: f"Mock docs search result for: {query}",
            "get_version": lambda: "Mock docs version: s19-demo",
        },
    )
    return client


def _make_deploy_server() -> MockMCPClient:
    """Create a deterministic mock deployment server; it never deploys anything."""
    client = MockMCPClient("deploy")
    client.register(
        [
            {
                "name": "get_status",
                "description": "Return mock deployment status (readOnly).",
                "annotations": {"readOnlyHint": True},
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            {
                "name": "trigger",
                "description": "Simulate a deployment trigger (destructive).",
                "annotations": {"destructiveHint": True},
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "environment": {
                            "type": "string",
                            "description": "Mock target environment, for example staging.",
                        },
                    },
                    "required": ["environment"],
                    "additionalProperties": False,
                },
            },
        ],
        {
            "get_status": lambda: "Mock deploy status: healthy",
            "trigger": lambda environment: (
                f"Mock deployment triggered for '{environment}' (no real action was taken)"
            ),
        },
    )
    return client


# This explicit registry is the teaching equivalent of an MCP configuration
# file.  Adding a mock server here does not install, start, or contact anything.
MOCK_SERVERS: dict[str, Callable[[], MockMCPClient]] = {
    "docs": _make_docs_server,
    "deploy": _make_deploy_server,
}

# Connection state is intentionally process-local.  The s19 mock has no
# database migration or persistent server configuration.
_mcp_clients: dict[str, MockMCPClient] = {}


def connect_mcp(name: str) -> str:
    """Connect to a preconfigured mock server and simulate tool discovery."""
    name = str(name).strip()
    if not name:
        return "Mock MCP error: server name must not be empty"
    if name in _mcp_clients:
        return f"Mock MCP server '{name}' already connected"

    factory = MOCK_SERVERS.get(name)
    if factory is None:
        available = ", ".join(sorted(MOCK_SERVERS))
        return f"Mock MCP error: unknown server '{name}'. Available: {available}"

    client = factory()
    _mcp_clients[name] = client
    discovered = ", ".join(tool["name"] for tool in client.tools)
    return f"Connected to mock MCP server '{name}'. Discovered: {discovered}"


def connected_mcp_clients() -> Mapping[str, MockMCPClient]:
    """Return the active mock clients for dynamic tool-pool assembly."""
    return _mcp_clients


def mcp_tool_annotations(prefixed_tool_name: str) -> dict[str, Any]:
    """Return declared annotations for an ``mcp__server__tool`` name.

    Metadata is read from the configured mock-server definitions rather than
    only connected clients, keeping permission decisions independent of the
    dynamic tool-pool assembly order.
    """
    if not prefixed_tool_name.startswith("mcp__"):
        return {}

    for server_name, factory in MOCK_SERVERS.items():
        safe_server_name = normalize_mcp_name(server_name)
        for tool in factory().tools:
            expected_name = (
                f"mcp__{safe_server_name}__{normalize_mcp_name(tool['name'])}"
            )
            if prefixed_tool_name == expected_name:
                return dict(tool.get("annotations", {}))
    return {}


def reset_mock_mcp_clients() -> None:
    """Clear process-local mock connections; intended for deterministic tests."""
    _mcp_clients.clear()
