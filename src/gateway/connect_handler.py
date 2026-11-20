"""Lambda entry point for credential management.

POST   /connect          store (or replace) the caller's GitHub token
GET    /connect/status   show whether a token is connected, without decrypting it
DELETE /connect          remove the caller's token
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .common import HttpError, json_response, log_event, log_safe_id, parse_json_body, user_sub
from .deps import Deps, default_deps
from .github_client import GitHubClient, GitHubError
from .token_store import Connection

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_]{20,255}$")

# Classic personal access tokens report their scopes; fine-grained tokens do not.
# Reject classic tokens that could write, so connected credentials stay read-only.
WRITE_SCOPES = {"repo", "public_repo", "delete_repo", "workflow", "write:org", "admin:org",
                "write:packages", "admin:repo_hook", "write:discussion", "gist"}


def handler(event: dict, context: object) -> dict:
    return handle(event, default_deps())


def handle(event: dict, deps: Deps) -> dict:
    sub = user_sub(event)
    if sub is None:
        return json_response(401, {"error": "unauthenticated"})

    route = event.get("routeKey")
    try:
        if route == "POST /connect":
            return _connect(event, sub, deps)
        if route == "GET /connect/status":
            return _status(sub, deps)
        if route == "DELETE /connect":
            deps.store.delete(sub)
            log_event("token_revoked", user=log_safe_id(sub))
            return json_response(200, {"connected": False})
        return json_response(404, {"error": "not found"})
    except HttpError as exc:
        return json_response(exc.status, {"error": exc.message})


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _connection_body(conn: Connection | None) -> dict:
    if conn is None:
        return {"connected": False}
    return {"connected": True, "github_login": conn.github_login,
            "connected_at": _iso(conn.connected_at), "expires_at": _iso(conn.expires_at)}


def _status(sub: str, deps: Deps) -> dict:
    return json_response(200, _connection_body(deps.store.status(sub)))


def _connect(event: dict, sub: str, deps: Deps) -> dict:
    body = parse_json_body(event)
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not TOKEN_PATTERN.match(token):
        raise HttpError(400, "body must be {\"token\": \"<GitHub personal access token>\"}")

    try:
        user, headers = GitHubClient(token, transport=deps.transport).get("/user")
    except GitHubError as exc:
        if exc.status == 401:
            raise HttpError(400, "GitHub rejected the token") from exc
        raise HttpError(502, "could not validate the token with GitHub") from exc

    scopes_header = headers.get("x-oauth-scopes")
    if scopes_header is not None:
        scopes = {s.strip() for s in scopes_header.split(",") if s.strip()}
        risky = sorted((scopes & WRITE_SCOPES) | {s for s in scopes if s.startswith(("write:", "admin:"))})
        if risky:
            raise HttpError(
                400,
                "token has write scopes (" + ", ".join(risky) + "). "
                "Use a fine-grained token with read-only repository permissions.",
            )

    login = str((user or {}).get("login", ""))
    conn = deps.store.put(sub, token, login)
    log_event("token_connected", user=log_safe_id(sub), github_login=login)
    return json_response(200, _connection_body(conn))
