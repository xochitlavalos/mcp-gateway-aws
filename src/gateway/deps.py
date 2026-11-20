"""Runtime dependency wiring. Kept separate so tests can inject fakes and never import boto3."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from .github_client import Transport, urllib_transport
from .token_store import TokenStore


@dataclass
class Deps:
    store: TokenStore
    transport: Transport = urllib_transport


@lru_cache(maxsize=1)
def default_deps() -> Deps:
    import boto3  # available in the Lambda Python runtime

    store = TokenStore(
        kms=boto3.client("kms"),
        table=boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"]),
        key_id=os.environ["KEY_ID"],
        ttl_days=int(os.environ.get("TOKEN_TTL_DAYS", "90")),
    )
    return Deps(store=store)
