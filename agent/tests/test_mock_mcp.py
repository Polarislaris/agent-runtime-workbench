"""Tests for the s19 in-process mock MCP teaching implementation."""

from __future__ import annotations

import pytest

from agent.features.mcp import (
    connect_mcp,
    normalize_mcp_name,
    reset_mock_mcp_clients,
)
from agent.tooling.pool import assemble_tool_pool


@pytest.fixture(autouse=True)
def clear_mock_connections():
    """Keep process-local teaching state isolated between tests."""
    reset_mock_mcp_clients()
    yield
    reset_mock_mcp_clients()


def test_mock_mcp_tools_are_absent_until_server_is_connected():
    tools, handlers = assemble_tool_pool()

    assert "connect_mcp" in {tool["name"] for tool in tools}
    assert not any(tool["name"].startswith("mcp__") for tool in tools)
    assert "mcp__docs__search" not in handlers


def test_connecting_docs_discovers_and_dispatches_mock_tools():
    assert connect_mcp("docs") == (
        "Connected to mock MCP server 'docs'. Discovered: search, get_version"
    )

    tools, handlers = assemble_tool_pool()
    tools_by_name = {tool["name"]: tool for tool in tools}

    assert "mcp__docs__search" in tools_by_name
    assert "readOnly" in tools_by_name["mcp__docs__search"]["description"]
    assert handlers["mcp__docs__search"](query="agent loop") == (
        "Mock docs search result for: agent loop"
    )
    assert handlers["mcp__docs__get_version"]() == "Mock docs version: s19-demo"


def test_mock_connection_validation_and_name_normalization():
    assert "unknown server 'missing'" in connect_mcp("missing")
    assert connect_mcp("deploy").startswith("Connected to mock MCP server 'deploy'")
    # The second explicit call verifies that a mock connection is idempotent.
    assert connect_mcp("deploy") == "Mock MCP server 'deploy' already connected"
    assert normalize_mcp_name("docs.server/search") == "docs_server_search"
