import asyncio
import json

import httpx
import pytest

from proxy.admission import Admission, CapacityUnavailable, AdmissionCancelled
from proxy.app import create_app, error
from proxy.engine import GatewayError
from test_engine import config, MODEL


@pytest.mark.asyncio
async def test_fifo_and_idempotent_release():
    state={'n':0};gate=Admission(state, wait_seconds=1, max_waiters=4)
    release=await gate.acquire(1)
    order=[]
    async def run(index):
        lease=await gate.acquire(1)
        order.append(index)
        await asyncio.sleep(0)
        lease();lease()
    tasks=[]
    for i in range(3):
        tasks.append(asyncio.create_task(run(i)))
        await asyncio.sleep(0)
    assert state=={'n':1,'queued':3}
    release()
    await asyncio.gather(*tasks)
    assert order==[0,1,2] and state=={'n':0,'queued':0}


@pytest.mark.asyncio
async def test_expiry_overflow_cancellation():
    state={'n':0};gate=Admission(state,wait_seconds=0.03,max_waiters=1)
    release=await gate.acquire(1)
    queued=asyncio.create_task(gate.acquire(1));await asyncio.sleep(0)
    with pytest.raises(CapacityUnavailable):await gate.acquire(1)
    with pytest.raises(CapacityUnavailable):await queued
    assert state['queued']==0 and state['n']==1
    task=asyncio.create_task(gate.acquire(1));await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert state['queued']==0
    release();assert state['n']==0


@pytest.mark.asyncio
async def test_queue_waiter_disconnect():
    state={'n':0};gate=Admission(state,wait_seconds=1)
    release=await gate.acquire(1)
    async def disconnected():return True
    with pytest.raises(AdmissionCancelled):await gate.acquire(1,disconnected)
    assert state['queued']==0
    release()


def auth_config(app):
    config(app.state.store)
    cfg=app.state.store.get_config();cfg['settings']['max_concurrent_requests']=1
    app.state.store.save_config(cfg,cfg['revision'])
    token=app.state.store.create_key('test',[])['token']
    return {'Authorization':'Bearer '+token}


@pytest.mark.asyncio
async def test_http_capacity_waits_then_succeeds(tmp_path):
    requests=[]
    async def upstream(req):
        requests.append(req)
        await asyncio.sleep(0.08)
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'okay'},'finish_reason':'stop'}]})
    app=create_app(tmp_path,transport=httpx.MockTransport(upstream));h=auth_config(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        responses=await asyncio.gather(*(client.post('/v1/chat/completions',headers=h,json={'model':MODEL,'messages':[]}) for _ in range(3)))
    assert all(r.status_code==200 for r in responses)
    assert all(r.headers.get('X-Upstream')=='Provider 1' for r in responses)
    assert len(requests)==3 and app.state.inflight['n']==0 and app.state.inflight['queued']==0
    await app.state.engine.close()


@pytest.mark.asyncio
async def test_http_capacity_timeout_has_retry_after_and_request_id(tmp_path):
    app=create_app(tmp_path);h=auth_config(app)
    app.state.admission.wait_seconds=0.02
    lease=await app.state.admission.acquire(1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        response=await client.post('/v1/chat/completions',headers=h,json={'model':MODEL,'messages':[]})
    assert response.status_code==429 and response.headers['retry-after']=='1'
    assert response.headers['x-request-id'].startswith('req_')
    assert app.state.inflight['queued']==0
    lease();await app.state.engine.close()


@pytest.mark.asyncio
async def test_engine_capacity_path_also_has_retry_after(tmp_path):
    app=create_app(tmp_path);h=auth_config(app)
    async def fail(*args,**kwargs):raise GatewayError('gateway_capacity',429,False)
    app.state.engine.generate=fail
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
        r=await c.post('/v1/chat/completions',headers=h,json={'model':MODEL,'messages':[]})
    assert r.status_code==429 and r.headers['retry-after']=='1'
    assert app.state.inflight['n']==0
    await app.state.engine.close()


@pytest.mark.asyncio
async def test_http_cancellation_while_queued_releases_waiter(tmp_path):
    app=create_app(tmp_path);h=auth_config(app)
    lease=await app.state.admission.acquire(1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
        pending=asyncio.create_task(c.post('/v1/chat/completions',headers=h,json={'model':MODEL,'messages':[]}))
        async with asyncio.timeout(1):
            while app.state.inflight['queued']!=1:
                await asyncio.sleep(0.001)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):await pending
    assert app.state.inflight['queued']==0 and app.state.inflight['n']==1
    lease();await app.state.engine.close()


def test_429_guidance_for_both_paths():
    for kind in ('gateway_capacity','upstream_http_429','login_rate_limited'):
        r=error(kind,429)
        assert r.headers['retry-after']=='1'
