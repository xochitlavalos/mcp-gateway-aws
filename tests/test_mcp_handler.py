import base64
import json
import logging

import pytest

from conftest import make_event
from gateway.mcp_handler import handle
from gateway.mcp_protocol import SUPPORTED_PROTOCOL_VERSIONS

TOKEN = "github_pat_11AAAAAAA0exampletokenvalue"


def rpc(deps, method, params=None, id_=1, sub="user-123"):
    msg = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    resp = handle(make_event("POST /mcp", msg, sub=sub), deps)
    return resp, (json.loads(resp["body"]) if resp.get("body") else None)


def call_tool(deps, name, arguments=None):
    _, out = rpc(deps, "tools/call", {"name": name, "arguments": arguments or {}})
    return out


@pytest.fixture
def connected(deps):
    deps.store.put("user-123", TOKEN, "octocat")
    return deps


# ------------------------------------------------------------ protocol


def test_initialize_handshake(deps):
    resp, out = rpc(deps, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"},
    })
    assert resp["statusCode"] == 200
    assert out["id"] == 1
    assert out["result"]["protocolVersion"] == "2025-06-18"
    assert out["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert out["result"]["serverInfo"]["name"] == "mcp-gateway-aws"


def test_initialize_falls_back_to_latest_supported_version(deps):
    _, out = rpc(deps, "initialize", {"protocolVersion": "1999-01-01"})
    assert out["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[0]


def test_initialized_notification_returns_202(deps):
    resp, out = rpc(deps, "notifications/initialized", id_=None)
    assert resp["statusCode"] == 202
    assert out is None


def test_ping(deps):
    _, out = rpc(deps, "ping", id_="abc")
    assert out == {"jsonrpc": "2.0", "id": "abc", "result": {}}


def test_tools_list_works_without_connection_and_without_kms(deps, kms):
    _, out = rpc(deps, "tools/list")
    names = {t["name"] for t in out["result"]["tools"]}
    assert names == {"list_repos", "get_file", "search_issues", "list_pull_requests"}
    for tool in out["result"]["tools"]:
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["inputSchema"]["type"] == "object"
    assert kms.count("decrypt") == 0


def test_unknown_method(deps):
    _, out = rpc(deps, "resources/list")
    assert out["error"]["code"] == -32601


def test_parse_error(deps):
    resp = handle(make_event("POST /mcp", "{not json"), deps)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == -32700


def test_invalid_request_shapes(deps):
    for msg in [{"id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 1}, {"jsonrpc": "2.0", "id": True, "method": "ping"}]:
        out = json.loads(handle(make_event("POST /mcp", msg), deps)["body"])
        assert out["error"]["code"] == -32600


def test_batch_rejected(deps):
    resp = handle(make_event("POST /mcp", [{"jsonrpc": "2.0", "id": 1, "method": "ping"}]), deps)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == -32600


def test_base64_body(deps):
    raw = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}).encode()
    event = make_event("POST /mcp", base64.b64encode(raw).decode())
    event["isBase64Encoded"] = True
    assert json.loads(handle(event, deps)["body"])["id"] == 7


def test_get_and_delete_not_allowed(deps):
    assert handle(make_event("GET /mcp"), deps)["statusCode"] == 405
    assert handle(make_event("DELETE /mcp"), deps)["statusCode"] == 405


def test_unauthenticated_request_rejected(deps):
    resp, _ = rpc(deps, "ping", sub=None)
    assert resp["statusCode"] == 401


# ------------------------------------------------------------ tools/call


def test_call_without_connected_token_is_tool_error(deps):
    out = call_tool(deps, "list_repos")
    assert out["result"]["isError"] is True
    assert "POST /connect" in out["result"]["content"][0]["text"]


def test_list_repos_happy_path(connected, github, kms):
    github.add("/user/repos", [{"full_name": "octocat/hello", "private": False, "html_url": "https://github.com/octocat/hello"}])
    out = call_tool(connected, "list_repos", {"per_page": 5})

    assert out["result"]["isError"] is False
    data = json.loads(out["result"]["content"][0]["text"])
    assert data[0]["full_name"] == "octocat/hello"

    req = github.requests[-1]
    assert req["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert req["query"]["per_page"] == ["5"]
    assert req["query"]["visibility"] == ["all"]  # default applied
    # decrypted per request, with the caller's encryption context
    assert kms.calls[-1] == ("decrypt", {"KeyId": kms.calls[-1][1]["KeyId"], "EncryptionContext": {"user_sub": "user-123"}})


def test_get_file_decodes_text(connected, github):
    github.add("/repos/octocat/hello/contents/docs/README.md",
               {"type": "file", "encoding": "base64", "size": 5, "sha": "abc",
                "content": base64.b64encode(b"hello").decode()})
    out = call_tool(connected, "get_file", {"owner": "octocat", "repo": "hello", "path": "docs/README.md"})
    assert json.loads(out["result"]["content"][0]["text"])["content"] == "hello"


def test_get_file_directory_listing(connected, github):
    github.add("/repos/octocat/hello/contents/src", [{"name": "app.py", "type": "file", "size": 10}])
    out = call_tool(connected, "get_file", {"owner": "octocat", "repo": "hello", "path": "src"})
    assert json.loads(out["result"]["content"][0]["text"])["type"] == "dir"


def test_search_issues(connected, github):
    github.add("/search/issues", {"total_count": 1, "items": [{"title": "Bug", "number": 3, "pull_request": {}}]})
    out = call_tool(connected, "search_issues", {"query": "repo:octocat/hello is:open"})
    data = json.loads(out["result"]["content"][0]["text"])
    assert data["items"][0]["is_pull_request"] is True
    assert github.requests[-1]["query"]["q"] == ["repo:octocat/hello is:open"]


def test_list_pull_requests(connected, github):
    github.add("/repos/octocat/hello/pulls", [{"number": 9, "title": "Add feature", "head": {"ref": "feat"}}])
    out = call_tool(connected, "list_pull_requests", {"owner": "octocat", "repo": "hello", "state": "all"})
    assert json.loads(out["result"]["content"][0]["text"])[0]["head"] == "feat"


def test_upstream_404_is_tool_error(connected):
    out = call_tool(connected, "list_pull_requests", {"owner": "octocat", "repo": "missing"})
    assert out["result"]["isError"] is True
    assert "Not found" in out["result"]["content"][0]["text"]


def test_upstream_rate_limit_is_tool_error(connected, github):
    github.add("/user/repos", {"message": "API rate limit exceeded"}, status=403,
               headers={"X-RateLimit-Remaining": "0"})
    out = call_tool(connected, "list_repos")
    assert out["result"]["isError"] is True
    assert "rate limit" in out["result"]["content"][0]["text"]


def test_revoked_token_is_tool_error(connected, github):
    github.add("/user/repos", {"message": "Bad credentials"}, status=401)
    out = call_tool(connected, "list_repos")
    assert out["result"]["isError"] is True
    assert "Reconnect" in out["result"]["content"][0]["text"]


@pytest.mark.parametrize("name,args", [
    ("nope", {}),
    ("get_file", {"owner": "octocat", "repo": "hello"}),                       # missing path
    ("get_file", {"owner": "octo cat", "repo": "hello", "path": "a"}),          # bad owner
    ("get_file", {"owner": "octocat", "repo": "hello", "path": "../secrets"}),  # traversal
    ("list_repos", {"per_page": 0}),
    ("list_repos", {"per_page": "10"}),
    ("list_repos", {"visibility": "internal"}),
    ("list_repos", {"unexpected": 1}),
    ("search_issues", {"query": "   "}),
])
def test_invalid_params(connected, github, name, args):
    before = len(github.requests)
    out = call_tool(connected, name, args)
    assert out["error"]["code"] == -32602
    assert len(github.requests) == before  # no upstream call on bad input


def test_unexpected_exception_is_internal_error(connected, github, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(connected, "transport", boom)
    out = call_tool(connected, "list_repos")
    assert out["error"]["code"] == -32603
    assert "kaboom" not in json.dumps(out)


def test_token_never_logged(connected, github, caplog):
    github.add("/user/repos", [])
    with caplog.at_level(logging.DEBUG):
        call_tool(connected, "list_repos")
    assert TOKEN not in caplog.text
