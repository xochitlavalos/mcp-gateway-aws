"""Minimal read-only GitHub REST client built on the standard library.

The client only ever issues GET requests. Even if a user connects a token that can write,
this gateway has no code path that uses that capability.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

API_BASE = "https://api.github.com"
USER_AGENT = "mcp-gateway-aws/0.1"

# transport(url, headers, timeout) -> (status, headers, body)
Transport = Callable[[str, dict[str, str], float], tuple[int, dict[str, str], bytes]]


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def urllib_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 (fixed https base)
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()
    except urllib.error.URLError as exc:
        raise GitHubError(0, f"could not reach GitHub: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GitHubError(0, "GitHub request timed out") from exc


class GitHubClient:
    def __init__(
        self,
        token: str,
        transport: Transport = urllib_transport,
        base_url: str = API_BASE,
        timeout: float = 10.0,
    ):
        self._token = token
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def __repr__(self) -> str:  # never leak the token through repr in tracebacks
        return "GitHubClient(token=***)"

    def get(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, dict[str, str]]:
        url = f"{self._base_url}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        status, resp_headers, body = self._transport(url, headers, self._timeout)
        lower_headers = {k.lower(): v for k, v in resp_headers.items()}
        if 200 <= status < 300:
            try:
                return (json.loads(body) if body else None), lower_headers
            except ValueError as exc:
                raise GitHubError(status, "GitHub returned a non-JSON response") from exc
        raise GitHubError(status, _error_message(status, lower_headers, body))


def _error_message(status: int, headers: dict[str, str], body: bytes) -> str:
    detail = ""
    try:
        detail = json.loads(body).get("message", "")
    except (ValueError, AttributeError):
        pass
    if status == 401:
        return "GitHub rejected the token (expired or revoked). Reconnect with POST /connect."
    if status in (403, 429) and headers.get("x-ratelimit-remaining") == "0":
        return "GitHub rate limit reached. Try again after the reset window."
    if status == 404:
        return "Not found, or the connected token does not have access to this resource."
    return f"GitHub API error {status}: {detail or 'unexpected response'}"
