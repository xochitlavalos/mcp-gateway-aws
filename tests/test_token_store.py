import pytest

from conftest import KEY_ID
from fakes import FakeKmsError


def test_round_trip_uses_user_encryption_context(store, kms, table):
    store.put("alice", "github_pat_secret_value_123", "alice-gh")

    assert table.items["alice"]["ciphertext"] != b"github_pat_secret_value_123"
    assert b"github_pat" not in table.items["alice"]["ciphertext"]
    assert store.get_token("alice") == "github_pat_secret_value_123"

    ops = {name: args for name, args in kms.calls}
    assert ops["encrypt"]["EncryptionContext"] == {"user_sub": "alice"}
    assert ops["decrypt"]["EncryptionContext"] == {"user_sub": "alice"}
    assert ops["encrypt"]["KeyId"] == KEY_ID


def test_ciphertext_moved_to_another_user_cannot_be_decrypted(store, table):
    store.put("alice", "github_pat_secret_value_123", "alice-gh")
    stolen = dict(table.items["alice"], user_sub="mallory")
    table.items["mallory"] = stolen

    with pytest.raises(FakeKmsError):
        store.get_token("mallory")


def test_expired_rows_are_treated_as_absent(store, clock):
    store.put("alice", "github_pat_secret_value_123", "alice-gh")
    clock.now += 91 * 86400
    assert store.get_token("alice") is None
    assert store.status("alice") is None


def test_status_never_decrypts(store, kms):
    store.put("alice", "github_pat_secret_value_123", "alice-gh")
    conn = store.status("alice")
    assert conn.github_login == "alice-gh"
    assert kms.count("decrypt") == 0


def test_delete(store):
    store.put("alice", "github_pat_secret_value_123", "alice-gh")
    store.delete("alice")
    assert store.get_token("alice") is None
