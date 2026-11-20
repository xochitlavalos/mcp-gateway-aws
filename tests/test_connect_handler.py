import json
import logging

from conftest import make_event
from gateway.connect_handler import handle

TOKEN = "github_pat_11AAAAAAA0exampletokenvalue"


def body(resp):
    return json.loads(resp["body"])


def test_connect_validates_and_stores(deps, github, table):
    resp = handle(make_event("POST /connect", {"token": TOKEN}), deps)

    assert resp["statusCode"] == 200
    assert body(resp)["connected"] is True
    assert body(resp)["github_login"] == "octocat"
    assert github.requests[0]["path"] == "/user"
    assert github.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "user-123" in table.items


def test_connect_rejects_token_github_does_not_accept(deps, github, table):
    github.add("/user", {"message": "Bad credentials"}, status=401)
    resp = handle(make_event("POST /connect", {"token": TOKEN}), deps)
    assert resp["statusCode"] == 400
    assert table.items == {}


def test_connect_rejects_classic_token_with_write_scopes(deps, github, table):
    github.add("/user", {"login": "octocat"}, headers={"X-OAuth-Scopes": "repo, read:org"})
    resp = handle(make_event("POST /connect", {"token": "ghp_" + "a" * 36}), deps)
    assert resp["statusCode"] == 400
    assert "repo" in body(resp)["error"]
    assert table.items == {}


def test_connect_accepts_classic_token_with_read_scopes(deps, github):
    github.add("/user", {"login": "octocat"}, headers={"X-OAuth-Scopes": "read:org, read:user"})
    resp = handle(make_event("POST /connect", {"token": "ghp_" + "a" * 36}), deps)
    assert resp["statusCode"] == 200


def test_connect_rejects_malformed_body(deps):
    assert handle(make_event("POST /connect", "not json"), deps)["statusCode"] == 400
    assert handle(make_event("POST /connect", {"token": "has spaces in it, nope"}), deps)["statusCode"] == 400
    assert handle(make_event("POST /connect", {}), deps)["statusCode"] == 400


def test_status_and_delete(deps):
    assert body(handle(make_event("GET /connect/status"), deps)) == {"connected": False}
    handle(make_event("POST /connect", {"token": TOKEN}), deps)
    assert body(handle(make_event("GET /connect/status"), deps))["connected"] is True
    assert body(handle(make_event("DELETE /connect"), deps)) == {"connected": False}
    assert body(handle(make_event("GET /connect/status"), deps)) == {"connected": False}


def test_unauthenticated(deps):
    assert handle(make_event("GET /connect/status", sub=None), deps)["statusCode"] == 401


def test_token_never_logged(deps, caplog):
    with caplog.at_level(logging.DEBUG):
        handle(make_event("POST /connect", {"token": TOKEN}), deps)
    assert TOKEN not in caplog.text
    assert "user-123" not in caplog.text  # only a hashed id is logged
