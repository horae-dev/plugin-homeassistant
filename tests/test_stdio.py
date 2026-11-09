# SPDX-License-Identifier: Apache-2.0
"""The plugin runs as an MCP server over stdio, as the core's plugin host starts it."""

import sys
from typing import Any

import anyio
from horae_sdk.testing import read_result
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from horae_homeassistant import COMMAND, NAME


async def serve_and_call() -> tuple[set[str], Any, Any]:
    # `COMMAND` without its interpreter: run the module with the test's own Python.
    server = StdioServerParameters(command=sys.executable, args=COMMAND[1:], env={})
    async with Client(server, read_timeout_seconds=30) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        identity = read_result(await client.call_tool(f"{NAME}.identity", {"paths": {}}))
        arguments = {
            "paths": {},
            "intent": {"type": "notify.push", "body": "hello"},
            "tool": f"{NAME}.push",
            "params": {"url": "https://ha.example.org"},
        }
        refused = read_result(await client.call_tool(f"{NAME}.apply", arguments))
    return tools, identity.data, refused.data


def test_serves_over_stdio() -> None:
    """The sink operations are listed; a misconfigured entry answers an error object, without
    any network."""
    tools, identity, refused = anyio.run(serve_and_call)
    assert {f"{NAME}.apply", f"{NAME}.identity"} <= tools
    assert identity == {"identity": NAME}
    assert refused["error"]["kind"] == "config_invalid"
    assert "service" in refused["error"]["message"]
