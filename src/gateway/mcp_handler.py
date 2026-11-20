"""Lambda entry point for the MCP Streamable HTTP endpoint (POST /mcp).

Stateless mode: every POST carries one JSON-RPC message and gets a JSON response
(or 202 Accepted for notifications). No sessions and no SSE stream, which fits Lambda.
"""

from __future__ import annotations

from .common import HttpError, json_response, log_event, log_safe_id, parse_json_body, user_sub
from .deps import Deps, default_deps
from .github_client import GitHubClient
from .mcp_protocol import INVALID_REQUEST, PARSE_ERROR, McpServer, error_response
from .tools import TOOLS


def handler(event: dict, context: object) -> dict:
    return handle(event, default_deps())


def handle(event: dict, deps: Deps) -> dict:
    if event.get("routeKey") != "POST /mcp":
        # This server does not offer a standalone SSE stream or session deletion.
        return json_response(405, {"error": "method not allowed"}, headers={"allow": "POST"})

    sub = user_sub(event)
    if sub is None:  # the JWT authorizer should prevent this; fail closed anyway
        return json_response(401, {"error": "unauthenticated"})

    try:
        message = parse_json_body(event)
    except HttpError:
        return json_response(400, error_response(None, PARSE_ERROR, "parse error"))
    if isinstance(message, list):
        return json_response(400, error_response(None, INVALID_REQUEST, "batch requests are not supported"))

    def client_factory() -> GitHubClient | None:
        token = deps.store.get_token(sub)
        return GitHubClient(token, transport=deps.transport) if token else None

    server = McpServer(TOOLS, client_factory)
    response = server.handle(message)

    method = message.get("method") if isinstance(message, dict) else None
    params = message.get("params") if isinstance(message, dict) else None
    tool = params.get("name") if method == "tools/call" and isinstance(params, dict) else None
    log_event(
        "mcp_request",
        user=log_safe_id(sub),
        method=method,
        tool=tool,
        error=(response or {}).get("error", {}).get("code"),
        tool_error=((response or {}).get("result") or {}).get("isError"),
    )

    if response is None:
        return {"statusCode": 202, "headers": {"cache-control": "no-store"}}
    return json_response(200, response)
