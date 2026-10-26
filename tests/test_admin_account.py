import asyncio

import httpx
import pytest

from proxy.app import create_app
from proxy.store import Store


@pytest.mark.asyncio
async def test_account_changes_require_password_csrf_revoke_all_sessions(tmp_path):
    app=create_app(tmp_path);s=app.state.store;s.initialize_admin('admin','old-password')
    key=s.create_key('unchanged',[])
    transport=httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport,base_url='http://test') as a,httpx.AsyncClient(transport=transport,base_url='http://test') as b:
        h={'X-CSRF-Token':(await a.post('/admin/login',json={'username':'admin','password':'old-password'})).json()['csrf_token']}
        await b.post('/admin/login',json={'username':'admin','password':'old-password'})
        payload={'current_password':'old-password','new_username':'owner','new_password':'new-strong-password-123'}
        assert (await a.post('/admin/account',json=payload)).status_code==403
        wrong=await a.post('/admin/account',headers=h,json={**payload,'current_password':'wrong'})
        assert wrong.status_code==403 and wrong.json()['error']['code']=='invalid_current_password'
        assert s.authenticate_admin('admin','old-password')
        changed=await a.post('/admin/account',headers=h,json=payload)
        assert changed.status_code==200 and changed.json()=={'ok':True,'reauthenticate':True}
        assert 'new-strong' not in changed.text and 'old-password' not in changed.text
        assert (await a.get('/admin/session')).status_code==401
        assert (await b.get('/admin/session')).status_code==401
        assert not s.authenticate_admin('admin','old-password') and s.authenticate_admin('owner','new-strong-password-123')
        s.initialize_admin('admin','old-password')
        assert s.authenticate_admin('owner','new-strong-password-123'), 'restart env must not reset changed credentials'
        assert (await a.post('/admin/login',json={'username':'owner','password':'new-strong-password-123'})).status_code==200
        assert s.authenticate_key(key['token'])
    await app.state.pools.close();await app.state.monitor.close();await app.state.engine.close()


@pytest.mark.asyncio
async def test_username_only_change_preserves_password_and_rate_limits_failures(tmp_path):
    app=create_app(tmp_path);s=app.state.store;s.initialize_admin('admin','old-password')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
        h={'X-CSRF-Token':(await c.post('/admin/login',json={'username':'admin','password':'old-password'})).json()['csrf_token']}
        for _ in range(5):
            r=await c.post('/admin/account',headers=h,json={'current_password':'bad','new_username':'owner','new_password':''});assert r.status_code==403
        assert (await c.post('/admin/account',headers=h,json={'current_password':'old-password','new_username':'owner','new_password':''})).status_code==429
    # Fresh app limiter; same durable account/session database.
    fresh=create_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(fresh),base_url='http://test') as c:
        h={'X-CSRF-Token':(await c.post('/admin/login',json={'username':'admin','password':'old-password'})).json()['csrf_token']}
        r=await c.post('/admin/account',headers=h,json={'current_password':'old-password','new_username':'owner','new_password':''});assert r.status_code==200
        assert s.authenticate_admin('owner','old-password')
    for target in (app,fresh):await target.state.pools.close();await target.state.monitor.close();await target.state.engine.close()


@pytest.mark.parametrize('username,password',[('', 'strong-password123'),('has space','strong-password123'),('bad\nname','strong-password123'),('admin','short'),('admin',' '*12),('admin',None)])
def test_bad_new_credentials_rejected_without_revoking_session(tmp_path,username,password):
    s=Store(tmp_path);s.initialize_admin('admin','old-password');cookie,_=s.create_session('admin')
    with pytest.raises(ValueError):s.update_admin_credentials('admin','old-password',username,password,cookie)
    assert s.get_session(cookie) and s.authenticate_admin('admin','old-password')
