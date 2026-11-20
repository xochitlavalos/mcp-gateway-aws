"""A small, dependency-free MCP server core (JSON-RPC 2.0).

Implements the subset of the Model Context Protocol a stateless tools server needs:
initialize, notifications/initialized, ping, tools/list, and tools/call.

Error handling follows the MCP split:
- Protocol problems (bad JSON-RPC, unknown method, unknown tool, invalid arguments)
  return JSON-RPC errors.
- Problems while running a tool (upstream 404, rate limits, no connected credential)
  return a normal result with isError: true, so the model can read and react to them.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from . import __version__
from .common import logger
from .github_client import GitHubClient, GitHubError
from .tools import InvalidArguments, Tool, validate_arguments

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "mcp-gateway-aws", "title": "GitHub (read-only) via MCP gateway", "version": __version__}
INSTRUCTIONS = (
    "Read-only access to GitHub using the caller's own connected token. "
    "If a tool reports that no token is connected, ask the user to connect one via POST /connect."
)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

NOT_CONNECTED_MESSAGE = (
    "No GitHub token is connected for your account. "
    "Connect a fine-grained, read-only token with POST /connect, then retry."
)


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def error_response(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_result(payload: Any, is_error: bool = False) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class McpServer:
    def __init__(self, tools: dict[str, Tool], client_factory: Callable[[], GitHubClient | None]):
        """client_factory is called lazily, only for tools/call, so listing tools never
        touches the credential store or KMS."""
        self._tools = tools
        self._client_factory = client_factory

    def handle(self, message: Any) -> dict | None:
        """Handle one JSON-RPC message. Returns a response dict, or None for notifications."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return error_response(None, INVALID_REQUEST, "expected a single JSON-RPC 2.0 message object")

        has_id = "id" in message
        request_id = message.get("id")
        method = message.get("method")

        if not isinstance(method, str):
            # A response or malformed message; this server never sends requests, so reject.
            return error_response(request_id if has_id else None, INVALID_REQUEST, "missing method")
        if has_id and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
            return error_response(None, INVALID_REQUEST, "id must be a string or integer")

        if not has_id:  # notification: never answered
            return None

        params = message.get("params") or {}
        if not isinstance(params, dict):
            return error_response(request_id, INVALID_PARAMS, "params must be an object")

        try:
            result = self._dispatch(method, params)
        except JsonRpcError as exc:
            return error_response(request_id, exc.code, exc.message)
        except Exception:  # noqa: BLE001
            logger.exception("unhandled error in method %s", method)
            return error_response(request_id, INTERNAL_ERROR, "internal error")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # ------------------------------------------------------------ methods

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [t.definition() for t in self._tools.values()]}
        if method == "tools/call":
            return self._call_tool(params)
        raise JsonRpcError(METHOD_NOT_FOUND, f"method not found: {method}")

    @staticmethod
    def _initialize(params: dict) -> dict:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise JsonRpcError(INVALID_PARAMS, f"unknown tool: {name}")
        try:
            args = validate_arguments(tool.input_schema, params.get("arguments"))
        except InvalidArguments as exc:
            raise JsonRpcError(INVALID_PARAMS, f"invalid arguments for {tool.name}: {exc}") from exc

        client = self._client_factory()
        if client is None:
            return _tool_result(NOT_CONNECTED_MESSAGE, is_error=True)

        try:
            return _tool_result(tool.run(client, args))
        except InvalidArguments as exc:
            raise JsonRpcError(INVALID_PARAMS, f"invalid arguments for {tool.name}: {exc}") from exc
        except GitHubError as exc:
            return _tool_result(exc.message, is_error=True)
