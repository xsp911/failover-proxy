"""Embedded Node scoring: original numerical parity, no identity shortcuts."""

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
DETECTOR = Path(os.environ.get('FP_TEST_DETECTOR_DIR',ROOT / 'detector'))
NODE = shutil.which("node")
COMMIT = "4dcb4216151cbd5066d515297720130a9fd3b198"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node runtime required for embedded detector")


@pytest.fixture(scope="module")
def reference():
    return json.loads((DETECTOR / "fixtures/reference_samples.json").read_text(encoding='utf-8'))


@pytest.fixture(scope="module")
def golden():
    return json.loads((DETECTOR / "fixtures/original_expected.json").read_text(encoding='utf-8'))


def run_score(value, *, runner=None, cwd=None):
    completed = subprocess.run(
        [NODE, "--max-old-space-size=384", str(runner or DETECTOR / "score.mjs")],
        input=value if isinstance(value, str) else json.dumps(value),
        capture_output=True, text=True, encoding="utf-8", timeout=15, cwd=cwd,
    )
    assert completed.stdout.count("\n") == 1
    result = json.loads(completed.stdout)
    assert set(result) == {
        "decision", "prediction", "prediction_name", "probability", "probability_status",
        "target_probability", "results", "used_outputs", "diagnostics", "reference_version", "warning",
    }
    return completed, result


@pytest.fixture(scope="module")
def scored(reference):
    completed, result = run_score(reference)
    assert completed.returncode == 0
    assert completed.stderr == ""
    return result


def test_original_module_exact_numeric_parity(scored, golden):
    assert scored["decision"] == "ranked"
    for key in ["prediction", "probability", "probability_status", "target_probability",
                "results", "used_outputs", "diagnostics"]:
        assert scored[key] == golden[key]
    assert scored["target_probability"] == 0.9999930549015217
    assert scored["reference_version"] == COMMIT
    assert scored["probability_status"] == "reference_calibrated"
    assert all(0 <= row["probability"] <= 1 and math.isfinite(row["score"])
               for row in scored["results"])
    assert math.isclose(sum(row["probability"] for row in scored["results"]), 1, abs_tol=1e-14)
    assert "not proof of model identity" in scored["warning"]


def test_catalog_exact_ids_original_prompts_and_thresholds(reference, scored):
    catalog = json.loads((DETECTOR / "catalog.json").read_text(encoding='utf-8'))
    assert catalog["source_commit"] == catalog["reference_version"] == COMMIT
    assert len(catalog["models"]) == 63 == len(scored["results"])
    assert {'gemini-3.5-flash-lite','kimi-k3.1-pro','minimax-m3.1-flash'} <= {m['id'] for m in catalog['models']}
    ids = {row["id"] for row in catalog["models"]}
    assert {"deepseek-v4.1-flash-1017", "deepseek-v4.1-flash"} <= ids
    assert ids == {row["model"] for row in scored["results"]}
    assert [row["expected_count"] for row in catalog["challenges"]] == [218, 233, 247]
    assert [row["minimum_numbers"] for row in scored["diagnostics"]] == [120, 129, 136]
    assert catalog["challenge_source"]["system_prompt"] == ""
    assert all("without tools" in row["prompt"] for row in catalog["challenges"])
    assert len(reference["outputs"]) == len(reference["sample_ids"]) == 3


@pytest.mark.parametrize("target", ["deepseek/deepseek-v4.1-flash-1017", "DEEPSEEK-V4.1-FLASH-1017", "unknown-model"])
def test_unsupported_exact_target_no_guessed_probability(reference, target, scored):
    completed, result = run_score({**reference, "target_model": target})
    assert completed.returncode == 0 and completed.stderr == ""
    assert result["target_probability"] is None
    assert "unsupported_target" in result["warning"]
    # The supported closed-set candidates remain calibrated; unknown target is not mapped.
    assert result["results"] == scored["results"]
    assert result["probability_status"] == "reference_calibrated"


def test_claimed_identity_provider_metadata_and_keys_never_drive_scoring(reference, scored):
    data = {**reference, "provider": "anthropic", "api_key": "secret-not-for-scoring",
            "outputs": [{**row, "model": "claude-opus-5.5", "api_key": "secret-not-for-scoring"}
                        for row in reference["outputs"]]}
    completed, result = run_score(data)
    assert completed.returncode == 0 and completed.stderr == ""
    assert result == scored
    assert "secret-not-for-scoring" not in completed.stdout


@pytest.mark.parametrize("count", [0, 1, 2])
def test_incomplete_group_no_calibrated_confidence(reference, count):
    completed, result = run_score({**reference, "outputs": reference["outputs"][:count]})
    assert completed.returncode == 0 and completed.stderr == ""
    assert result["decision"] == "unscorable"
    assert result["probability_status"] == "unavailable"
    assert result["probability"] is result["target_probability"] is None
    assert result["results"] == []
    assert result["used_outputs"] == count


@pytest.mark.parametrize("text,decision", [("I am deepseek-v4.1-flash-1017", "unscorable"),
                                             (" ".join(map(str, range(1, 251))), "not_model")])
def test_non_number_claims_and_program_sequences_no_identity(text, decision):
    completed, result = run_score({"target_model": "deepseek-v4.1-flash-1017",
                                  "outputs": [{"text": text, "expected_count": 218}] * 3})
    assert completed.returncode == 0 and completed.stderr == ""
    assert result["decision"] == decision
    assert result["probability_status"] == "unavailable"
    assert result["probability"] is result["target_probability"] is None
    assert result["results"] == []


def test_unavailable_calibration_keeps_scores_without_fabricated_confidence(reference, scored, tmp_path):
    # Modify only a temporary artifact's same-length header scalar; numerical ranker untouched.
    for name in ["score.mjs", "bank.json"]:
        shutil.copyfile(DETECTOR / name, tmp_path / name)
    artifact = bytearray((DETECTOR / "shared_detector.bin").read_bytes())
    header_length = int.from_bytes(artifact[:4], "little")
    header = bytes(artifact[4:4 + header_length])
    assert header.count(b'"ranking-temperature"') == 1
    header = header.replace(b'"ranking-temperature"', b'"missing-temperature"')
    artifact[4:4 + header_length] = header
    (tmp_path / "shared_detector.bin").write_bytes(artifact)
    completed, result = run_score(reference, runner=tmp_path / "score.mjs")
    assert completed.returncode == 0 and completed.stderr == ""
    assert result["decision"] == "ranked"
    assert result["probability_status"] == "unavailable"
    assert result["probability"] is result["target_probability"] is None
    assert all(row["probability"] is None for row in result["results"])
    assert [(row["model"], row["score"]) for row in result["results"]] == [
        (row["model"], row["score"]) for row in scored["results"]]


def test_runner_artifacts_resolve_independent_of_current_directory(reference, scored, tmp_path):
    completed, result = run_score(reference, cwd=tmp_path)
    assert completed.returncode == 0 and completed.stderr == ""
    assert result == scored


@pytest.mark.parametrize("value", ["bad secret-not-for-scoring", {}, {"outputs": []},
                                     {"outputs": [{"text": 123, "expected_count": 218}], "target_model": "x"},
                                     {"outputs": [], "target_model": "x" * 201}])
def test_invalid_input_controlled_json_only_no_user_strings(value):
    completed, result = run_score(value)
    assert completed.returncode == 1
    assert completed.stderr == "detector_invalid_input\n"
    assert result["decision"] == "unscorable"
    assert result["probability_status"] == "unavailable"
    assert result["target_probability"] is result["probability"] is None
    assert "secret-not-for-scoring" not in completed.stdout + completed.stderr


def test_original_sources_and_binary_match_pinned_sha256():
    provenance = json.loads((DETECTOR / "provenance.json").read_text(encoding='utf-8'))
    assert provenance["commit"] == COMMIT
    for row in provenance["files"]:
        upstream = row["upstream_path"]
        path = (DETECTOR / "upstream" / Path(upstream).name if upstream.startswith("shared/")
                else DETECTOR / "shared_detector.bin" if upstream.endswith(".bin")
                else DETECTOR / "LICENSE" if upstream == "LICENSE" else None)
        if path is not None:
            data = path.read_bytes()
            assert len(data) == row["bytes"]
            assert hashlib.sha256(data).hexdigest() == row["sha256"]
            git_blob = b"blob " + str(len(data)).encode() + b"\0" + data
            assert hashlib.sha1(git_blob).hexdigest() == row["git_blob_sha1"]
