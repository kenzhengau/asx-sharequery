#!/usr/bin/env python3
"""Dependency-free MCP stdio server for the ASX public-data tools."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from typing import Any

from asx_sharequery.tools import ASX_TOOLS

_PROTOCOL_VERSION = "2024-11-05"


def _tool_schema(tool: Any) -> dict[str, Any]:
    func = getattr(tool, "func", None)
    if func is None:
        return {"name": tool.name, "description": "", "inputSchema": {"type": "object", "properties": {}}}

    properties: dict[str, Any] = {}
    required: list[str] = []
    signature = inspect.signature(func)
    annotations = getattr(func, "__annotations__", {})

    for parameter in signature.parameters.values():
        if parameter.name == "symbol":
            properties[parameter.name] = {
                "type": "string",
                "description": "ASX ticker code, for example IXR or BHP",
            }
            required.append(parameter.name)
        elif parameter.name == "horizon_days":
            properties[parameter.name] = {
                "type": "integer",
                "minimum": 1,
                "maximum": 252,
                "description": "Forecast horizon in trading days",
            }
        elif parameter.name == "company_name":
            properties[parameter.name] = {"type": "string"}
        elif parameter.name == "sector":
            properties[parameter.name] = {"type": "string"}
        elif parameter.name == "period":
            properties[parameter.name] = {
                "type": "string",
                "enum": ["1mo", "3mo", "6mo", "1y"],
                "description": "Price-history period",
            }

    description = getattr(func, "__doc__", None) or getattr(tool, "description", "")
    return {
        "name": tool.name,
        "description": description.strip().splitlines()[0] if description else "",
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


_TOOL_MAP = {tool.name: tool for tool in ASX_TOOLS}


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def _handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")

    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": message.get("params", {}).get("protocolVersion", _PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "asx-sharequery", "version": "1.0.0"},
            },
        )
    if method == "notifications/initialized" or method == "notifications/cancelled":
        return None
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": [_tool_schema(tool) for tool in ASX_TOOLS]})
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = _TOOL_MAP.get(name)
        if tool is None:
            return _error(request_id, -32602, f"Unknown tool: {name}")

        func = getattr(tool, "func", None)
        if func is None:
            return _error(request_id, -32603, f"Tool is not callable: {name}")

        try:
            if inspect.iscoroutinefunction(func):
                output = await func(**arguments)
            else:
                output = func(**arguments)
            parsed = json.loads(output) if isinstance(output, str) else output
            text = json.dumps(parsed, indent=2, ensure_ascii=False, default=str)
            return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                },
            )

    if request_id is not None:
        return _error(request_id, -32601, f"Method not found: {method}")
    return None


async def main() -> None:
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = await _handle_request(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def cli() -> None:
    """Run the ASX MCP stdio server."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
