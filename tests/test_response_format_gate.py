"""Preemptive response_format gate: route passports and the 400-before-upstream path.

Ports the feature tests onto the upstream layout: a declared refusal happens before
any provider is called; an absent passport keeps best-effort passthrough.
"""

import json

import httpx
import pytest

from proxy.engine import Completion, Engine, GatewayError, parse_sse

MODEL = 'deepseek-v4.1-flash-1017'


def _ok(req):
    return httpx.Response(200, json={"choices": [{"index": 0, "message": {
        "role": "assistant", "content": "okay"}, "finish_reason": "stop"}]})



# ---- 5. response_format gate: a declared refusal happens before the upstream call ----
# A route that declares it cannot serve structured output was still being called, paying
# for a round trip that could only end in a refusal. The passport lets the relay answer
# before any provider is contacted; an absent passport keeps today's best effort intact.

_MISSING = object()


def _rf_raw_config(tmp_path, passport):
    """A valid config with one route whose passport is `passport` (or absent)."""
    from proxy.store import Store

    c = Store(tmp_path).get_config()
    c["settings"]["trust_private_upstreams"] = True
    c["providers"] = [{"id": "1", "name": "P1", "priority": 1, "enabled": True,
                       "base_url": "http://p1.invalid/v1", "api_key": "secret-1"}]
    route = {"provider_id": "1", "upstream_model": MODEL, "enabled": True}
    if passport is not _MISSING:
        route["supports_response_formats"] = passport
    c["models"] = [{"id": "model", "name": MODEL, "enabled": True,
                    "supports_vision": False, "routes": [route]}]
    return c


def _rf_store(tmp_path, routes):
    """Store with one model; routes is [(provider_id, passport-or-None), ...]."""
    from proxy.store import Store

    store = Store(tmp_path)
    c = store.get_config()
    c["settings"]["trust_private_upstreams"] = True
    c["providers"] = [
        {"id": pid, "name": "Provider " + pid, "priority": int(pid), "enabled": True,
         "base_url": "http://p" + pid + ".invalid/v1", "api_key": "secret-" + pid}
        for pid, _ in routes
    ]
    model_routes = []
    for pid, passport in routes:
        route = {"provider_id": pid, "upstream_model": MODEL, "enabled": True}
        if passport is not None:
            route["supports_response_formats"] = passport
        model_routes.append(route)
    c["models"] = [{"id": "model", "name": MODEL, "enabled": True, "supports_vision": True,
                    "routes": model_routes}]
    store.save_config(c, c["revision"])
    return store


def _rf_engine(store, handler):
    return Engine(store, httpx.MockTransport(handler))


@pytest.mark.parametrize("value,expected", [
    (True, ["json_schema", "json_object"]),
    (False, []),
    (["json_object"], ["json_object"]),
    (["json_schema", "json_object"], ["json_schema", "json_object"]),
])
def test_route_response_format_passport_is_normalized(tmp_path, value, expected):
    from proxy.config import validate_config

    clean = validate_config(_rf_raw_config(tmp_path, value))
    assert clean["models"][0]["routes"][0]["supports_response_formats"] == expected


@pytest.mark.parametrize("value", ["yaml", ["yaml"], [True], None, {"json_schema": True}, 1])
def test_invalid_route_response_format_passport_is_rejected(tmp_path, value):
    from proxy.config import validate_config

    with pytest.raises(ValueError, match="^invalid_supports_response_formats$"):
        validate_config(_rf_raw_config(tmp_path, value))


def test_absent_response_format_passport_stays_absent(tmp_path):
    from proxy.config import validate_config

    clean = validate_config(_rf_raw_config(tmp_path, _MISSING))
    assert "supports_response_formats" not in clean["models"][0]["routes"][0]


def test_route_response_format_passport_roundtrips_through_store(tmp_path):
    from proxy.config import validate_config
    from proxy.store import Store

    raw = _rf_raw_config(tmp_path, True)
    stored = Store(tmp_path)
    saved = stored.save_config(validate_config(raw), raw["revision"])
    assert saved["models"][0]["routes"][0]["supports_response_formats"] == ["json_schema", "json_object"]
    reloaded = Store(tmp_path).get_config()
    assert reloaded["models"][0]["routes"][0]["supports_response_formats"] == ["json_schema", "json_object"]


def test_response_format_passport_is_a_route_field_not_a_model_field(tmp_path):
    from proxy.config import validate_config

    raw = _rf_raw_config(tmp_path, ["json_object"])
    raw["models"][0]["supports_response_formats"] = True
    clean = validate_config(raw)
    assert "supports_response_formats" not in clean["models"][0]


@pytest.mark.asyncio
async def test_gate_refuses_before_any_upstream_call(tmp_path):
    store = _rf_store(tmp_path, [("1", False), ("2", [])])
    called = []

    def handler(req):
        called.append(req.url.host)
        return _ok(req)

    engine = _rf_engine(store, handler)
    try:
        with pytest.raises(GatewayError) as caught:
            await engine.generate(
                {"model": MODEL, "messages": [],
                 "response_format": {"type": "json_schema",
                                     "json_schema": {"name": "x", "schema": {"type": "object"}}}},
                store.get_config(True), "req_gate", "key")
        error = caught.value
        assert error.kind == "response_format_unsupported"
        assert (error.status, error.retryable) == (400, False)
        assert MODEL in error.detail
        assert "excluded by config" in error.detail
        assert "Retry without response_format" in error.detail
        assert called == [], "a declared-unsupported route must not be called"
        # Telemetry: the refusal is a request with zero attempts, not a lost request.
        with store._connection() as conn:
            payload = json.loads(conn.execute(
                "SELECT payload FROM requests WHERE id=?", ("req_gate",)).fetchone()[0])
        assert payload["error_kind"] == "response_format_unsupported"
        assert payload["attempts"] == 0
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_gate_skips_declared_route_and_keeps_unknown(tmp_path):
    store = _rf_store(tmp_path, [("1", False), ("2", None)])
    called = []

    def handler(req):
        called.append(req.url.host)
        return _ok(req)

    engine = _rf_engine(store, handler)
    try:
        result = await engine.generate(
            {"model": MODEL, "messages": [], "response_format": {"type": "json_schema"}},
            store.get_config(True), "req_mixed", "key")
        assert result["choices"][0]["message"]["content"] == "okay"
        assert called == ["p2.invalid"]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_passport_is_per_format(tmp_path):
    store = _rf_store(tmp_path, [("1", ["json_object"])])
    called = []

    def handler(req):
        called.append(req.url.host)
        return _ok(req)

    engine = _rf_engine(store, handler)
    try:
        result = await engine.generate(
            {"model": MODEL, "messages": [], "response_format": {"type": "json_object"}},
            store.get_config(True), "req_object", "key")
        assert result["choices"][0]["message"]["content"] == "okay"
        assert called == ["p1.invalid"]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_json_schema_is_filtered_by_json_object_only_passport(tmp_path):
    store = _rf_store(tmp_path, [("1", ["json_object"])])
    called = []

    def handler(req):
        called.append(req.url.host)
        return _ok(req)

    engine = _rf_engine(store, handler)
    try:
        with pytest.raises(GatewayError) as caught:
            await engine.generate(
                {"model": MODEL, "messages": [], "response_format": {"type": "json_schema"}},
                store.get_config(True), "req_schema", "key")
        assert caught.value.kind == "response_format_unsupported"
        assert called == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_no_passport_keeps_best_effort_passthrough(tmp_path):
    store = _rf_store(tmp_path, [("1", None), ("2", None)])
    called = []

    def handler(req):
        called.append(req.url.host)
        return _ok(req)

    engine = _rf_engine(store, handler)
    try:
        result = await engine.generate(
            {"model": MODEL, "messages": [], "response_format": {"type": "json_schema"}},
            store.get_config(True), "req_passthrough", "key")
        assert result["choices"][0]["message"]["content"] == "okay"
        assert called == ["p1.invalid"]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_capable_route_in_cooldown_is_503_not_400(tmp_path):
    import time

    store = _rf_store(tmp_path, [("1", ["json_schema", "json_object"])])
    called = []

    def handler(req):
        called.append(req.url.host)
        return _ok(req)

    engine = _rf_engine(store, handler)
    try:
        engine.cooldowns["1"] = time.time() + 60
        with pytest.raises(GatewayError) as caught:
            await engine.generate(
                {"model": MODEL, "messages": [], "response_format": {"type": "json_schema"}},
                store.get_config(True), "req_cooldown", "key")
        assert caught.value.kind == "no_available_provider"
        assert caught.value.status == 503
        assert called == []
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("response_format", [_MISSING, {"type": "text"}])
async def test_gate_inactive_without_structured_output(tmp_path, response_format):
    store = _rf_store(tmp_path, [("1", [])])
    called = []

    def handler(req):
        called.append(req.url.host)
        return _ok(req)

    payload = {"model": MODEL, "messages": []}
    if response_format is not _MISSING:
        payload["response_format"] = response_format
    engine = _rf_engine(store, handler)
    try:
        result = await engine.generate(payload, store.get_config(True), "req_text", "key")
        assert result["choices"][0]["message"]["content"] == "okay"
        assert called == ["p1.invalid"]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_post_factum_refusal_still_works_on_unknown_route(tmp_path):
    store = _rf_store(tmp_path, [("1", False), ("2", None)])
    called = []

    def handler(req):
        called.append(req.url.host)
        return httpx.Response(400, json={"error": {"message": "response_format is not supported"}})

    engine = _rf_engine(store, handler)
    try:
        with pytest.raises(GatewayError) as caught:
            await engine.generate(
                {"model": MODEL, "messages": [], "response_format": {"type": "json_schema"}},
                store.get_config(True), "req_postfactum", "key")
        assert caught.value.kind == "response_format_unsupported"
        assert caught.value.status == 400
        assert called == ["p2.invalid"]
    finally:
        await engine.close()


def test_preemptive_refusal_api_contract(tmp_path):
    from fastapi.testclient import TestClient

    from proxy.app import create_app

    app = create_app(tmp_path, transport=httpx.MockTransport(
        lambda request: pytest.fail("upstream must not be called")))
    app.state.store.initialize_admin("admin", "test-password-long")
    store = app.state.store
    c = store.get_config()
    c["settings"]["trust_private_upstreams"] = True
    c["providers"] = [{"id": "1", "name": "P1", "priority": 1, "enabled": True,
                       "base_url": "http://p1.invalid/v1", "api_key": "secret-1"}]
    c["models"] = [{"id": "model", "name": MODEL, "enabled": True, "supports_vision": False,
                    "routes": [{"provider_id": "1", "upstream_model": MODEL, "enabled": True,
                                "supports_response_formats": False}]}]
    store.save_config(c, c["revision"])
    key = store.create_key("test", [])
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions",
                               headers={"Authorization": "Bearer " + key["token"]},
                               json={"model": MODEL, "messages": [],
                                     "response_format": {"type": "json_schema"}})
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "response_format_unsupported"
    assert body["param"] == "response_format"
    assert body["type"] == "gateway_error"
    assert MODEL in body["message"]
    assert "excluded by config" in body["message"]
    assert response.headers["X-Request-ID"].startswith("req_")


def test_models_endpoint_advertises_route_passports(tmp_path):
    from fastapi.testclient import TestClient

    from proxy.app import create_app

    app = create_app(tmp_path, transport=httpx.MockTransport(lambda request: _ok(request)))
    app.state.store.initialize_admin("admin", "test-password-long")
    store = app.state.store
    c = store.get_config()
    c["settings"]["trust_private_upstreams"] = True
    c["providers"] = [
        {"id": "1", "name": "P1", "priority": 1, "enabled": True,
         "base_url": "http://p1.invalid/v1", "api_key": "secret-1"},
        {"id": "2", "name": "P2", "priority": 2, "enabled": True,
         "base_url": "http://p2.invalid/v1", "api_key": "secret-2"},
    ]
    c["models"] = [
        {"id": "m1", "name": "model-narrow", "enabled": True, "supports_vision": False,
         "routes": [{"provider_id": "1", "upstream_model": "upstream", "enabled": True,
                     "supports_response_formats": ["json_schema", "json_object"]},
                    {"provider_id": "2", "upstream_model": "upstream", "enabled": True,
                     "supports_response_formats": ["json_object"]}]},
        {"id": "m2", "name": "model-unknown", "enabled": True, "supports_vision": False,
         "routes": [{"provider_id": "1", "upstream_model": "upstream", "enabled": True}]},
    ]
    store.save_config(c, c["revision"])
    key = store.create_key("test", [])
    with TestClient(app) as client:
        data = client.get("/v1/models", headers={"Authorization": "Bearer " + key["token"]}).json()["data"]
    by_name = {row["id"]: row for row in data}
    assert by_name["model-narrow"]["supports_response_formats"] == ["json_object"]
    assert by_name["model-unknown"]["supports_response_formats"] is None
