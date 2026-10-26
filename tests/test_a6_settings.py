import json
import time

import httpx
import pytest

from proxy.a6_settings import A6Settings, mask, validate
from proxy.app import create_app
from test_engine import config, MODEL

PAT = 'SYNTHETIC_TEST_PAT_0000000000000'


def app_with_a6(tmp_path):
    app = create_app(tmp_path)
    app.state.store.initialize_admin('admin', 'password')
    config(app.state.store)
    return app


async def login(client):
    r = await client.post('/admin/login', json={'username': 'admin', 'password': 'password'})
    assert r.status_code == 200
    return {'X-CSRF-Token': r.json()['csrf_token']}


@pytest.mark.asyncio
async def test_requires_session_and_csrf(tmp_path):
    app = app_with_a6(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as c:
        assert (await c.get('/admin/a6')).status_code == 401
        h = await login(c)
        assert (await c.put('/admin/a6', json={'pat': PAT, 'user_id': 23984})).status_code == 403
        assert (await c.delete('/admin/a6')).status_code == 403
    await app.state.monitor.close()
    await app.state.engine.close()


@pytest.mark.asyncio
async def test_save_masks_and_never_returns_pat(tmp_path):
    app = app_with_a6(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as c:
        h = await login(c)
        r = await c.put('/admin/a6', json={'pat': PAT, 'user_id': 23984}, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {'configured': True, 'has_pat': True, 'pat_masked': 'SYNT********00', 'user_id': 23984}
        assert PAT not in r.text
        again = (await c.get('/admin/a6', headers=h)).json()
        assert PAT not in json.dumps(again)
        raw = (tmp_path / 'proxy.db').read_bytes()
        assert PAT.encode() not in raw
    await app.state.monitor.close()
    await app.state.engine.close()


@pytest.mark.asyncio
async def test_blank_pat_keeps_secret_and_clear_removes(tmp_path):
    app = app_with_a6(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as c:
        h = await login(c)
        await c.put('/admin/a6', json={'pat': PAT, 'user_id': 23984}, headers=h)
        r = await c.put('/admin/a6', json={'pat': '', 'user_id': 24000}, headers=h)
        assert r.status_code == 200 and r.json()['user_id'] == 24000
        assert app.state.a6.secret() == (PAT, 24000)
        cleared = (await c.delete('/admin/a6', headers=h)).json()
        assert cleared == {'configured': False, 'has_pat': False, 'pat_masked': '', 'user_id': None}
        assert app.state.a6.secret() == (None, None)
        assert (await c.put('/admin/a6', json={'pat': '', 'user_id': 23984}, headers=h)).status_code == 400
    await app.state.monitor.close()
    await app.state.engine.close()


@pytest.mark.asyncio
async def test_validation_rejects_bad_values(tmp_path):
    app = app_with_a6(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as c:
        h = await login(c)
        for payload in ({'pat': 'short', 'user_id': 23984}, {'pat': PAT, 'user_id': '23984'},
                        {'pat': PAT, 'user_id': True}, {'pat': PAT, 'user_id': 0},
                        {'pat': PAT, 'user_id': 2_147_483_648}, {'pat': 'bad token with space', 'user_id': 1}):
            assert (await c.put('/admin/a6', json=payload, headers=h)).status_code == 400, payload
    await app.state.monitor.close()
    await app.state.engine.close()


@pytest.mark.asyncio
async def test_verify_success_and_mismatch(tmp_path):
    def upstream(request):
        if request.url.path == '/api/user/self':
            return httpx.Response(200, json={'data': {'id': 23984, 'display_name': 'xspider'}})
        return httpx.Response(200, json={'data': {'total': 20}})
    app = create_app(tmp_path, transport=None)
    app.state.store.initialize_admin('admin', 'password')
    config(app.state.store)
    from proxy import a6_settings
    original = a6_settings.verify

    async def fake_verify(pat, user_id):
        return await original(pat, user_id, transport=httpx.MockTransport(upstream))

    a6_settings_verify = app.state.a6
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as c:
        h = await login(c)
        import proxy.app as app_module
        app_module.a6_verify_credentials = fake_verify
        ok = await c.post('/admin/a6/verify', json={'pat': PAT, 'user_id': 23984}, headers=h)
        assert ok.status_code == 200
        assert ok.json() == {'ok': True, 'status': 200, 'user_id': 23984, 'display_name': 'xspider', 'tokens_total': 20}
        mismatch = await c.post('/admin/a6/verify', json={'pat': PAT, 'user_id': 999}, headers=h)
        assert mismatch.json()['ok'] is False and mismatch.json()['error_kind'] == 'a6_user_id_mismatch'
        assert PAT not in ok.text
    await app.state.monitor.close()
    await app.state.engine.close()


@pytest.mark.asyncio
async def test_verify_rejected_and_connection_error(tmp_path):
    from proxy.a6_settings import verify
    def rejected(request):
        return httpx.Response(401, json={'message': 'bad'})
    assert await verify(PAT, 23984, transport=httpx.MockTransport(rejected)) == {
        'ok': False, 'status': 401, 'error_kind': 'a6_auth_rejected'}
    def boom(request):
        raise httpx.ConnectError('offline')
    result = await verify(PAT, 23984, transport=httpx.MockTransport(boom))
    assert result['ok'] is False and result['error_kind'] == 'a6_connection_failed'


def test_mask_and_validate_helpers():
    assert mask(PAT) == 'SYNT********00'
    assert mask('short') == '*****'
    assert mask('') == ''
    assert validate(PAT, 23984) == (PAT, 23984)
    for bad in ('', 'has space here 1234567', 'x' * 513):
        with pytest.raises(ValueError):
            validate(bad, 1)
