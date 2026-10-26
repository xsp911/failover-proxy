"""Optional model limits are validated metadata, persisted without enforcement."""

import pytest

from proxy.config import validate_config
from proxy.store import Store


@pytest.fixture
def config(tmp_path):
    value = Store(tmp_path / "private").get_config()
    value["models"] = [{"id": "chat", "name": "chat", "enabled": True,
                        "supports_vision": False, "routes": []}]
    return value


@pytest.mark.parametrize("field,minimum", [("context_length", 1000), ("max_output_tokens", 1)])
@pytest.mark.parametrize("value", [True, False, 1000.5, 1000.0, -1, 0, 10000001, "1000", [], {}])
def test_invalid_model_metadata(config, field, minimum, value):
    config["models"][0][field] = value
    with pytest.raises(ValueError, match="^invalid_" + field + "$"):
        validate_config(config)


def test_context_length_below_minimum(config):
    config["models"][0]["context_length"] = 999
    with pytest.raises(ValueError, match="^invalid_context_length$"):
        validate_config(config)


@pytest.mark.parametrize("field,minimum", [("context_length", 1000), ("max_output_tokens", 1)])
@pytest.mark.parametrize("limit", ["minimum", "maximum", "null", "missing"])
def test_valid_model_metadata(config, field, minimum, limit):
    if limit != "missing":
        config["models"][0][field] = {"minimum": minimum, "maximum": 10000000, "null": None}[limit]
    clean = validate_config(config)
    assert clean["models"] == config["models"]
    if limit in ("minimum", "maximum"):
        assert type(clean["models"][0][field]) is int


@pytest.mark.parametrize("metadata", [
    {},
    {"context_length": None, "max_output_tokens": None},
    {"context_length": 1000, "max_output_tokens": 1},
    {"context_length": 10000000, "max_output_tokens": 10000000},
    {"context_length": 128000, "max_output_tokens": 16384},
    {"context_length": None, "max_output_tokens": 16384},
    {"context_length": 128000},
])
def test_model_metadata_store_roundtrip(config, tmp_path, metadata):
    config["models"][0].update(metadata)
    expected = config["models"][0].copy()
    store = Store(tmp_path / "private")
    saved = store.save_config(validate_config(config), config["revision"])
    assert saved["models"] == [expected]
    reopened = Store(store.data_dir)
    loaded = validate_config(reopened.get_config())
    assert loaded["models"] == [expected]
    loaded["models"][0]["name"] = "renamed-chat"
    expected["name"] = "renamed-chat"
    saved = reopened.save_config(validate_config(loaded), loaded["revision"])
    assert saved["models"] == [expected]
    assert Store(store.data_dir).get_config()["models"] == [expected]
