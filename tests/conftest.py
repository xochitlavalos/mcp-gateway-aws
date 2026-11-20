from __future__ import annotations

import json

import pytest

from fakes import FakeGitHub, FakeKms, FakeTable
from gateway.deps import Deps
from gateway.token_store import TokenStore

KEY_ID = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
NOW = 1_800_000_000


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def kms():
    return FakeKms()


@pytest.fixture
def table():
    return FakeTable()


@pytest.fixture
def github():
    gh = FakeGitHub()
    gh.add("/user", {"login": "octocat"})
    return gh


@pytest.fixture
def store(kms, table, clock):
    return TokenStore(kms=kms, table=table, key_id=KEY_ID, ttl_days=90, clock=clock)


@pytest.fixture
def deps(store, github):
    return Deps(store=store, transport=github)


def make_event(route: str, body=None, sub: str | None = "user-123") -> dict:
    event: dict = {"routeKey": route, "requestContext": {}}
    if sub is not None:
        event["requestContext"] = {"authorizer": {"jwt": {"claims": {"sub": sub}}}}
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    return event
