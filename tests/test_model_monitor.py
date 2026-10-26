import asyncio
import copy
import json
import time
from pathlib import Path

import httpx
import pytest

from proxy.app import create_app
from proxy.engine import GatewayError
from proxy.model_monitor import ModelMonitor, configuration_key
from proxy.store import Store
from test_engine import config, MODEL, frame, wire

SAMPLE_NUMBERS=', '.join(str(1+(i*137)%355) for i in range(250))


async def settle(monitor,job):
    task=monitor.tasks.get(job['id'])
    if task:await task
    return monitor.jobs[job['id']]


def target(provider='1'):
    return {'provider_id':provider,'model':MODEL,'reference_model':MODEL}


async def settings(monitor,enabled=True):
    current=monitor.db.settings()
    return await monitor.save_settings({**current,'enabled':enabled,'interval_minutes':30,'threshold_percent':95,'targets':[target(str(x)) for x in (1,2,3)]})


def analysis(probability=.99,**extra):
    return {'decision':'ranked','probability_status':'reference_calibrated','used_outputs':3,'target_probability':probability,
            'probability':probability,'prediction':MODEL,'results':[{'model':MODEL,'probability':probability}],**extra}


@pytest.mark.asyncio
async def test_manual_real_scoring_and_transition_journal(tmp_path):
    fixture=json.loads((Path(__file__).parent.parent/'detector'/'fixtures'/'reference_samples.json').read_text())
    outputs=fixture['outputs']
    calls=[]
    def upstream(req):
        calls.append(req.url.host)
        text=outputs[(len(calls)-1)%3]['text']
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(text),frame(reason='stop')]))
    store=Store(tmp_path);config(store)
    monitor=ModelMonitor(store,tmp_path,httpx.MockTransport(upstream))
    assert monitor.available
    await settings(monitor)
    job=await monitor.enqueue('1',MODEL,MODEL)
    done=await settle(monitor,job)
    assert done['status']=='done' and done['result']['status']=='verified',done
    assert done['result']['target_probability']>=.95
    assert len(calls)==3
    assert not await monitor.blocked_providers()
    assert len(monitor.db.journal())==1
    await settle(monitor,await monitor.enqueue('1',MODEL,MODEL))
    assert len(monitor.db.journal())==1
    await monitor.close()


@pytest.mark.asyncio
async def test_quarantine_error_hold_recovery_and_restart(tmp_path):
    def upstream(req):return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(SAMPLE_NUMBERS),frame(reason='stop')]))
    store=Store(tmp_path);config(store)
    monitor=ModelMonitor(store,tmp_path,httpx.MockTransport(upstream))
    await settings(monitor)
    probability=.90
    async def score(*args):return analysis(probability)
    monitor.score=score
    done=await settle(monitor,await monitor.enqueue('1',MODEL,MODEL))
    assert done['result']['status']=='suspect'
    assert await monitor.blocked_providers()=={'1'}
    restarted=ModelMonitor(store,tmp_path,httpx.MockTransport(upstream))
    assert await restarted.blocked_providers()=={'1'}
    async def broken_score(*args):raise ValueError('offline')
    monitor.score=broken_score
    await settle(monitor,await monitor.enqueue('1',MODEL,MODEL))
    assert await monitor.blocked_providers()=={'1'}
    assert len(monitor.db.journal())==1
    monitor.score=score;probability=.99
    await settle(monitor,await monitor.enqueue('1',MODEL,MODEL))
    assert not await monitor.blocked_providers()
    assert len(monitor.db.journal())==2
    await monitor.close();await restarted.close()


@pytest.mark.asyncio
async def test_unknown_not_quarantined_and_changed_config_invalidates(tmp_path):
    def upstream(req):return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(SAMPLE_NUMBERS),frame(reason='stop')]))
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path,httpx.MockTransport(upstream));await settings(m)
    async def score(*args):return analysis(.1,probability_status='unavailable')
    m.score=score
    done=await settle(m,await m.enqueue('1',MODEL,MODEL));assert done['result']['status']=='unknown'
    assert not await m.blocked_providers() and not m.db.journal()
    async def suspect(*args):return analysis(.1)
    m.score=suspect
    await settle(m,await m.enqueue('1',MODEL,MODEL));assert await m.blocked_providers()=={'1'}
    c=s.get_config();c['providers'][0]['api_key']='new-credential';s.save_config(c,c['revision'])
    assert not await m.blocked_providers()
    await m.close()


@pytest.mark.asyncio
async def test_live_routing_quarantine_and_manual_disable_preserved(tmp_path):
    seen=[]
    def upstream(req):
        seen.append(req.url.host)
        return httpx.Response(200,json={'choices':[{'message':{'content':'OK'},'finish_reason':'stop'}]})
    app=create_app(tmp_path,httpx.MockTransport(upstream));s=app.state.store;config(s);m=app.state.monitor
    await settings(m)
    c=s.get_config(True);p=c['providers'][0]
    m.db.record_result({'id':'test-suspect','provider_id':'1','provider_name':p['name'],'model':MODEL,'reference_model':MODEL,'configuration_key':m.binding_key(p,MODEL,MODEL),'status':'suspect','target_probability':.1,'prediction':'other','prediction_probability':.9,'checked_at':time.time(),'source':'scheduled','error_kind':None,'candidates':[],'duration_ms':1,'reference_version':'test'})
    key=s.create_key('test',[])['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        r=await client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+key},json={'model':MODEL,'messages':[]})
    assert r.status_code==200 and seen==['p2.invalid']
    assert s.get_config()['providers'][0]['enabled'] is True
    await settings(m,False)
    assert not await m.blocked_providers()
    c=s.get_config();c['providers'][0]['enabled']=False;s.save_config(c,c['revision'])
    await m.refresh();assert s.get_config()['providers'][0]['enabled'] is False
    await m.close();await app.state.engine.close()


@pytest.mark.asyncio
async def test_detector_admin_auth_csrf_jobs_and_no_service(tmp_path):
    app=create_app(tmp_path);app.state.store.initialize_admin('admin','password');config(app.state.store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
        assert (await c.get('/admin/detector')).status_code==401
        session=(await c.post('/admin/login',json={'username':'admin','password':'password'})).json()
        info=(await c.get('/admin/detector')).json();assert info['available']
        assert info['settings']['interval_minutes']==30 and not info['settings']['enabled']
        assert (await c.put('/admin/detector/settings',json=info['settings'])).status_code==403
        h={'X-CSRF-Token':session['csrf_token']}
        data={**info['settings'],'enabled':True,'targets':[target(str(x)) for x in (1,2,3)]}
        assert (await c.put('/admin/detector/settings',json=data,headers=h)).status_code==200
        assert (await c.put('/admin/detector/settings',json=data,headers=h)).status_code==409
        assert (await c.post('/admin/detector/check',headers=h,json={'provider_id':'1','model':MODEL,'reference_model':'invented'})).status_code==400
        assert (await c.get('/admin/detector/log')).json()=={'changes':[]}
    await app.state.monitor.close();await app.state.engine.close()
