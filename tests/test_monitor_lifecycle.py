import asyncio
import time

import httpx
import pytest

from proxy.model_monitor import ModelMonitor
from proxy.store import Store
from test_engine import config,MODEL,frame,wire
from test_model_monitor import SAMPLE_NUMBERS, settings,target,analysis,settle


@pytest.mark.asyncio
async def test_scheduler_runs_and_rechecks_quarantined_target(tmp_path):
    def upstream(req):return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(SAMPLE_NUMBERS),frame(reason='stop')]))
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path,httpx.MockTransport(upstream))
    async def score(*_):return analysis(.5)
    m.score=score
    await settings(m)
    await m.start()
    async with asyncio.timeout(5):
        while not len(m.jobs)>=3 or any(j['status'] in ('queued','running') for j in m.jobs.values()):await asyncio.sleep(.01)
    assert await m.blocked_providers()=={'1','2','3'}
    assert len(m.db.journal())==3
    previous=len(m.jobs)
    m.next_run=time.time()-1;m.wake.set()
    async with asyncio.timeout(5):
        while len(m.jobs)==previous or any(j['status'] in ('queued','running') for j in m.jobs.values()):await asyncio.sleep(.01)
    assert len(m.jobs)==previous+3
    assert len(m.db.journal())==3
    await m.close()


@pytest.mark.asyncio
async def test_cancel_probe_does_not_create_false_result(tmp_path):
    entered=asyncio.Event();cancelled=asyncio.Event()
    async def upstream(req):
        entered.set()
        try:await asyncio.sleep(60)
        except asyncio.CancelledError:cancelled.set();raise
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path,httpx.MockTransport(upstream))
    job=await m.enqueue('1',MODEL,MODEL)
    await entered.wait()
    assert await m.cancel(job['id'])
    assert cancelled.is_set()
    assert m.jobs[job['id']]['status']=='error' and not m.db.journal()
    await m.close()


@pytest.mark.asyncio
async def test_changed_config_mid_probe_is_not_conclusive(tmp_path):
    def upstream(req):return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(SAMPLE_NUMBERS),frame(reason='stop')]))
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path,httpx.MockTransport(upstream));await settings(m)
    async def score(*_):
        c=s.get_config();c['providers'][0]['api_key']='changed';s.save_config(c,c['revision'])
        return analysis(.01)
    m.score=score
    done=await settle(m,await m.enqueue('1',MODEL,MODEL))
    assert done['result']['status']=='unknown' and done['result']['error_kind']=='configuration_changed'
    assert not m.db.journal() and not await m.blocked_providers()
    await m.close()


@pytest.mark.asyncio
async def test_quarantine_uses_request_snapshot_when_config_changes(tmp_path):
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path);await settings(m)
    old=s.get_config(True);p=old['providers'][0]
    m.db.record_result({'id':'race','provider_id':'1','provider_name':p['name'],'model':MODEL,'reference_model':MODEL,'configuration_key':m.binding_key(p,MODEL,MODEL),'status':'suspect','target_probability':.1,'prediction':'other','prediction_probability':.9,'checked_at':time.time(),'source':'manual','candidates':[],'duration_ms':1,'reference_version':'test'})
    c=s.get_config();c['providers'][0]['api_key']='changed';s.save_config(c,c['revision'])
    assert not await m.blocked_providers()
    assert await m.blocked_providers(old)=={'1'}
    await m.close()


@pytest.mark.asyncio
async def test_monitor_covers_new_routes_and_commit_cannot_be_cancelled(tmp_path):
    import threading
    def upstream(req):return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(SAMPLE_NUMBERS),frame(reason='stop')]))
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path,httpx.MockTransport(upstream));await settings(m)
    c=s.get_config();c['models'][0]['routes'][0]['upstream_model']='unmonitored-new-model'
    with pytest.raises(ValueError,match='unmonitored'):await m.validate_routing_change(c)
    entered=threading.Event();release=threading.Event();original=m.db.record_result
    def delayed(result):
        entered.set();release.wait(3)
        return original(result)
    m.db.record_result=delayed
    async def score(*_):return analysis(.1)
    m.score=score
    job=await m.enqueue('1',MODEL,MODEL)
    async with asyncio.timeout(3):
        while not entered.is_set():await asyncio.sleep(.01)
    assert await m.cancel(job['id']) is False
    release.set();await settle(m,job)
    assert m.jobs[job['id']]['status']=='done'
    assert await m.blocked_providers()=={'1'}
    await m.close()


@pytest.mark.asyncio
async def test_invalid_settings_and_manual_inputs(tmp_path):
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path)
    current=m.db.settings()
    for payload in ({**current,'enabled':True,'targets':[target('1')]}, {**current,'interval_minutes':True}, {**current,'threshold_percent':101}):
        with pytest.raises(ValueError):await m.save_settings(payload)
    for data in [('missing',MODEL,MODEL),('1',MODEL,'unknown'),('1','has space',MODEL)]:
        with pytest.raises(ValueError):await m.enqueue(*data)
    await m.close()
