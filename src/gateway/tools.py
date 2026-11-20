"""Read-only GitHub tools exposed over MCP.

Each tool's JSON Schema is the single source of truth: it is advertised in tools/list
and enforced by validate_arguments() before any upstream call is made.
"""

from __future__ import annotations

import base64
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from .github_client import GitHubClient

MAX_FILE_BYTES = 200_000
NAME_PATTERN = r"^[A-Za-z0-9_.-]{1,100}$"


class InvalidArguments(Exception):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    run: Callable[[GitHubClient, dict], Any]

    def definition(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"readOnlyHint": True, "openWorldHint": True},
        }


# ---------------------------------------------------------------- validation


def validate_arguments(schema: dict, args: Any) -> dict:
    """Validate a flat object against the subset of JSON Schema used by these tools.

    Returns a new dict with defaults applied. Raises InvalidArguments on any violation.
    """
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise InvalidArguments("arguments must be an object")
    props: dict = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            raise InvalidArguments(f"missing required argument: {key}")
    if schema.get("additionalProperties") is False:
        extra = sorted(set(args) - set(props))
        if extra:
            raise InvalidArguments(f"unexpected argument(s): {', '.join(extra)}")

    out: dict[str, Any] = {}
    for key, spec in props.items():
        if key not in args:
            if "default" in spec:
                out[key] = spec["default"]
            continue
        value = args[key]
        kind = spec.get("type")
        if kind == "string":
            if not isinstance(value, str):
                raise InvalidArguments(f"{key} must be a string")
            if len(value) > spec.get("maxLength", 10_000):
                raise InvalidArguments(f"{key} is too long")
            if "pattern" in spec and not re.search(spec["pattern"], value):
                raise InvalidArguments(f"{key} has an invalid format")
        elif kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidArguments(f"{key} must be an integer")
            if value < spec.get("minimum", value) or value > spec.get("maximum", value):
                raise InvalidArguments(
                    f"{key} must be between {spec.get('minimum')} and {spec.get('maximum')}"
                )
        if "enum" in spec and value not in spec["enum"]:
            raise InvalidArguments(f"{key} must be one of: {', '.join(map(str, spec['enum']))}")
        out[key] = value
    return out


def _per_page(default: int = 30) -> dict:
    return {"type": "integer", "minimum": 1, "maximum": 100, "default": default,
            "description": "Results per page (1-100)."}


def _owner_repo() -> dict:
    return {
        "owner": {"type": "string", "pattern": NAME_PATTERN, "description": "Repository owner (user or org)."},
        "repo": {"type": "string", "pattern": NAME_PATTERN, "description": "Repository name."},
    }


# ---------------------------------------------------------------- tool bodies


def _list_repos(gh: GitHubClient, args: dict) -> Any:
    data, _ = gh.get(
        "/user/repos",
        {"visibility": args["visibility"], "sort": "updated", "per_page": args["per_page"]},
    )
    return [
        {
            "full_name": r.get("full_name"),
            "private": r.get("private"),
            "description": r.get("description"),
            "default_branch": r.get("default_branch"),
            "updated_at": r.get("updated_at"),
            "url": r.get("html_url"),
        }
        for r in data or []
    ]


def _get_file(gh: GitHubClient, args: dict) -> Any:
    path = args["path"].strip("/")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise InvalidArguments("path must be a relative path without '.' or '..' segments")
    quoted = urllib.parse.quote(path, safe="/")
    data, _ = gh.get(f"/repos/{args['owner']}/{args['repo']}/contents/{quoted}", {"ref": args.get("ref")})

    if isinstance(data, list):  # a directory
        return {"type": "dir", "path": path,
                "entries": [{"name": e.get("name"), "type": e.get("type"), "size": e.get("size")} for e in data]}

    size = int(data.get("size", 0))
    if data.get("encoding") != "base64" or size > MAX_FILE_BYTES:
        return {"type": "file", "path": path, "size": size,
                "note": f"File not returned inline (over {MAX_FILE_BYTES} bytes or not base64 encoded).",
                "url": data.get("html_url")}
    raw = base64.b64decode(data.get("content", ""))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"type": "file", "path": path, "size": size, "note": "Binary file; content omitted."}
    return {"type": "file", "path": path, "size": size, "sha": data.get("sha"), "content": text}


def _search_issues(gh: GitHubClient, args: dict) -> Any:
    data, _ = gh.get("/search/issues", {"q": args["query"], "per_page": args["per_page"]})
    return {
        "total_count": data.get("total_count"),
        "items": [
            {
                "title": i.get("title"),
                "number": i.get("number"),
                "state": i.get("state"),
                "is_pull_request": "pull_request" in i,
                "author": (i.get("user") or {}).get("login"),
                "url": i.get("html_url"),
                "updated_at": i.get("updated_at"),
            }
            for i in data.get("items", [])
        ],
    }


def _list_pull_requests(gh: GitHubClient, args: dict) -> Any:
    data, _ = gh.get(
        f"/repos/{args['owner']}/{args['repo']}/pulls",
        {"state": args["state"], "per_page": args["per_page"]},
    )
    return [
        {
            "number": p.get("number"),
            "title": p.get("title"),
            "state": p.get("state"),
            "draft": p.get("draft"),
            "author": (p.get("user") or {}).get("login"),
            "head": (p.get("head") or {}).get("ref"),
            "base": (p.get("base") or {}).get("ref"),
            "url": p.get("html_url"),
        }
        for p in data or []
    ]


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            name="list_repos",
            title="List repositories",
            description="List repositories the connected GitHub user can access, most recently updated first.",
            input_schema={
                "type": "object",
                "properties": {
                    "visibility": {"type": "string", "enum": ["all", "public", "private"], "default": "all"},
                    "per_page": _per_page(),
                },
                "additionalProperties": False,
            },
            run=_list_repos,
        ),
        Tool(
            name="get_file",
            title="Get file or directory",
            description="Read a text file (up to 200 KB) or list a directory in a repository.",
            input_schema={
                "type": "object",
                "properties": {
                    **_owner_repo(),
                    "path": {"type": "string", "maxLength": 1000, "description": "Path inside the repository."},
                    "ref": {"type": "string", "maxLength": 255, "description": "Branch, tag, or commit SHA."},
                },
                "required": ["owner", "repo", "path"],
                "additionalProperties": False,
            },
            run=_get_file,
        ),
        Tool(
            name="search_issues",
            title="Search issues and pull requests",
            description="Search issues and pull requests with GitHub search syntax, e.g. 'repo:octo/app is:open label:bug'.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "maxLength": 256, "pattern": r"\S", "description": "GitHub search query."},
                    "per_page": _per_page(20),
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            run=_search_issues,
        ),
        Tool(
            name="list_pull_requests",
            title="List pull requests",
            description="List pull requests in a repository.",
            input_schema={
                "type": "object",
                "properties": {
                    **_owner_repo(),
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                    "per_page": _per_page(),
                },
                "required": ["owner", "repo"],
                "additionalProperties": False,
            },
            run=_list_pull_requests,
        ),
    ]
}
