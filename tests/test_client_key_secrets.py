from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import traceback

from cryptography.fernet import Fernet
import pytest

from proxy.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "private")


def _legacy_key(store, token="fp-legacy-token-only-known-outside-the-database", *, enabled=True):
    key_id = "legacy-key-id"
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,?)", (
            key_id, "Legacy", token[:11], hashlib.sha256(token.encode()).hexdigest(),
            "2025-01-01T00:00:00Z", int(enabled), '["chat"]', 7))
    return key_id, token


def _snapshot(store):
    with sqlite3.connect(store.db_path) as conn:
        return (conn.execute("SELECT * FROM api_keys ORDER BY id").fetchall(),
                conn.execute("SELECT * FROM api_key_secrets ORDER BY key_id").fetchall())


def test_new_secret_is_encrypted_at_rest_with_existing_fernet_key(store):
    created = store.create_key("Client", ["chat"])
    token, key_id = created["token"], created["key"]["id"]
    with sqlite3.connect(store.db_path) as conn:
        encrypted = conn.execute("SELECT encrypted_token FROM api_key_secrets WHERE key_id=?",
                                 (key_id,)).fetchone()[0]
    assert encrypted != token
    assert Fernet(store.key_path.read_bytes().strip()).decrypt(encrypted.encode()).decode() == token
    assert all(token.encode() not in path.read_bytes()
               for path in store.data_dir.iterdir() if path.is_file())
    assert Store(store.data_dir).reveal_key(key_id) == token


def test_list_contains_only_metadata_and_reveal_capability(store):
    created = store.create_key("Client", ["chat"])
    legacy_id, _ = _legacy_key(store)
    rows = {row["id"]: row for row in store.list_keys()}
    assert rows[created["key"]["id"]] == {**created["key"], "can_reveal": True}
    assert rows[legacy_id]["can_reveal"] is False
    for row in rows.values():
        assert set(row) == {"id", "name", "prefix", "created_at", "enabled", "models", "requests", "can_reveal"}
    assert created["token"] not in json.dumps(rows)


def test_legacy_keys_and_missing_keys_cannot_be_revealed(store):
    key_id, token = _legacy_key(store)
    before = _snapshot(store)
    assert store.reveal_key(key_id) is None
    assert store.reveal_key("missing") is None
    assert store.authenticate_key(token)["id"] == key_id
    assert _snapshot(Store(store.data_dir)) == before


def test_schema_upgrade_preserves_hash_only_legacy_key(store):
    key_id, token = _legacy_key(store)
    keys_before, _ = _snapshot(store)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP TABLE api_key_secrets")
    reopened = Store(store.data_dir)
    assert _snapshot(reopened) == (keys_before, [])
    assert reopened.reveal_key(key_id) is None
    assert reopened.authenticate_key(token)["id"] == key_id


def test_create_and_auth_metadata_unchanged_reveal_is_read_only(store):
    created = store.create_key("Client", ["chat"])
    token, metadata = created["token"], created["key"]
    assert set(metadata) == {"id", "name", "prefix", "created_at", "enabled", "models", "requests"}
    assert store.authenticate_key(token) == metadata
    assert store.authenticate_key(token + "wrong") is None
    before = _snapshot(store)
    assert store.reveal_key(metadata["id"]) == token
    assert store.reveal_key(metadata["id"]) == token
    assert _snapshot(store) == before
    assert store.authenticate_key(token) == metadata


def test_deleted_key_cascades_secret_and_revokes_auth(store):
    created = store.create_key("Client", [])
    key_id = created["key"]["id"]
    assert store.delete_key(key_id)
    assert not store.delete_key(key_id)
    assert store.reveal_key(key_id) is None
    assert store.authenticate_key(created["token"]) is None
    assert _snapshot(store) == ([], [])


def test_disabled_key_remains_retrievable_for_admin(store):
    created = store.create_key("Client", [])
    key_id, token = created["key"]["id"], created["token"]
    assert store.update_key(key_id, False)
    assert store.authenticate_key(token) is None
    before = _snapshot(store)
    assert store.reveal_key(key_id) == token
    assert store.list_keys()[0]["can_reveal"] is True
    assert _snapshot(store) == before


@pytest.mark.parametrize("enabled", [True, False])
def test_retain_matching_legacy_token_without_changing_key(store, enabled):
    key_id, token = _legacy_key(store, enabled=enabled)
    keys_before, _ = _snapshot(store)
    assert store.retain_key_secret(token) is True
    keys_after, secrets_after = _snapshot(store)
    assert keys_after == keys_before
    assert len(secrets_after) == 1
    assert store.reveal_key(key_id) == token
    assert store.list_keys()[0]["can_reveal"] is True
    assert bool(store.authenticate_key(token)) is enabled
    assert store.retain_key_secret(token) is False
    assert _snapshot(store) == (keys_after, secrets_after)


@pytest.mark.parametrize("token", ["fp-wrong-token", "", "not-a-client-token", None, 123])
def test_retain_wrong_token_is_noop(store, token):
    _legacy_key(store)
    before = _snapshot(store)
    assert store.retain_key_secret(token) is False
    assert _snapshot(store) == before


@pytest.mark.parametrize("corruption", ["invalid", "mismatch", "invalid_utf8"])
def test_corrupt_secret_has_sanitized_error_without_leak_or_auth_change(store, corruption):
    created = store.create_key("Client", [])
    token, key_id = created["token"], created["key"]["id"]
    wrong_token = "fp-another-valid-but-unrelated-secret"
    if corruption == "invalid":
        encrypted = "corrupt-secret-value-DO-NOT-LEAK"
    elif corruption == "mismatch":
        encrypted = store._fernet.encrypt(wrong_token.encode()).decode()
    else:
        encrypted = store._fernet.encrypt(b"\xff\xfe").decode()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE api_key_secrets SET encrypted_token=? WHERE key_id=?", (encrypted, key_id))
    before = _snapshot(store)
    with pytest.raises(ValueError, match="^client_key_secret_unavailable$") as exc:
        store.reveal_key(key_id)
    formatted = "".join(traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__))
    assert token not in formatted
    assert wrong_token not in formatted
    assert encrypted not in formatted
    assert exc.value.__suppress_context__ is True
    assert store.authenticate_key(token) == created["key"]
    assert store.retain_key_secret(token) is False
    assert _snapshot(store) == before


def test_create_rolls_back_key_when_secret_insert_fails(store):
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("""CREATE TRIGGER reject_secret BEFORE INSERT ON api_key_secrets
            BEGIN SELECT RAISE(ABORT, 'test_secret_failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="test_secret_failure"):
        store.create_key("Client", [])
    assert _snapshot(store) == ([], [])


def test_retain_uses_stored_hash_not_token_format(store):
    key_id, token = _legacy_key(store, token="original-token-format")
    assert store.retain_key_secret(token) is True
    assert store.reveal_key(key_id) == token
    # Recovery must not broaden the existing authentication format contract.
    assert store.authenticate_key(token) is None


def test_concurrent_retain_inserts_only_once(store):
    key_id, token = _legacy_key(store)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.retain_key_secret(token), range(4)))
    assert results.count(True) == 1
    assert store.reveal_key(key_id) == token
    assert len(_snapshot(store)[1]) == 1
