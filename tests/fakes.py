"""In-memory stand-ins for KMS, DynamoDB, and the GitHub HTTP API."""

from __future__ import annotations

import json
import uuid
from urllib.parse import parse_qs, urlparse


class FakeKmsError(Exception):
    """Mirrors KMS InvalidCiphertextException when the encryption context does not match."""


class FakeKms:
    def __init__(self):
        self._blobs: dict[bytes, tuple[bytes, dict, str]] = {}
        self.calls: list[tuple[str, dict]] = []

    def encrypt(self, KeyId, Plaintext, EncryptionContext):  # noqa: N803 (boto3 casing)
        self.calls.append(("encrypt", {"KeyId": KeyId, "EncryptionContext": EncryptionContext}))
        blob = b"ciphertext:" + uuid.uuid4().bytes
        self._blobs[blob] = (Plaintext, dict(EncryptionContext), KeyId)
        return {"CiphertextBlob": blob, "KeyId": KeyId}

    def decrypt(self, CiphertextBlob, EncryptionContext=None, KeyId=None):  # noqa: N803
        self.calls.append(("decrypt", {"KeyId": KeyId, "EncryptionContext": EncryptionContext}))
        plaintext, ctx, key = self._blobs[CiphertextBlob]
        if ctx != (EncryptionContext or {}) or (KeyId and KeyId != key):
            raise FakeKmsError("InvalidCiphertextException")
        return {"Plaintext": plaintext, "KeyId": key}

    def count(self, op: str) -> int:
        return sum(1 for name, _ in self.calls if name == op)


class FakeTable:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def put_item(self, Item):  # noqa: N803
        self.items[Item["user_sub"]] = dict(Item)

    def get_item(self, Key):  # noqa: N803
        item = self.items.get(Key["user_sub"])
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key):  # noqa: N803
        self.items.pop(Key["user_sub"], None)


class FakeGitHub:
    """A transport that serves canned responses keyed by URL path."""

    def __init__(self):
        self.routes: dict[str, tuple[int, dict, object]] = {}
        self.requests: list[dict] = []

    def add(self, path: str, body: object, status: int = 200, headers: dict | None = None):
        self.routes[path] = (status, headers or {}, body)

    def __call__(self, url: str, headers: dict, timeout: float):
        parsed = urlparse(url)
        self.requests.append({"path": parsed.path, "query": parse_qs(parsed.query), "headers": headers})
        if parsed.path not in self.routes:
            return 404, {}, json.dumps({"message": "Not Found"}).encode()
        status, resp_headers, body = self.routes[parsed.path]
        return status, resp_headers, json.dumps(body).encode()
