"""Per-user credential storage: KMS envelope (with encryption context) + DynamoDB.

Each ciphertext is bound to its owner through the KMS encryption context
{"user_sub": <sub>}. A ciphertext copied onto another user's row cannot be decrypted,
because KMS requires the exact same context on decrypt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

CONTEXT_KEY = "user_sub"


@dataclass(frozen=True)
class Connection:
    github_login: str
    connected_at: int
    expires_at: int


class TokenStore:
    def __init__(
        self,
        kms: Any,
        table: Any,
        key_id: str,
        ttl_days: int = 90,
        clock: Callable[[], float] = time.time,
    ):
        self._kms = kms
        self._table = table
        self._key_id = key_id
        self._ttl_seconds = ttl_days * 86400
        self._clock = clock

    @staticmethod
    def _context(sub: str) -> dict[str, str]:
        return {CONTEXT_KEY: sub}

    def put(self, sub: str, token: str, github_login: str) -> Connection:
        encrypted = self._kms.encrypt(
            KeyId=self._key_id,
            Plaintext=token.encode("utf-8"),
            EncryptionContext=self._context(sub),
        )
        now = int(self._clock())
        conn = Connection(github_login=github_login, connected_at=now, expires_at=now + self._ttl_seconds)
        self._table.put_item(
            Item={
                "user_sub": sub,
                "ciphertext": encrypted["CiphertextBlob"],
                "github_login": conn.github_login,
                "connected_at": conn.connected_at,
                "expires_at": conn.expires_at,  # DynamoDB TTL attribute
            }
        )
        return conn

    def _item(self, sub: str) -> dict | None:
        item = self._table.get_item(Key={"user_sub": sub}).get("Item")
        if not item:
            return None
        # DynamoDB TTL deletes lazily, so treat expired rows as absent.
        if int(item.get("expires_at", 0)) <= int(self._clock()):
            return None
        return item

    def status(self, sub: str) -> Connection | None:
        item = self._item(sub)
        if item is None:
            return None
        return Connection(
            github_login=str(item["github_login"]),
            connected_at=int(item["connected_at"]),
            expires_at=int(item["expires_at"]),
        )

    def get_token(self, sub: str) -> str | None:
        item = self._item(sub)
        if item is None:
            return None
        blob = item["ciphertext"]
        blob = getattr(blob, "value", blob)  # boto3 returns Binary wrappers
        decrypted = self._kms.decrypt(
            CiphertextBlob=bytes(blob),
            KeyId=self._key_id,
            EncryptionContext=self._context(sub),
        )
        return decrypted["Plaintext"].decode("utf-8")

    def delete(self, sub: str) -> None:
        self._table.delete_item(Key={"user_sub": sub})
