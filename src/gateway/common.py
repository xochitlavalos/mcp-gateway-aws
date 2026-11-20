"""Shared helpers for API Gateway (HTTP API, payload v2) Lambda handlers."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger("gateway")
logger.setLevel(logging.INFO)


class HttpError(Exception):
    """An error that maps directly to an HTTP response."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def json_response(status: int, body: Any = None, headers: dict[str, str] | None = None) -> dict:
    out_headers = {"content-type": "application/json", "cache-control": "no-store"}
    if headers:
        out_headers.update(headers)
    resp: dict[str, Any] = {"statusCode": status, "headers": out_headers}
    if body is not None:
        resp["body"] = json.dumps(body)
    return resp


def raw_body(event: dict) -> str:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return body


def parse_json_body(event: dict) -> Any:
    try:
        return json.loads(raw_body(event))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HttpError(400, "request body must be valid JSON") from exc


def user_sub(event: dict) -> str | None:
    """Return the verified JWT subject placed in the event by the HTTP API JWT authorizer."""
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    sub = claims.get("sub")
    return sub if isinstance(sub, str) and sub else None


def log_safe_id(sub: str) -> str:
    """A short, non-reversible identifier for correlating logs without storing the raw subject."""
    return hashlib.sha256(sub.encode("utf-8")).hexdigest()[:12]


def log_event(message: str, **fields: Any) -> None:
    """Structured log line. Callers must never pass tokens or request bodies."""
    logger.info(json.dumps({"msg": message, **fields}, default=str))
