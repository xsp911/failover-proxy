from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import stat
import threading
import time

import pytest

from proxy.store import DEFAULT_SETTINGS, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "private")


def _config(store, key="provider-key-very-secret-123456"):
    config = store.get_config()
    config["providers"] = [{"id": "p1", "name": "Primary", "priority": 1,
                            "enabled": True, "api_key": key, "base_url": "https://example.org/v1"}]
    config["models"] = [{"id": "chat", "arbitrary": {"validated": True}}]
    return config


def _all_bytes(path):
    return b"".join(p.read_bytes() for p in path.iterdir() if p.is_file())


def test_defaults_private_permissions_and_encrypted_persistence(store):
    assert store.get_config() == {"revision": 0, "settings": DEFAULT_SETTINGS, "providers": [], "models": []}
    config = _config(store)
    result = store.save_config(config, 0)
    assert result["revision"] == 1
    assert result["providers"][0]["has_api_key"] is True
    assert "api_key" not in result["providers"][0]
    assert "_api_key_encrypted" not in result["providers"][0]
    assert config["providers"][0]["api_key"] == "provider-key-very-secret-123456"
    assert b"provider-key-very-secret-123456" not in _all_bytes(store.data_dir)
    assert stat.S_IMODE(store.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.key_path.stat().st_mode) == 0o600
    secret_key = store.key_path.read_bytes()
    reopened = Store(store.data_dir)
    assert reopened.key_path.read_bytes() == secret_key
    assert reopened.get_config(True)["providers"][0]["api_key"] == "provider-key-very-secret-123456"
    assert reopened.get_config()["models"] == config["models"]
    for path in store.data_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_blank_key_retains_secret_and_revision_is_optimistic(store):
    saved = store.save_config(_config(store, "first-secret"), 0)
    saved["providers"][0]["api_key"] = "   "
    saved["providers"][0]["_api_key_encrypted"] = "attacker-controlled"
    changed = store.save_config(saved, 1)
    assert changed["revision"] == 2
    assert store.get_config(True)["providers"][0]["api_key"] == "first-secret"
    with pytest.raises(ValueError, match="^revision_conflict$"):
        store.save_config(saved, 1)
    assert store.get_config()["revision"] == 2
    changed["providers"][0]["api_key"] = "  replacement-secret  "
    store.save_config(changed, 2)
    assert store.get_config(True)["providers"][0]["api_key"] == "replacement-secret"


def test_admin_hash_and_initialize_once(store):
    store.initialize_admin("admin", "correct-horse-battery-staple")
    store.initialize_admin("replaced", "another-password")
    assert store.authenticate_admin("admin", "correct-horse-battery-staple")
    assert not store.authenticate_admin("replaced", "another-password")
    assert not store.authenticate_admin("admin", "incorrect")
    assert not store.authenticate_admin("other", "correct-horse-battery-staple")
    assert b"correct-horse-battery-staple" not in _all_bytes(store.data_dir)
    reopened = Store(store.data_dir)
    assert reopened.authenticate_admin("admin", "correct-horse-battery-staple")


def test_api_keys_hashed_revocation_counter_and_sessions(store):
    created = store.create_key("Test client", ["chat"])
    token, metadata = created["token"], created["key"]
    assert token.startswith("fp-") and len(token) >= 50
    assert metadata["models"] == ["chat"] and metadata["requests"] == 0
    assert "token_hash" not in metadata and "token" not in metadata
    assert store.authenticate_key(token) == metadata
    assert store.authenticate_key(token + "x") is None
    assert token.encode() not in _all_bytes(store.data_dir)
    store.increment_key_requests(metadata["id"])
    assert store.authenticate_key(token)["requests"] == 1
    assert store.update_key(metadata["id"], False)
    assert store.authenticate_key(token) is None
    assert store.list_keys()[0]["enabled"] is False
    assert store.update_key(metadata["id"], True)
    assert store.authenticate_key(token) is not None
    assert store.delete_key(metadata["id"])
    assert not store.delete_key(metadata["id"])
    assert store.authenticate_key(token) is None
    cookie, csrf = store.create_session("admin")
    assert store.get_session(cookie) == {"username": "admin", "csrf_token": csrf}
    assert cookie.encode() not in _all_bytes(store.data_dir)
    assert store.get_session("invalid") is None
    assert Store(store.data_dir).get_session(cookie)["csrf_token"] == csrf
    store.delete_session(cookie)
    assert store.get_session(cookie) is None
    store.session_ttl_seconds = -1
    expired, _ = store.create_session("admin")
    assert store.get_session(expired) is None


def test_metrics_null_usage_switches_and_safe_fields(store):
    store.save_config(_config(store), 0)
    now = time.time() - 10
    common = {"request_id": "r1", "provider_id": "p1", "ts": now,
              "prompt": "NEVER-PERSIST-THIS-PROMPT", "api_key": "NEVER-PERSIST-THIS-KEY",
              "response": "NEVER-PERSIST-THIS-RESPONSE"}
    store.record_attempt({**common, "id": "a1", "ordinal": 1, "status": "error",
                          "http_status": 503, "error_kind": "upstream_error", "latency_ms": 100})
    store.record_attempt({**common, "id": "a2", "ordinal": 2, "status": "success", "latency_ms": 300,
                          "ttft_ms": 50, "generation_ms": 200, "input_tokens": 10, "output_tokens": 20,
                          "cache_read_tokens": 4, "cache_write_tokens": 2})
    store.record_request({"id": "r1", "time": now, "model": "chat", "status": "success", "attempts": 2,
                          "provider_id": "p1", "total_ms": 410, "client_ttft_ms": 160,
                          "input_tokens": 10, "output_tokens": 20, "prompt": "NEVER-PERSIST-THIS-PROMPT"})
    store.record_request({"id": "r2", "time": now, "model": "chat", "status": "error", "attempts": 0,
                          "provider_id": None, "total_ms": 1})
    result = store.stats("hour")
    summary = result["summary"]
    assert summary["requests"] == 2
    assert summary["success"] == 1 and summary["errors"] == 1 and summary["switches"] == 1
    assert summary["final_requests"] == 2 and summary["final_errors"] == 1
    assert summary["final_success"] == 1 and summary["final_success_rate"] == 50
    assert summary["input_tokens"] == 10 and summary["output_tokens"] == 20
    assert summary["total_tokens"] == 30
    assert summary["cache_read_tokens"] == 4 and summary["cache_write_tokens"] == 2
    assert summary["success_rate"] == 50
    assert summary["avg_latency_ms"] == 200
    assert summary["avg_ttft_ms"] == 50 and summary["avg_client_ttft_ms"] == 160
    assert summary["avg_tps"] == 100
    assert summary["p50_ms"] == 200 and summary["p95_ms"] == 290
    assert result["providers"][0]["name"] == "Primary"
    assert result["providers"][0]["requests"] == 2
    assert sum(b["requests"] for b in result["timeline"]) == 2
    assert sum(b["switches"] for b in result["timeline"]) == 1
    assert result["recent_errors"][0]["provider"] == "Primary"
    assert result["recent_errors"][0]["kind"] == "upstream_error"
    assert next(r for r in result["recent_requests"] if r["id"] == "r2")["input_tokens"] is None
    assert result["coverage"]["with_usage"] == 1
    assert b"NEVER-PERSIST" not in _all_bytes(store.data_dir)
    with sqlite3.connect(store.db_path) as conn:
        row = json.loads(conn.execute("SELECT payload FROM attempts WHERE id='a1'").fetchone()[0])
    assert row["input_tokens"] is None and row["output_tokens"] is None


def test_period_window_and_ordinal_inference_and_weighted_tps(store):
    now = time.time() - 1
    store.record_attempt({"id": "old", "request_id": "old", "ts": now - 31 * 86400,
                          "status": "error", "latency_ms": 1000})
    for i, output, generation in [(1, 20, 1000), (2, 60, 3000)]:
        store.record_attempt({"id": f"a{i}", "request_id": "r1", "ts": now,
                              "provider_id": "p1", "status": "success", "output_tokens": output,
                              "generation_ms": generation})
    # Replacing the same attempt must not increase its inferred ordinal.
    store.record_attempt({"id": "a2", "request_id": "r1", "ts": now, "provider_id": "p1",
                          "status": "success", "output_tokens": 60, "generation_ms": 3000})
    stats = store.stats("month")
    assert stats["summary"]["requests"] == 2
    assert stats["summary"]["switches"] == 1
    assert stats["summary"]["avg_tps"] == 20
    assert stats["summary"]["avg_ttft_ms"] is None
    assert stats["summary"]["avg_client_ttft_ms"] is None
    assert stats["summary"]["avg_latency_ms"] is None
    assert stats["summary"]["p95_ms"] is None
    assert stats["providers"][0]["enabled"] is False
    for period in ["hour", "day", "week", "month", "all"]:
        assert store.stats(period)["summary"]["requests"] == 2
    with pytest.raises(ValueError, match="invalid_period"):
        store.stats("year")


def test_concurrent_calls_and_optimistic_revision(store):
    key = store.create_key("Concurrent", [])
    def increment(_):
        store.increment_key_requests(key["key"]["id"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(increment, range(60)))
    assert store.authenticate_key(key["token"])["requests"] == 60
    config = _config(store)
    def save(_):
        try:
            store.save_config(config, 0)
            return True
        except ValueError as exc:
            assert str(exc) == "revision_conflict"
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save, range(4)))
    assert sum(results) == 1
    assert store.get_config()["revision"] == 1


def test_concurrent_initializers_share_a_persistent_key(tmp_path):
    directory = tmp_path / "concurrent"
    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(pool.map(lambda _: Store(directory), range(8)))
    stores[0].save_config(_config(stores[0]), 0)
    assert all(s.get_config(True)["providers"][0]["api_key"] == "provider-key-very-secret-123456"
               for s in stores)


def test_repeated_concurrent_schema_initialization(tmp_path):
    # A barrier aligns journal-mode/schema initialization attempts on each fresh
    # database. Repeat across directories to exercise the WAL transition itself.
    workers = 12
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index in range(12):
            directory = tmp_path / f"startup-{index}"
            barrier = threading.Barrier(workers)
            def initialize(_):
                barrier.wait(timeout=10)
                instance = Store(directory)
                instance.record_request({"id": str(threading.get_ident()), "status": "success"})
                return instance.get_config()["revision"]
            assert list(pool.map(initialize, range(workers))) == [0] * workers
            instance = Store(directory)
            assert instance.stats("day")["summary"]["final_requests"] == workers
            with sqlite3.connect(instance.db_path) as conn:
                assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_error_kind_does_not_persist_raw_exception_text(store):
    store.record_attempt({"request_id": "r1", "status": "error",
                          "error_kind": "Authorization: Bearer secret-token-leaked; response body"})
    assert store.stats("day")["recent_errors"][0]["kind"] == "upstream_error"
    assert b"secret-token-leaked" not in _all_bytes(store.data_dir)


def test_hourly_retention_is_durable_and_prunes_both_tables(store, monkeypatch):
    import proxy.store as module
    now = time.time()
    monkeypatch.setattr(module.time, "time", lambda: now)
    expired_ts = now - 31 * 86400
    store.record_attempt({"id": "old-attempt", "ts": expired_ts, "status": "error"})
    store.record_request({"id": "old-request", "time": expired_ts, "status": "error"})
    # Expensive age cleanup is not repeated before the hourly deadline.
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
        last = conn.execute("SELECT last_pruned_at FROM metrics_maintenance").fetchone()[0]
    reopened = Store(store.data_dir)
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT last_pruned_at FROM metrics_maintenance").fetchone()[0] == last
    monkeypatch.setattr(module.time, "time", lambda: now + 3601)
    reopened.record_attempt({"id": "fresh", "ts": now + 3601, "status": "success"})
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT id FROM attempts").fetchall() == [("fresh",)]
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        state = conn.execute("SELECT request_rows,attempt_rows FROM metrics_maintenance").fetchone()
        assert state == (0, 1)


def test_hard_row_cap_eviction_and_upsert_counters(store, monkeypatch):
    import proxy.store as module
    monkeypatch.setattr(module, "METRICS_MAX_ROWS", 5)
    monkeypatch.setattr(module, "METRICS_EVICTION_BATCH", 2)
    now = time.time() - 100
    for index in range(8):
        for _ in range(2):  # Upserts must not inflate maintenance counts.
            store.record_attempt({"id": f"a{index}", "ts": now + index, "status": "success"})
            store.record_request({"id": f"r{index}", "time": now + index, "status": "success"})
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT id FROM attempts ORDER BY ts").fetchall() == [(f"a{i}",) for i in range(4, 8)]
        assert conn.execute("SELECT id FROM requests ORDER BY ts").fetchall() == [(f"r{i}",) for i in range(4, 8)]
        assert conn.execute("SELECT request_rows,attempt_rows FROM metrics_maintenance").fetchone() == (4, 4)
    reopened = Store(store.data_dir)
    assert reopened.stats("month")["summary"]["requests"] == 4
    assert reopened.stats("month")["coverage"]["max_rows_per_table"] == 5


def test_sampled_percentiles_leave_sql_totals_and_averages_exact(store, monkeypatch):
    import proxy.store as module
    monkeypatch.setattr(module, "STATS_PERCENTILE_SAMPLE_LIMIT", 2)
    store.save_config(_config(store), 0)
    now = time.time() - 100
    for index in range(6):
        store.record_attempt({"id": f"a{index}", "request_id": "r1", "provider_id": "p1",
                              "ts": now + index, "ordinal": index + 1,
                              "status": "error" if index == 0 else "success",
                              "latency_ms": 100 * index, "ttft_ms": 10 * index,
                              "input_tokens": 10, "output_tokens": 20, "generation_ms": 1000})
        store.record_request({"id": f"r{index}", "time": now + index, "status": "success",
                              "client_ttft_ms": 100 * index})
    stats = store.stats("day")
    summary = stats["summary"]
    assert summary["requests"] == 6 and summary["final_requests"] == 6
    assert summary["success"] == 5 and summary["errors"] == 1 and summary["switches"] == 5
    assert summary["input_tokens"] == 60 and summary["output_tokens"] == 120
    assert summary["avg_latency_ms"] == 250 and summary["avg_ttft_ms"] == 25
    assert summary["avg_client_ttft_ms"] == 250 and summary["avg_tps"] == 20
    assert summary["p50_ms"] == 450 and summary["p95_ms"] == 495
    assert stats["providers"][0]["requests"] == 6
    assert stats["providers"][0]["avg_latency_ms"] == 250
    assert sum(b["requests"] for b in stats["timeline"]) == 6
    assert stats["coverage"]["with_usage"] == 6
    assert stats["coverage"]["percentiles_sampled"] is True
    assert stats["coverage"]["percentile_sample_size"] == 2
    assert stats["coverage"]["percentile_population"] == 6
    assert stats["recent_errors"][0]["request_id"] == "r1"  # Outside percentile sample.
    assert "последним известным" in stats["coverage"]["note"]


def test_stats_recent_payloads_limited_and_configured_providers_always_exact(store, monkeypatch):
    import proxy.store as module
    monkeypatch.setattr(module, "STATS_PROVIDER_LIMIT", 1)
    store.save_config(_config(store), 0)
    now = time.time() - 100
    for index in range(40):
        store.record_attempt({"id": f"a{index}", "provider_id": "removed", "ts": now + index, "status": "error"})
        store.record_request({"id": f"r{index}", "time": now + index, "status": "error"})
    store.record_attempt({"id": "configured", "provider_id": "p1", "ts": now, "status": "success"})
    stats = store.stats("day")
    assert stats["summary"]["requests"] == 41
    assert len(stats["recent_requests"]) == 30 and len(stats["recent_errors"]) == 30
    assert next(p for p in stats["providers"] if p["id"] == "p1")["requests"] == 1
    assert stats["coverage"]["providers_truncated"] is True


def test_empty_stats_and_corrupt_key_fail_closed(store):
    summary = store.stats("day")["summary"]
    assert summary["requests"] == 0 and summary["success_rate"] is None
    assert summary["final_success"] == 0 and summary["final_success_rate"] is None
    assert summary["avg_tps"] is None and summary["total_tokens"] == 0
    with store.key_path.open("wb") as output:
        output.write(b"bad-key")
    with pytest.raises(ValueError):
        Store(store.data_dir)
