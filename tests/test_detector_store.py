from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import stat
import time

import pytest

from proxy.detector_store import DetectorStore


@pytest.fixture
def store(tmp_path):
    return DetectorStore(tmp_path / "private")


def binding(**changes):
    return {"provider_id": "p1", "model": "chat", "reference_model": "reference-v1",
            "configuration_key": "a" * 64, **changes}


def settings(**changes):
    return {"enabled": True, "interval_minutes": 30, "threshold_percent": 95,
            "targets": [{key: value for key, value in binding().items() if key != "configuration_key"}],
            **changes}


def result(identifier="r1", status="suspect", **changes):
    return {**binding(), "id": identifier, "provider_name": "Primary", "status": status,
            "target_probability": .99 if status == "verified" else .2 if status == "suspect" else None,
            "prediction": "reference-v1", "prediction_probability": .99,
            "checked_at": time.time(), "source": "manual", "error_kind": None,
            "candidates": [{"model": "reference-v1", "probability": .99}],
            "duration_ms": 1200, "reference_version": "v1", **changes}


def state(store, **changes):
    return store.state(**binding(**changes))


def test_defaults_isolated_database_private_wal(store):
    assert store.settings() == {"enabled": False, "interval_minutes": 30,
                                "threshold_percent": 95, "targets": [], "revision": 0}
    assert store.states() == [] and store.latest_results() == [] and store.journal() == []
    assert store.path.name == "detector.db"
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert not (store.path.parent / "proxy.db").exists()
    assert not (store.path.parent / "secret.key").exists()


def test_repeated_states_only_journal_first_conclusive_and_transitions(store):
    store.save_settings(settings(), 0)
    base = time.time() - 10
    for index, status in enumerate(["unknown", "verified", "verified", "error", "suspect", "suspect", "unknown", "verified"]):
        store.record_result(result(f"r{index}", status, checked_at=base + index,
                                   error_kind="timeout" if status == "error" else None))
    assert [(r["from_status"], r["to_status"]) for r in reversed(store.journal())] == [
        ("unknown", "verified"), ("verified", "suspect"), ("suspect", "verified")]
    assert state(store)["status"] == "verified"
    assert state(store)["latest_status"] == "verified"
    assert len(store.latest_results()) == 8
    assert store.blocked_provider_ids([binding()]) == set()
    for event in store.journal():
        assert event["provider_name"] == "Primary"
        assert event["model"] == "chat"
        assert event["checked_at"]


def test_errors_unknown_and_manual_checks_preserve_quarantine(store):
    store.save_settings(settings(), 0)
    base = time.time() - 10
    store.record_result(result("suspect", checked_at=base))
    for index, status in enumerate(["error", "unknown"]):
        store.record_result(result(f"inconclusive{index}", status, checked_at=base + index + 1))
        saved = state(store)
        assert saved["status"] == "suspect"
        assert saved["latest_status"] == status
        assert saved["conclusive_result"]["id"] == "suspect"
        assert store.blocked_provider_ids([binding()]) == {"p1"}
    assert len(store.journal()) == 1
    store.record_result(result("manual-verified", "verified", checked_at=base + 4))
    assert store.blocked_provider_ids([binding()]) == set()
    assert len(store.journal()) == 2


def test_configuration_binding_targets_enable_disable_and_restart(store):
    store.save_settings(settings(), 0)
    store.record_result(result())
    assert store.blocked_provider_ids([binding()]) == {"p1"}
    assert store.blocked_provider_ids([binding(configuration_key="b" * 64)]) == set()
    assert store.blocked_provider_ids([binding(model="other-model")]) == set()
    assert store.blocked_provider_ids([binding(reference_model="other-reference")]) == set()
    assert store.blocked_provider_ids([binding(provider_id="p2")]) == set()
    reopened = DetectorStore(store.path.parent)
    assert reopened.blocked_provider_ids([binding()]) == {"p1"}
    assert state(reopened)["conclusive_result"]["id"] == "r1"
    assert len(reopened.latest_results()) == len(reopened.journal()) == 1
    reopened.save_settings(settings(enabled=False), 1)
    assert reopened.blocked_provider_ids([binding()]) == set()
    reopened.save_settings(settings(), 2)
    assert reopened.blocked_provider_ids([binding()]) == {"p1"}
    reopened.save_settings(settings(targets=[]), 3)
    assert reopened.blocked_provider_ids([binding()]) == set()


def test_one_suspect_model_blocks_entire_provider(store):
    second = {"provider_id": "p1", "model": "other", "reference_model": "reference-v1"}
    store.save_settings(settings(targets=settings()["targets"] + [second]), 0)
    store.record_result(result("ok", "verified"))
    store.record_result(result("bad", model="other"))
    assert store.blocked_provider_ids([binding(), binding(model="other")]) == {"p1"}


@pytest.mark.parametrize("revision", [True, False, 0.0, "0", None])
def test_revision_requires_integer(store, revision):
    with pytest.raises(ValueError, match="invalid_revision"):
        store.save_settings(settings(), revision)
    assert store.settings()["revision"] == 0


def test_revision_conflict_does_not_write_and_concurrent_save_only_one_wins(store):
    def save():
        try:
            return store.save_settings(settings(), 0)["revision"]
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(lambda _: save(), range(4)))
    assert outcomes.count(1) == 1
    assert outcomes.count("revision_conflict") == 3
    assert store.settings()["revision"] == 1


@pytest.mark.parametrize("changes", [
    {"enabled": 1}, {"enabled": "true"}, {"interval_minutes": True},
    {"interval_minutes": 4}, {"interval_minutes": 1441}, {"interval_minutes": 30.0},
    {"threshold_percent": True}, {"threshold_percent": "95"}, {"threshold_percent": 49}, {"threshold_percent": 101}, {"threshold_percent": float('nan')},
    {"targets": "not-list"}, {"targets": [{"provider_id": "bad id", "model": "chat", "reference_model": "ref"}]},
    {"targets": [{"provider_id": "p1", "model": " ", "reference_model": "ref"}]},
    {"targets": [{"provider_id": "p1", "model": "chat", "reference_model": "bad ref"}]},
    {"targets": settings()["targets"] * 2},
])
def test_settings_validation(store, changes):
    with pytest.raises(ValueError):
        store.save_settings(settings(**changes), 0)
    assert store.settings()["revision"] == 0


def test_no_unlisted_secrets_or_content_persisted(store):
    sentinel = "NEVER-PERSIST-SECRET-PROMPT-RESPONSE"
    payload = result(api_key=sentinel, prompt=sentinel, response=sentinel,
                     base_url=sentinel, exception=sentinel)
    payload["candidates"] = [{"model": f"candidate-{i}", "probability": .1, "response": sentinel}
                             for i in range(7)]
    store.record_result(payload)
    saved = store.latest_results()[0]
    assert len(saved["candidates"]) == 5
    assert all(set(candidate) == {"model", "probability"} for candidate in saved["candidates"])
    assert sentinel not in json.dumps(store.states() + store.journal() + store.latest_results())
    assert sentinel.encode() not in b"".join(path.read_bytes() for path in store.path.parent.iterdir() if path.is_file())


@pytest.mark.parametrize("changes", [
    {"provider_id": "bad id"}, {"configuration_key": "not-sha256"},
    {"model": "bad model"}, {"reference_model": ""}, {"model": "x" * 201},
    {"target_probability": float("nan")}, {"target_probability": True},
    {"target_probability": 1.01}, {"prediction_probability": float("inf")},
    {"duration_ms": -1}, {"duration_ms": True}, {"checked_at": True},
    {"error_kind": "upstream response secret"}, {"source": "other"},
])
def test_result_validation_rejects_unsafe_metadata(store, changes):
    with pytest.raises(ValueError):
        store.record_result(result(**changes))
    assert store.states() == [] and store.journal() == []


def test_old_suspect_snapshot_survives_history_retention(store):
    store.save_settings(settings(), 0)
    store.record_result(result("old", checked_at=time.time() - 31 * 86400))
    assert store.latest_results() == [] and store.journal() == []
    assert state(store)["status"] == "suspect"
    assert DetectorStore(store.path.parent).blocked_provider_ids([binding()]) == {"p1"}


def test_idempotent_check_and_late_completion_do_not_create_transitions(store):
    base = time.time() - 10
    check = result("new", checked_at=base + 5)
    store.record_result(check)
    store.record_result(check)
    store.record_result(result("late", "verified", checked_at=base))
    assert state(store)["status"] == "suspect"
    assert len(store.journal()) == 1
    assert {row['id'] for row in store.latest_results()} == {'new','late'}


def test_concurrent_repeated_suspect_checks_journal_once(store):
    base = time.time() - 10
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda i: store.record_result(result(f"r{i}", checked_at=base + i)), range(20)))
    assert state(store)["status"] == "suspect"
    assert len(store.journal()) == 1
    assert state(store)["latest_result"]["id"] == "r19"


def test_result_and_journal_row_caps_do_not_remove_current_suspect(store):
    store.save_settings(settings(), 0)
    store.record_result(result("initial"))
    with sqlite3.connect(store.path) as conn:
        payload = json.dumps(result("seed"))
        rows = [(f"seed-{i}", time.time() - 100, payload) for i in range(10010)]
        conn.executemany("INSERT INTO results VALUES(?,?,?)", rows)
        conn.executemany("INSERT INTO journal(ts,payload) VALUES(?,?)", [(row[1], payload) for row in rows])
    store.record_result(result("prune", "error"))
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM results").fetchone()[0] == 10000
        assert conn.execute("SELECT count(*) FROM journal").fetchone()[0] == 10000
    assert store.blocked_provider_ids([binding()]) == {"p1"}
    assert state(store)["latest_status"] == "error"
    assert state(store)["status"] == "suspect"
    assert len(store.latest_results()) == 50
    assert len(store.journal()) == 100
