import asyncio

from asx_sharequery.mcp_server import _handle_request


def test_mcp_initialize_and_tool_list() -> None:
    async def run() -> None:
        initialized = await _handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        listed = await _handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        return initialized, listed

    initialized, listed = asyncio.run(run())

    assert initialized["result"]["serverInfo"]["name"] == "asx-sharequery"
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert "get_share_research" in names
    assert "get_share_history" in names
    assert "get_share_forecast" in names


def test_mcp_tool_call_returns_tool_content() -> None:
    async def run() -> None:
        return await _handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_share_snapshot", "arguments": {"symbol": "IXR"}},
            }
        )

    response = asyncio.run(run())

    assert response["result"]["isError"] is False
    assert response["result"]["content"][0]["type"] == "text"
    assert "IXR" in response["result"]["content"][0]["text"]
