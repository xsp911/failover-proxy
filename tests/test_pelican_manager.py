import asyncio
import json
import re

import httpx
import pytest

from proxy.pelican import PelicanManager,TIMEOUT_SECONDS,PROMPT
from proxy.pelican_store import PelicanStore
from proxy.store import Store
from test_engine import config,MODEL,frame,wire


def response_svg(nonce):return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 640"><circle cx="30" cy="30" r="20"><animate attributeName="cx" values="30;60;30" dur="2s" repeatCount="indefinite"/></circle><text x="800" y="600">'+nonce+'</text></svg>'


async def settle(manager,run):
    task=manager.tasks.get(run['id'])
    if task:await task
    return manager.db.get(run['id'])


@pytest.mark.asyncio
async def test_one_generation_one_scene_300_seconds_safe_result(tmp_path):
    store=Store(tmp_path);config(store);calls=[]
    def upstream(req):
        value=json.loads(req.content);calls.append(value)
        prompt=value['messages'][0]['content'];nonce=re.search(r'code ([A-F0-9]{8})',prompt)[1]
        svg=response_svg(nonce)
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(svg),frame(reason='stop')]))
    manager=PelicanManager(store,tmp_path,httpx.MockTransport(upstream))
    info=await manager.info();assert info['timeout_seconds']==300 and info['max_parallel']==1
    run=await manager.enqueue({'provider_id':'1','model':MODEL,'confirm_cost':True})
    done,svg=await settle(manager,run)
    assert done['status']=='done' and done['svg_available'] and done['checks']['safe_svg'],done
    assert all(done['checks'].values()) and svg
    assert len(calls)==1 and calls[0]['max_tokens']==16000
    assert 'exactly ONE cute pelican' in calls[0]['messages'][0]['content']
    assert 'api_key' not in json.dumps(done) and done['generation_count']==1
    await manager.close()


@pytest.mark.asyncio
async def test_responses_protocol_and_no_auto_retry_on_failure(tmp_path):
    store=Store(tmp_path);config(store);calls=[]
    def upstream(req):
        value=json.loads(req.content);calls.append(value)
        prompt=value['input'][0]['content'][0]['text'];nonce=re.search(r'code ([A-F0-9]{8})',prompt)[1]
        out={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':response_svg(nonce)}]}],'usage':{'input_tokens':100,'output_tokens':200}}
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=('event: response.completed\ndata: '+json.dumps({'type':'response.completed','response':out})+'\n\n').encode())
    manager=PelicanManager(store,tmp_path,httpx.MockTransport(upstream));run=await manager.enqueue({'provider_id':'1','model':MODEL,'protocol':'responses','confirm_cost':True});done,svg=await settle(manager,run)
    assert done['status']=='done' and done['usage']['output_tokens']==200 and len(calls)==1
    assert calls[0]['max_output_tokens']==16000
    await manager.close()
    failed=[]
    manager=PelicanManager(store,tmp_path,httpx.MockTransport(lambda req:(failed.append(req) or httpx.Response(503))))
    run=await manager.enqueue({'provider_id':'1','model':MODEL,'confirm_cost':True});done,svg=await settle(manager,run)
    assert done['status']=='error' and not svg and len(failed)==1
    await manager.close()


@pytest.mark.asyncio
async def test_consent_model_validation_busy_cancel_and_timeout(tmp_path,monkeypatch):
    store=Store(tmp_path);config(store);manager=PelicanManager(store,tmp_path)
    with pytest.raises(ValueError,match='confirmation'):await manager.enqueue({'provider_id':'1','model':MODEL})
    with pytest.raises(ValueError,match='model_unavailable'):await manager.enqueue({'provider_id':'1','model':'arbitrary','confirm_cost':True})
    started=asyncio.Event()
    async def wait(*args):started.set();await asyncio.Event().wait()
    manager._generate=wait
    run=await manager.enqueue({'provider_id':'1','model':MODEL,'confirm_cost':True});await started.wait()
    with pytest.raises(ValueError,match='busy'):await manager.enqueue({'provider_id':'1','model':MODEL,'confirm_cost':True})
    assert await manager.cancel(run['id']);assert manager.db.get(run['id'])[0]['status']=='cancelled'
    monkeypatch.setattr('proxy.pelican.TIMEOUT_SECONDS',.01)
    run=await manager.enqueue({'provider_id':'1','model':MODEL,'confirm_cost':True});done,svg=await settle(manager,run)
    assert done['error_kind']=='pelican_timeout' and not svg
    await manager.close()


def test_restart_marks_interrupted_no_secret_fields(tmp_path):
    db=PelicanStore(tmp_path);db.save({'id':'pelican_test','status':'running','created_at':'now','api_key':'dont-save','generation_count':1})
    reopened=PelicanStore(tmp_path);run,svg=reopened.get('pelican_test')
    assert run['status']=='error' and run['error_kind']=='interrupted_by_restart' and 'api_key' not in run
